"""HTTP layer for the rollup endpoint."""

import uuid

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.orm import Session

from app.db import get_session
from app.services.rollup import TenantNotFound, summary

router = APIRouter()


@router.get("/usage")
def usage(
    session: Session = Depends(get_session),
    x_tenant_id: str = Header(...),
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

    try:
        return summary(session, tenant_id=tenant_id)
    except TenantNotFound:
        raise HTTPException(
            status_code=404,
            detail={"error": "tenant_not_found", "message": "Unknown tenant."},
        ) from None