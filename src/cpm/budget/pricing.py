"""Versioned price table and the cost arithmetic (docs/THREATS.md T-14, T-16).

All prices are integer micro-USD per million tokens, so a cost is an integer micro-USD amount
rounded UP (never under-counted). Costs computed here are ESTIMATES: the UI labels them so until
they are reconciled against the Console (SDK_NOTES section 6). A model with no price row is
charged the dearest price of every component, so an unknown model can only over-count.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from cpm.db.models import Price

_PER_MTOK = 1_000_000
MODEL_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
MAX_PRICE_MICROUSD_MTOK = 1_000_000_000  # 1000 USD per million tokens: a typo guard


class PriceError(ValueError):
    pass


@dataclass(frozen=True)
class Tier:
    input: int
    output: int
    cache_read: int
    cache_write: int


@dataclass(frozen=True)
class PriceRow:
    model: str
    version: int
    base: Tier
    long_threshold: int | None
    long: Tier | None
    source: str
    verified_on: str

    def tier_for(self, prompt_tokens: int) -> Tier:
        if self.long is not None and self.long_threshold and prompt_tokens > self.long_threshold:
            return self.long
        return self.base


def _row(p: Price) -> PriceRow:
    long: Tier | None = None
    if p.long_threshold and None not in (
        p.long_input_mtok,
        p.long_output_mtok,
        p.long_cache_read_mtok,
        p.long_cache_write_mtok,
    ):
        long = Tier(
            int(p.long_input_mtok or 0), int(p.long_output_mtok or 0),
            int(p.long_cache_read_mtok or 0), int(p.long_cache_write_mtok or 0),
        )  # fmt: skip
    return PriceRow(
        p.model, p.version,
        Tier(p.input_mtok, p.output_mtok, p.cache_read_mtok, p.cache_write_mtok),
        p.long_threshold, long, p.source, p.verified_on,
    )  # fmt: skip


def cost_microusd(
    tier: Tier, input_tokens: int, output_tokens: int, cache_read: int = 0, cache_write: int = 0
) -> int:
    total = (
        input_tokens * tier.input + output_tokens * tier.output
        + cache_read * tier.cache_read + cache_write * tier.cache_write
    )  # fmt: skip
    return -(-total // _PER_MTOK)


class PriceTable:
    """Current (highest-version) price per model, cached in memory, reloaded on every change."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self._lock = threading.Lock()
        self._current: dict[str, PriceRow] | None = None  # loaded on first use: the app builds
        # this object before the migrations have run

    def reload(self) -> None:
        with Session(self.engine) as s:
            rows = list(s.scalars(select(Price).order_by(Price.model, Price.version)))
        current = {r.model: _row(r) for r in rows}  # later versions overwrite earlier ones
        with self._lock:
            self._current = current

    def current(self) -> dict[str, PriceRow]:
        with self._lock:
            loaded = self._current
        if loaded is None:
            self.reload()
            with self._lock:
                loaded = self._current or {}
        return dict(loaded)

    def _fallback(self) -> Tier:
        tiers = [t for r in self.current().values() for t in (r.base, r.long) if t is not None]
        if not tiers:
            raise PriceError("the price table is empty")
        return Tier(
            max(t.input for t in tiers), max(t.output for t in tiers),
            max(t.cache_read for t in tiers), max(t.cache_write for t in tiers),
        )  # fmt: skip

    def estimate(
        self, model: str, input_tokens: int, output_tokens: int,
        cache_read: int = 0, cache_write: int = 0,
    ) -> tuple[int, int | None]:  # fmt: skip
        """(micro-USD estimate, price version used or None for the dearest-price fallback)."""
        for n in (input_tokens, output_tokens, cache_read, cache_write):
            if n < 0:
                raise PriceError("token counts cannot be negative")
        row = self.current().get(model)
        if row is None:
            tier, version = self._fallback(), None
        else:
            tier, version = row.tier_for(input_tokens + cache_read + cache_write), row.version
        return cost_microusd(tier, input_tokens, output_tokens, cache_read, cache_write), version

    def add_version(
        self, model: str, tier: Tier, *, long_threshold: int | None = None,
        long: Tier | None = None, source: str, verified_on: str,
    ) -> PriceRow:  # fmt: skip
        """Append a new price version for a model (old rows stay: past costs stay explainable)."""
        if not MODEL_RE.match(model):
            raise PriceError("invalid model id")
        for v in (tier.input, tier.output, tier.cache_read, tier.cache_write):
            if not 0 <= v <= MAX_PRICE_MICROUSD_MTOK:
                raise PriceError("price out of range")
        if (long is None) != (long_threshold is None):
            raise PriceError("long tier needs both a threshold and prices")
        if long is not None:
            for v in (long.input, long.output, long.cache_read, long.cache_write):
                if not 0 <= v <= MAX_PRICE_MICROUSD_MTOK:
                    raise PriceError("price out of range")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", verified_on):
            raise PriceError("verified_on must be YYYY-MM-DD")
        with Session(self.engine) as s:
            last = s.scalars(
                select(Price).where(Price.model == model).order_by(Price.version.desc()).limit(1)
            ).first()
            row = Price(
                model=model, version=(last.version + 1) if last else 1,
                input_mtok=tier.input, output_mtok=tier.output,
                cache_read_mtok=tier.cache_read, cache_write_mtok=tier.cache_write,
                long_threshold=long_threshold,
                long_input_mtok=long.input if long else None,
                long_output_mtok=long.output if long else None,
                long_cache_read_mtok=long.cache_read if long else None,
                long_cache_write_mtok=long.cache_write if long else None,
                source=source[:200], verified_on=verified_on,
            )  # fmt: skip
            s.add(row)
            s.commit()
            out = _row(row)
        self.reload()
        return out
