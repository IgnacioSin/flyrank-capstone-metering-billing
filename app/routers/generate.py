"""HTTP layer for the billable endpoint.

This is the only file allowed to speak in status codes. It translates the
service's outcome into HTTP and nothing more — no SQL, no pricing, no quota
arithmetic.
"""

import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from sqlalchemy.orm import Session

from app.db import get_session
from app.schemas import GenerateRequest
from app.services.meter import (
    IdempotencyKeyReused,
    QuotaExceeded,
    TenantNotFound,
    record,
)

router = APIRouter()

# A rejection carries a remedy; the remedy decides the status code.
# "upgrade" -> 402: a payment action unblocks this caller.
# "wait"    -> 429: the plan is paid and the period is spent.
_REMEDY_STATUS = {"upgrade": 402, "wait": 429}


def _normalize(value):
    """Sort every nested object so a replay is byte-identical to the original.

    JSONB does not preserve key order, and the acceptance probe compares the
    two responses.
    """
    if isinstance(value, dict):
        return {k: _normalize(v) for k, v in sorted(value.items())}
    if isinstance(value, list):
        return [_normalize(v) for v in value]
    return value


@router.post("/generate")
def generate(
    payload: GenerateRequest,
    response: Response,
    session: Session = Depends(get_session),
    x_tenant_id: str = Header(...),
    idempotency_key: str = Header(...),
) -> dict:
    try:
        tenant_id = uuid.UUID(x_tenant_id)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_tenant_id",
                "message": "X-Tenant-Id must be a UUID",
            },
        ) from None

    if not idempotency_key.strip():
        raise HTTPException(
            status_code=400,
            detail={
                "error": "missing_idempotency_key",
                "message": "Idempotency-Key must not be empty",
            },
        )

    try:
        result = record(
            session,
            tenant_id=tenant_id,
            idempotency_key=idempotency_key,
            metrics=payload.as_metrics(),
        )
    except TenantNotFound:
        raise HTTPException(
            status_code=404,
            detail={"error": "tenant_not_found", "message": "Unknown tenant."},
        ) from None
    except IdempotencyKeyReused:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "idempotency_key_reused",
                "message": (
                    "This Idempotency-Key was already used with a different "
                    "request body."
                ),
            },
        ) from None
    except QuotaExceeded as exc:
        rejection = exc.rejection
        status = _REMEDY_STATUS[rejection.remedy]
        message = (
            f"This request needs {rejection.requested} {rejection.limit_name} "
            f"but only {max(0, rejection.limit - rejection.used)} remain of "
            f"{rejection.limit} this period."
        )
        if rejection.remedy == "upgrade":
            message += " Upgrade to Pro for a higher limit."
        else:
            message += " The quota resets at the start of next month."
        raise HTTPException(
            status_code=status,
            detail={
                "error": "quota_exceeded",
                "limit_name": rejection.limit_name,
                "limit": rejection.limit,
                "used": rejection.used,
                "requested": rejection.requested,
                "message": message,
            },
        ) from None

    # A replay is not a creation. 200 says "this already happened"; 201 would
    # claim a second event exists.
    response.status_code = 200 if result.replayed else 201
    response.headers["Idempotent-Replay"] = "true" if result.replayed else "false"

    # Sorted so a replayed body is byte-identical to the original: JSONB does
    # not preserve key order, and the acceptance probe compares responses.
    return _normalize(result.response)