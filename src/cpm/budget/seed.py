"""Version-1 price rows (micro-USD per million tokens). Mirrored literally in migration 0003.

Source: docs/SDK_NOTES.md section 5 and the Claude API model/pricing table, re-checked
2026-10-26 (see the note there about the Sonnet 5.5 cache-read price). Cache writes are the
5-minute rate; 1-hour writes are not used because CPM never sets ENABLE_PROMPT_CACHING_1H.
"""

from __future__ import annotations

from typing import Any

VERIFIED_ON = "2026-10-26"
SOURCE = "platform.claude.com pricing + SDK_NOTES s5"

# model -> (input, output, cache_read, cache_write_5m)
BASE: dict[str, tuple[int, int, int, int]] = {
    "claude-fable-5-1": (10_000_000, 50_000_000, 250_000, 12_500_000),
    "claude-opus-5-5": (4_000_000, 20_000_000, 200_000, 5_000_000),
    "claude-sonnet-5-5": (2_000_000, 10_000_000, 200_000, 2_500_000),
    "claude-haiku-5-5": (100_000, 500_000, 10_000, 125_000),
}
MODELS = tuple(BASE)  # the models a task may name (price table seeds); order = display order
# model -> (threshold tokens, input, output, cache_read, cache_write_5m) above the threshold
LONG: dict[str, tuple[int, int, int, int, int]] = {
    "claude-haiku-5-5": (100_000, 500_000, 2_500_000, 50_000, 625_000),
}


def rows() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for model, (i, o, cr, cw) in BASE.items():
        lg = LONG.get(model)
        out.append(
            {
                "model": model, "version": 1, "input_mtok": i, "output_mtok": o,
                "cache_read_mtok": cr, "cache_write_mtok": cw,
                "long_threshold": lg[0] if lg else None,
                "long_input_mtok": lg[1] if lg else None,
                "long_output_mtok": lg[2] if lg else None,
                "long_cache_read_mtok": lg[3] if lg else None,
                "long_cache_write_mtok": lg[4] if lg else None,
                "source": SOURCE, "verified_on": VERIFIED_ON,
            }
        )  # fmt: skip
    return out
