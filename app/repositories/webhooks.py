"""Data access for webhook deduplication."""

from datetime import datetime, timezone

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models import ProcessedWebhookEvent


def record_event(
    session: Session, *, stripe_event_id: str, event_type: str, payload: dict
) -> bool:
    """Claim this event id. True if it is new, False if already seen.

    Stripe delivers at least once: a slow response, a network blip, or a
    manual replay all produce the same event twice. The primary key on
    stripe_event_id is the dedup, and ON CONFLICT DO NOTHING makes claiming
    it atomic — the same shape as the metering path, on a different table.
    """
    stmt = (
        pg_insert(ProcessedWebhookEvent)
        .values(
            stripe_event_id=stripe_event_id,
            event_type=event_type,
            payload=payload,
            status="received",
        )
        .on_conflict_do_nothing(index_elements=["stripe_event_id"])
        .returning(ProcessedWebhookEvent.stripe_event_id)
    )
    return session.scalar(stmt) is not None


def get_event(session: Session, stripe_event_id: str) -> ProcessedWebhookEvent | None:
    return session.get(ProcessedWebhookEvent, stripe_event_id)


def mark_processed(session: Session, stripe_event_id: str, status: str) -> None:
    event = session.get(ProcessedWebhookEvent, stripe_event_id)
    if event is not None:
        event.status = status
        event.processed_at = datetime.now(timezone.utc)