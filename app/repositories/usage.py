"""Data access for usage events. The only layer that writes SQL."""

import uuid
from datetime import datetime

from sqlalchemy import func, insert, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models import UsageEvent, UsageEventItem


def insert_event_if_new(
    session: Session,
    *,
    event_id: uuid.UUID,
    tenant_id: uuid.UUID,
    idempotency_key: str,
    kind: str,
    request_hash: str,
    response_body: dict,
    metrics: dict[str, int],
) -> uuid.UUID | None:
    """Insert one usage event with its metrics, or report a duplicate.

    Returns the event id, or None if this (tenant, key) pair already exists.
    Does not commit — the caller owns the transaction, so the event and its
    items land together or not at all.

    The id is supplied by the caller rather than generated here, because the
    response body stored on the row has to carry the same id the caller
    returns. Two independently generated UUIDs would make a replayed response
    disagree with the original.

    ON CONFLICT DO NOTHING is what makes this safe under concurrency. A
    SELECT-then-INSERT would let two simultaneous retries both find nothing
    and both insert; here Postgres arbitrates and exactly one wins.
    """
    stmt = (
        pg_insert(UsageEvent)
        .values(
            id=event_id,
            tenant_id=tenant_id,
            idempotency_key=idempotency_key,
            kind=kind,
            request_hash=request_hash,
            response_body=response_body,
        )
        .on_conflict_do_nothing(constraint="uq_usage_events_tenant_key")
        .returning(UsageEvent.id)
    )

    inserted_id = session.scalar(stmt)
    if inserted_id is None:
        return None

    rows = [
        {"event_id": inserted_id, "metric": metric, "quantity": quantity}
        for metric, quantity in metrics.items()
        if quantity > 0
    ]
    if rows:
        session.execute(insert(UsageEventItem), rows)

    return inserted_id


def get_event_by_key(
    session: Session, *, tenant_id: uuid.UUID, idempotency_key: str
) -> UsageEvent | None:
    """The event a duplicate request is replaying."""
    return session.scalar(
        select(UsageEvent).where(
            UsageEvent.tenant_id == tenant_id,
            UsageEvent.idempotency_key == idempotency_key,
        )
    )


def usage_since(
    session: Session, *, tenant_id: uuid.UUID, since: datetime
) -> dict[str, int]:
    """Quantity per metric for this tenant since `since`.

    This is the query ix_usage_events_tenant_created exists for: it filters
    on exactly (tenant_id, created_at).
    """
    rows = session.execute(
        select(UsageEventItem.metric, func.sum(UsageEventItem.quantity))
        .join(UsageEvent, UsageEvent.id == UsageEventItem.event_id)
        .where(
            UsageEvent.tenant_id == tenant_id,
            UsageEvent.created_at >= since,
        )
        .group_by(UsageEventItem.metric)
    ).all()
    return {metric: int(total) for metric, total in rows}