"""Metering. Knows nothing about HTTP — see DESIGN.md section 5."""

import hashlib
import json
import uuid
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.repositories import tenants as tenant_repo
from app.repositories import usage as usage_repo
from app.services import quota
from app.services.cost import calculate_cost_uusd, format_usd


class TenantNotFound(Exception):
    pass


class IdempotencyKeyReused(Exception):
    """Same key, different payload — a client bug, not a retry.

    Replaying the stored response here would answer a question the caller
    never asked.
    """


class QuotaExceeded(Exception):
    def __init__(self, rejection: quota.QuotaRejection) -> None:
        self.rejection = rejection
        super().__init__(rejection.limit_name)


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
    """Record usage exactly once, if the tenant's plan has room for it.

    Order matters here. The replay check runs *before* the quota check,
    because a retry of a request that was already recorded must never be
    rejected: that usage is already counted, and answering 429 to a retry
    would break idempotency for any tenant sitting at its limit.
    """
    tenant = tenant_repo.lock_tenant(session, tenant_id)
    if tenant is None:
        raise TenantNotFound(tenant_id)

    plan = tenant_repo.get_plan(session, tenant.plan_code)
    if plan is None:
        raise TenantNotFound(tenant_id)

    request_hash = request_fingerprint(metrics)

    existing = usage_repo.get_event_by_key(
        session, tenant_id=tenant_id, idempotency_key=idempotency_key
    )
    if existing is not None:
        if existing.request_hash != request_hash:
            raise IdempotencyKeyReused(idempotency_key)
        return MeterResult(
            event_id=existing.id, response=existing.response_body, replayed=True
        )

    used = usage_repo.usage_since(
        session, tenant_id=tenant_id, since=quota.period_start()
    )
    rejection = quota.check(
        tenant=tenant, plan=plan, used=used, requested=metrics
    )
    if rejection is not None:
        raise QuotaExceeded(rejection)

    event_id = uuid.uuid4()
    cost_uusd = calculate_cost_uusd(metrics)

    # Usage after this event lands, so the caller sees what it has left.
    projected = {
        metric: used.get(metric, 0) + metrics.get(metric, 0)
        for metric in set(used) | set(metrics)
    }

    response = {
        "event_id": str(event_id),
        "metrics": metrics,
        "cost_uusd": cost_uusd,
        "cost_usd": format_usd(cost_uusd),
        "quota": quota.summarize(plan=plan, used=projected),
    }

    inserted_id = usage_repo.insert_event_if_new(
        session,
        event_id=event_id,
        tenant_id=tenant_id,
        idempotency_key=idempotency_key,
        kind=kind,
        request_hash=request_hash,
        response_body=response,
        cost_uusd=cost_uusd,
        metrics=metrics,
    )

    if inserted_id is None:
        # Unreachable while the tenant row is locked. Kept as a second line
        # of defence: if a request ever reaches here without the lock, the
        # unique constraint still refuses to double-count.
        session.rollback()
        raise IdempotencyKeyReused(idempotency_key)

    session.commit()
    return MeterResult(event_id=event_id, response=response, replayed=False)