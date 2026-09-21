"""Stripe integration. See DESIGN.md section 4.

Payment truth lives at Stripe; this module never decides that a tenant is
entitled to something. It asks Stripe to start a checkout, and later applies
what Stripe reports back through verified events.
"""

import uuid
from datetime import datetime, timezone

import stripe
from sqlalchemy.orm import Session

from app.config import APP_BASE_URL, STRIPE_PRICE_ID_PRO, STRIPE_SECRET_KEY
from app.repositories import subscriptions as subscription_repo
from app.repositories import tenants as tenant_repo

stripe.api_key = STRIPE_SECRET_KEY


class MalformedEvent(Exception):
    """The event is missing a field this handler needs.

    Distinct from MissingTenantReference: that one is a timing problem worth
    retrying, this one means the payload does not have the shape expected,
    and a retry will produce the same result.
    """


def _billing_period(obj) -> tuple[datetime | None, datetime | None]:
    """Read the billing period, wherever this API version keeps it.

    Accepts either a plain dict from a stored webhook payload or a Stripe
    resource from an API call. The two look alike but are not the same type:
    a Stripe resource supports obj["key"] and rejects obj.get(), so it is
    converted first rather than special-cased at every access below.
    """
    if hasattr(obj, "to_dict"):
        obj = obj.to_dict()

    start = obj.get("current_period_start")
    end = obj.get("current_period_end")

    if start is None or end is None:
        items = (obj.get("items") or {}).get("data") or []
        if items:
            start = start or items[0].get("current_period_start")
            end = end or items[0].get("current_period_end")

    return _timestamp(start), _timestamp(end)


def create_checkout_session(*, tenant_id: uuid.UUID) -> str:
    """Start a subscription checkout and return the hosted payment URL.

    Deliberately does not touch the database. The customer has not paid yet,
    and even once they do, the plan changes because a signed webhook said so
    — not because this function was called.

    The tenant id travels in two places:

    - `client_reference_id`, the field Stripe provides for the caller's own
      identifier, which comes back on `checkout.session.completed`.
    - `subscription_data.metadata`, which attaches it to the *subscription*
      object rather than the session. Later `customer.subscription.updated`
      and `.deleted` events carry the subscription, not the session, so
      without this they would arrive with no way to tell whose they are.
    """
    session = stripe.checkout.Session.create(
        mode="subscription",
        line_items=[{"price": STRIPE_PRICE_ID_PRO, "quantity": 1}],
        client_reference_id=str(tenant_id),
        metadata={"tenant_id": str(tenant_id)},
        subscription_data={"metadata": {"tenant_id": str(tenant_id)}},
        success_url=(
            f"{APP_BASE_URL}/checkout/success"
            "?session_id={CHECKOUT_SESSION_ID}"
        ),
        cancel_url=f"{APP_BASE_URL}/checkout/cancel",
    )
    return session.url


# ---------------------------------------------------------------------------
# Applying what Stripe reported
# ---------------------------------------------------------------------------

# Statuses that entitle a tenant to the Pro plan. Anything else — past_due,
# unpaid, canceled, incomplete — drops them to Free limits, which is what
# makes the quota layer answer 402 instead of 429: there is a payment action
# that unblocks them.
_ENTITLED_STATUSES = {"active", "trialing"}


class MissingTenantReference(Exception):
    """The event carries no tenant id.

    Raised rather than swallowed so the job retries: Stripe does not
    guarantee event order, and a subscription event can arrive before the
    checkout that created its metadata.
    """


def _timestamp(value: int | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value, tz=timezone.utc)


def _plan_for(status: str) -> str:
    return "pro" if status in _ENTITLED_STATUSES else "free"


def _apply_checkout_completed(session: Session, obj: dict) -> str:
    raw_tenant = obj.get("client_reference_id") or (obj.get("metadata") or {}).get(
        "tenant_id"
    )
    if not raw_tenant:
        raise MissingTenantReference("checkout.session.completed")

    tenant_id = uuid.UUID(raw_tenant)
    subscription_id = obj.get("subscription")
    if not subscription_id:
        raise MalformedEvent(
            "checkout.session.completed carries no subscription id"
        )

    # One network call for the authoritative status and period. The session
    # does not carry them, and assuming "active" would make the mirror
    # disagree with Stripe the moment a payment is delayed.
    remote = stripe.Subscription.retrieve(subscription_id)

    status = remote["status"]
    plan_code = _plan_for(status)
    period_start, period_end = _billing_period(remote)

    subscription_repo.upsert(
        session,
        tenant_id=tenant_id,
        plan_code=plan_code,
        status=status,
        stripe_customer_id=obj["customer"],
        stripe_subscription_id=subscription_id,
        current_period_start=period_start,
        current_period_end=period_end,
    )

    tenant = tenant_repo.get(session, tenant_id)
    if tenant is None:
        raise MissingTenantReference(str(tenant_id))
    tenant.plan_code = plan_code

    return f"tenant {tenant_id} -> {plan_code} ({status})"


def _apply_subscription_change(session: Session, obj: dict, deleted: bool) -> str:
    raw_tenant = (obj.get("metadata") or {}).get("tenant_id")
    if not raw_tenant:
        raise MissingTenantReference(obj.get("id", "subscription"))

    tenant_id = uuid.UUID(raw_tenant)
    status = "canceled" if deleted else obj["status"]
    plan_code = _plan_for(status)

    period_start, period_end = _billing_period(obj)

    subscription_repo.upsert(
        session,
        tenant_id=tenant_id,
        plan_code=plan_code,
        status=status,
        stripe_customer_id=obj["customer"],
        stripe_subscription_id=obj["id"],
        current_period_start=period_start,
        current_period_end=period_end,
    )

    tenant = tenant_repo.get(session, tenant_id)
    if tenant is None:
        raise MissingTenantReference(str(tenant_id))
    tenant.plan_code = plan_code

    return f"tenant {tenant_id} -> {plan_code} ({status})"


def apply_event(session: Session, *, event_type: str, payload: dict) -> str:
    """Apply one verified Stripe event to the local mirror.

    Returns a one-line description for the job dashboard. Unhandled event
    types are ignored rather than treated as failures — Stripe sends plenty
    this system has no opinion about.
    """
    obj = payload["data"]["object"]

    if event_type == "checkout.session.completed":
        return _apply_checkout_completed(session, obj)
    if event_type == "customer.subscription.updated":
        return _apply_subscription_change(session, obj, deleted=False)
    if event_type == "customer.subscription.deleted":
        return _apply_subscription_change(session, obj, deleted=True)

    return f"ignored {event_type}"