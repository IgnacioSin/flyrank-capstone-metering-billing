"""Metering. Knows nothing about HTTP — see DESIGN.md section 5."""

import hashlib
import json
import uuid
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.repositories import usage as usage_repo
from app.services.cost import calculate_cost_uusd, format_usd


class IdempotencyKeyReused(Exception):
    """Same key, different payload — a client bug, not a retry.

    Replaying the stored response here would answer a question the caller
    never asked.
    """


@dataclass
class MeterResult:
    event_id: uuid.UUID
    response: dict
    replayed: bool


def request_fingerprint(metrics: dict[str, int]) -> str:
    """A stable hash of the request payload.

    sort_keys matters: the same metrics in a different JSON order are the
    same request, and must not look like a key reused with new content.
    """
    canonical = json.dumps(metrics, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def record(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    idempotency_key: str,
    metrics: dict[str, int],
    kind: str = "ai_tokens",
) -> MeterResult:
    """Record usage exactly once for this (tenant, idempotency key)."""
    request_hash = request_fingerprint(metrics)
    cost_uusd = calculate_cost_uusd(metrics)

    event_id = uuid.uuid4()
    response = {
        "event_id": str(event_id),
        "metrics": metrics,
        "cost_uusd": cost_uusd,
        "cost_usd": format_usd(cost_uusd),
    }

    inserted_id = usage_repo.insert_event_if_new(
        session,
        event_id=event_id,
        tenant_id=tenant_id,
        idempotency_key=idempotency_key,
        kind=kind,
        request_hash=request_hash,
        response_body=response,
        metrics=metrics,
    )

    if inserted_id is not None:
        session.commit()
        return MeterResult(event_id=inserted_id, response=response, replayed=False)

    session.rollback()

    existing = usage_repo.get_event_by_key(
        session, tenant_id=tenant_id, idempotency_key=idempotency_key
    )
    if existing is None:
        # Only reachable if the row vanished between the conflict and this
        # read. Treating it as a conflict is safer than silently re-billing.
        raise IdempotencyKeyReused(idempotency_key)

    if existing.request_hash != request_hash:
        raise IdempotencyKeyReused(idempotency_key)

    return MeterResult(
        event_id=existing.id, response=existing.response_body, replayed=True
    )