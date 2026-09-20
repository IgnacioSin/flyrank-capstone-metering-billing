"""Period rollup for GET /usage. See DESIGN.md section 3."""

import uuid
from datetime import datetime

from sqlalchemy.orm import Session

from app.config import TOKEN_METRICS
from app.repositories import tenants as tenant_repo
from app.repositories import usage as usage_repo
from app.services import quota
from app.services.cost import format_usd


class TenantNotFound(Exception):
    pass


def summary(session: Session, *, tenant_id: uuid.UUID) -> dict:
    """Used, limit and cost for the tenant's current period.

    The cost is the sum of what each event was billed at, not a fresh
    calculation from current rates. Repricing history would mean a rollup for
    a closed month changes the day someone edits a constant.
    """
    tenant = tenant_repo.get(session, tenant_id)
    if tenant is None:
        raise TenantNotFound(tenant_id)

    plan = tenant_repo.get_plan(session, tenant.plan_code)
    if plan is None:
        raise TenantNotFound(tenant_id)

    since: datetime = quota.period_start()
    used = usage_repo.usage_since(session, tenant_id=tenant_id, since=since)
    cost_uusd = usage_repo.cost_since(session, tenant_id=tenant_id, since=since)

    return {
        "tenant_id": str(tenant_id),
        "plan": plan.code,
        "period_start": since.isoformat(),
        "usage": quota.summarize(plan=plan, used=used),
        # Per-metric totals, so the cost above can be checked by hand against
        # the pinned rates in app/config.py.
        "breakdown": {
            metric: used.get(metric, 0)
            for metric in ("api_calls", *TOKEN_METRICS)
        },
        "cost_uusd": cost_uusd,
        "cost_usd": format_usd(cost_uusd),
    }