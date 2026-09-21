"""Background jobs. Shared requirement #3.

The webhook endpoint answers Stripe in milliseconds; the work of applying an
event happens here, off the request path. Inngest supplies the retries with
backoff and the failure handler, and its dashboard is the evidence that a run
happened at all.
"""

import inngest

from app.db import SessionLocal
from app.repositories import webhooks as webhook_repo
from app.services.billing import apply_event

inngest_client = inngest.Inngest(app_id="metering-billing", is_production=False)

WEBHOOK_EVENT = "stripe/webhook.received"


async def _on_failure(ctx: inngest.Context) -> None:
    """Runs only after every retry is spent.

    Without it a failed event sits at status "received" forever and nothing
    distinguishes "still working" from "never going to happen" — the same
    lesson as the report that stayed pending in A7.
    """
    stripe_event_id = ctx.event.data["event"]["data"]["stripe_event_id"]
    with SessionLocal() as session:
        webhook_repo.mark_processed(session, stripe_event_id, "failed")
        session.commit()


@inngest_client.create_function(
    fn_id="apply-stripe-event",
    trigger=inngest.TriggerEvent(event=WEBHOOK_EVENT),
    # Three attempts total. Stripe does not guarantee event order, so a
    # subscription change can arrive before the checkout that created its
    # metadata; by the second attempt the other event has usually landed.
    retries=2,
    on_failure=_on_failure,
)
async def apply_stripe_event(ctx: inngest.Context) -> str:
    stripe_event_id = ctx.event.data["stripe_event_id"]

    def work() -> str:
        with SessionLocal() as session:
            event = webhook_repo.get_event(session, stripe_event_id)
            if event is None:
                raise RuntimeError(f"no stored event {stripe_event_id}")
            if event.status == "processed":
                return f"already processed {stripe_event_id}"

            result = apply_event(
                session, event_type=event.event_type, payload=event.payload
            )
            webhook_repo.mark_processed(session, stripe_event_id, "processed")
            session.commit()
            return result

    return await ctx.step.run("apply-event", work)