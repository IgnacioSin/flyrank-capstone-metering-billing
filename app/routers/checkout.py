"""HTTP layer for starting a subscription checkout."""

import uuid

import stripe
from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.orm import Session

from app.db import get_session
from app.repositories import tenants as tenant_repo
from app.services.billing import create_checkout_session

router = APIRouter()


@router.post("/checkout")
def checkout(
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

    if tenant_repo.get(session, tenant_id) is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "tenant_not_found", "message": "Unknown tenant."},
        )

    try:
        url = create_checkout_session(tenant_id=tenant_id)
    except stripe.StripeError as exc:
        # Stripe being unreachable or misconfigured is not the caller's
        # fault, but it is also not a 500 with a stack trace: the message
        # Stripe returns is the one worth surfacing.
        raise HTTPException(
            status_code=502,
            detail={"error": "stripe_error", "message": str(exc)},
        ) from None

    return {"checkout_url": url}


@router.get("/checkout/success")
def checkout_success(session_id: str | None = None) -> dict:
    """Where Stripe sends the customer after payment.

    Says nothing about the subscription being active: at this point the
    webhook may not have arrived. The browser landing here is not proof of
    anything — only the signed event is.
    """
    return {
        "status": "checkout_completed",
        "session_id": session_id,
        "note": "Plan changes apply once the webhook is processed.",
    }


@router.get("/checkout/cancel")
def checkout_cancel() -> dict:
    return {"status": "checkout_cancelled"}