"""Data access for the local mirror of Stripe subscriptions."""

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Subscription


def get_by_tenant(session: Session, tenant_id: uuid.UUID) -> Subscription | None:
    return session.scalar(
        select(Subscription).where(Subscription.tenant_id == tenant_id)
    )


def upsert(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    plan_code: str,
    status: str,
    stripe_customer_id: str,
    stripe_subscription_id: str,
    current_period_start=None,
    current_period_end=None,
) -> Subscription:
    """Write what Stripe reported. One subscription per tenant.

    An upsert rather than an insert because events arrive more than once and
    out of order: the same subscription is updated by checkout completion,
    then by status changes, then by cancellation.
    """
    subscription = get_by_tenant(session, tenant_id)
    if subscription is None:
        subscription = Subscription(tenant_id=tenant_id)
        session.add(subscription)

    subscription.plan_code = plan_code
    subscription.status = status
    subscription.stripe_customer_id = stripe_customer_id
    subscription.stripe_subscription_id = stripe_subscription_id
    subscription.current_period_start = current_period_start
    subscription.current_period_end = current_period_end
    return subscription