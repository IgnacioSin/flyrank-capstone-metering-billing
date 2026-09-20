"""Quota enforcement. See DESIGN.md sections 2.2 and 3.

Knows nothing about HTTP: a rejection carries a `remedy`, and the router
decides which status code that remedy deserves.
"""

from dataclasses import dataclass
from datetime import datetime, timezone

from app.config import TOKEN_METRICS
from app.models import Plan, Tenant

# Metrics that count toward each plan limit.
_API_CALL_METRICS = ("api_calls",)


@dataclass
class QuotaRejection:
    """Why a request was blocked, in numbers the caller can act on."""

    limit_name: str
    limit: int
    used: int
    requested: int
    remedy: str  # "upgrade" -> payment unblocks it; "wait" -> next period


def period_start(now: datetime | None = None) -> datetime:
    """First instant of the current UTC calendar month.

    Quotas reset on the calendar month, deliberately not on the Stripe
    billing period — see the non-goal in DESIGN.md section 6. UTC, so the
    reset does not move with the server's timezone.
    """
    now = now or datetime.now(timezone.utc)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _total(metrics: dict[str, int], names: tuple[str, ...]) -> int:
    return sum(metrics.get(name, 0) for name in names)


def check(
    *,
    tenant: Tenant,
    plan: Plan,
    used: dict[str, int],
    requested: dict[str, int],
) -> QuotaRejection | None:
    """None when the request fits, a rejection when it does not.

    The boundary rule, from DESIGN.md section 3.1:

        current_usage + requested <= limit  ->  allowed

    At 999 of 1,000 a request for 1 is allowed and leaves the tenant at
    exactly 1,000. At 1,000 the next request is rejected. The quota is a
    ceiling that may be reached but not crossed.
    """
    remedy = "upgrade" if plan.code == "free" else "wait"

    checks = (
        ("api_calls", _API_CALL_METRICS, plan.api_call_limit),
        ("tokens", TOKEN_METRICS, plan.token_limit),
    )

    for limit_name, metrics, limit in checks:
        used_total = _total(used, metrics)
        requested_total = _total(requested, metrics)
        if used_total + requested_total > limit:
            return QuotaRejection(
                limit_name=limit_name,
                limit=limit,
                used=used_total,
                requested=requested_total,
                remedy=remedy,
            )

    return None


def summarize(
    *, plan: Plan, used: dict[str, int]
) -> dict[str, dict[str, int]]:
    """Used and remaining per limit, for GET /usage and for response bodies."""
    api_used = _total(used, _API_CALL_METRICS)
    token_used = _total(used, TOKEN_METRICS)
    return {
        "api_calls": {
            "used": api_used,
            "limit": plan.api_call_limit,
            "remaining": max(0, plan.api_call_limit - api_used),
        },
        "tokens": {
            "used": token_used,
            "limit": plan.token_limit,
            "remaining": max(0, plan.token_limit - token_used),
        },
    }