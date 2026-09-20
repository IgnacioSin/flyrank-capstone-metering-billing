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
from app.services.meter import IdempotencyKeyReused, record

router = APIRouter()


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

    # A replay is not a creation. 200 says "this already happened"; 201 would
    # claim a second event exists.
    response.status_code = 200 if result.replayed else 201
    response.headers["Idempotent-Replay"] = "true" if result.replayed else "false"
    return dict(sorted(result.response.items()))