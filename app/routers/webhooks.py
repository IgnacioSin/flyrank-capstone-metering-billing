"""HTTP layer for Stripe webhooks. See DESIGN.md section 4."""

import json

import inngest
import stripe
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy.orm import Session

from app.config import STRIPE_WEBHOOK_SECRET
from app.db import get_session
from app.jobs import WEBHOOK_EVENT, inngest_client
from app.repositories import webhooks as webhook_repo

router = APIRouter()


@router.post("/webhooks/stripe")
async def stripe_webhook(
    request: Request,
    session: Session = Depends(get_session),
    stripe_signature: str = Header(default=""),
) -> dict:
    # The raw bytes, not a parsed model. Stripe signs the exact body it sent;
    # parsing to a dict and re-serializing changes whitespace and key order,
    # and verification then fails on a perfectly legitimate payload. This is
    # the one endpoint in the API that deliberately skips Pydantic.
    raw = await request.body()

    if not STRIPE_WEBHOOK_SECRET:
        raise HTTPException(
            status_code=500,
            detail={
                "error": "webhook_not_configured",
                "message": "STRIPE_WEBHOOK_SECRET is not set.",
            },
        )

    try:
        event = stripe.Webhook.construct_event(
            raw, stripe_signature, STRIPE_WEBHOOK_SECRET
        )
    except (ValueError, stripe.SignatureVerificationError):
        # A forged or malformed event is rejected before anything is read
        # out of it. Nothing is stored, nothing is enqueued, nothing changes.
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_signature",
                "message": "Signature verification failed.",
            },
        ) from None

    is_new = webhook_repo.record_event(
        session,
        stripe_event_id=event["id"],
        event_type=event["type"],
        payload=json.loads(raw),
    )

    if not is_new:
        session.rollback()
        # Still a 200. Answering with an error would tell Stripe the delivery
        # failed, and it would keep redelivering an event already handled.
        return {"status": "duplicate", "stripe_event_id": event["id"]}

    session.commit()

    await inngest_client.send(
        inngest.Event(
            name=WEBHOOK_EVENT, data={"stripe_event_id": event["id"]}
        )
    )

    return {"status": "accepted", "stripe_event_id": event["id"]}