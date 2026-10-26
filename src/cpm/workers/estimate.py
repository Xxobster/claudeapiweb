"""Client-side cost estimate per message (temporary until the WP8 price table).

Prices are USD per million tokens from docs/SDK_NOTES.md section 5 (verified 2026-10-21), stored
as integer micro-USD per million tokens so the arithmetic stays in integers
(docs/THREATS.md T-16). The
result is an ESTIMATE; the UI labels it so. WP8 replaces this with the versioned DB price table.
"""

from __future__ import annotations

# model -> (input, output, cache_read, cache_write_5m) in micro-USD per million tokens
PRICES: dict[str, tuple[int, int, int, int]] = {
    "claude-fable-5-1": (10_000_000, 50_000_000, 250_000, 12_500_000),
    "claude-opus-5-5": (4_000_000, 20_000_000, 200_000, 5_000_000),
    "claude-sonnet-5-5": (2_000_000, 10_000_000, 100_000, 2_500_000),
    "claude-haiku-5-5": (100_000, 500_000, 10_000, 125_000),
}
_PER_MTOK = 1_000_000


def estimate_microusd(
    model: str, input_tokens: int, output_tokens: int, cache_read: int = 0, cache_write: int = 0
) -> int:
    """Estimated cost in micro-USD; unknown models use the dearest price (never under-count)."""
    p_in, p_out, p_read, p_write = PRICES.get(model) or max(PRICES.values())
    total = (
        input_tokens * p_in + output_tokens * p_out + cache_read * p_read + cache_write * p_write
    )
    return -(-total // _PER_MTOK)
