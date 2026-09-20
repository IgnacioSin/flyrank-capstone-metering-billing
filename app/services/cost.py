"""Cost calculation. See DESIGN.md section 2.3.

Every number here is an integer. Float never enters the calculation, not even
in the final division — 0.1 + 0.2 is not 0.3, and money that drifts is money
someone eventually notices.
"""

from app.config import API_CALL_RATE_UUSD, TOKEN_RATES_UUSD_PER_MILLION

# The raw accumulator counts in micro-USD x 1,000,000, because token rates are
# quoted per million tokens. Dividing down happens once, at the end.
_SCALE = 1_000_000


def raw_contribution(metric: str, quantity: int) -> int:
    """This metric's share of the raw accumulator."""
    if metric == "api_calls":
        return quantity * API_CALL_RATE_UUSD * _SCALE
    try:
        return quantity * TOKEN_RATES_UUSD_PER_MILLION[metric]
    except KeyError:
        raise ValueError(f"no pinned rate for metric {metric!r}") from None


def calculate_cost_uusd(metrics: dict[str, int]) -> int:
    """Total cost in micro-USD, rounded exactly once.

    Rounding each metric separately would accumulate error across the four
    token categories and again across every event in a month, always in
    whichever direction the truncation happens to fall.
    """
    raw = sum(raw_contribution(m, q) for m, q in metrics.items())
    # Round half up with integers only; `round(raw / _SCALE)` would route the
    # value through a float and use banker's rounding on the way.
    return (raw + _SCALE // 2) // _SCALE


def format_usd(uusd: int) -> str:
    """Micro-USD as a dollar string, for humans reading a response body."""
    return f"${uusd / 1_000_000:.6f}"