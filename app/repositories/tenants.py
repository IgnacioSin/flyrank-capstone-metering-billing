"""Data access for tenants and plans."""

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Plan, Tenant


def lock_tenant(session: Session, tenant_id: uuid.UUID) -> Tenant | None:
    """Read the tenant and hold a row lock until the transaction ends.

    This is what makes quota enforcement exact. Without it, two concurrent
    requests both read the same usage total, both conclude there is room, and
    both record — leaving the tenant over its limit. The unique constraint on
    usage_events cannot catch that: the two requests carry different
    idempotency keys and neither row conflicts. The conflict is in the
    aggregate, so the lock has to be on something that already exists.

    Contention is per tenant, so one busy customer never blocks another.
    """
    return session.scalar(
        select(Tenant).where(Tenant.id == tenant_id).with_for_update()
    )


def get_plan(session: Session, code: str) -> Plan | None:
    return session.get(Plan, code)