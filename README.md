# Usage Metering & Billing Engine

A multi-tenant backend that answers the three questions every SaaS product
has to answer: how much has this customer used, what does it cost, and have
they hit their limit.

Usage is metered exactly once even under retries, quotas are enforced at the
exact boundary, AI-token costs are calculated with the rules that make token
pricing awkward, and subscriptions are synchronized from Stripe through
signature-verified webhooks.

FlyRank Internship · Backend Track · Capstone.

## What makes it interesting

Billing systems look simple until the real world shows up. The three things
this build is actually about:

**Exactly-once metering.** The same request retried after a network timeout
must record one usage event, not two. The guarantee is a unique constraint
plus `INSERT ... ON CONFLICT DO NOTHING` — not an application-level check,
which two concurrent retries would both pass.

**Honest boundaries.** At 999 of 1,000, one more call is allowed. At exactly
1,000, the next is refused. And a *retry* at exactly 1,000 is replayed, not
refused, because that usage is already counted.

**Token pricing that cannot be added up.** Cached input bills at a tenth of
fresh input; reasoning tokens bill at the output rate rather than free. The
four categories are priced separately and only the results are summed.

## Architecture

```text
                    ┌──────────────────────────────────────┐
   billable         │  POST /generate                      │
   request   ─────► │    ↓                                 │
                    │  MeterService                        │
                    │    lock tenant row (FOR UPDATE)      │
                    │    ↓                                 │
                    │    key already seen? ──► replay 200  │
                    │    ↓ no                              │
                    │  QuotaService                        │
                    │    used + requested <= limit?        │
                    │    ↓ no ──────────────► 402 / 429    │
                    │    ↓ yes                             │
                    │  CostCalculator ──► µUSD, rounded    │
                    │    ↓                  once           │
                    │  INSERT ... ON CONFLICT DO NOTHING   │
                    │    ↓                                 │
                    └──── 201 ─────────────────────────────┘

   GET /usage  ────► rollup(usage_events)  ──► used · limit · cost

   POST /checkout ─► Stripe Checkout URL   (writes nothing)

                    ┌──────────────────────────────────────┐
   Stripe           │  POST /webhooks/stripe               │
   signed    ─────► │    verify signature (raw bytes)      │
   event            │      ↓ forged ─────────► 400         │
                    │    dedupe on event id                │
                    │      ↓ duplicate ──────► 200         │
                    │    enqueue ──────────► 200 (fast)    │
                    └────────┬─────────────────────────────┘
                             │
                             ▼
                    Inngest job: apply-stripe-event
                      retrieve authoritative status
                      update subscription + tenant plan
```

Three layers, enforced by convention and reviewed as such: routers translate
HTTP and issue no SQL, services hold the rules and import nothing from
FastAPI, repositories are the only place queries are written.

Full reasoning in [DESIGN.md](DESIGN.md). Proof that each requirement works
in [EVIDENCE.md](EVIDENCE.md). Where AI helped and where it was wrong in
[BUILDLOG.md](BUILDLOG.md).

## Running it

Requires Python 3.10+ (built on 3.14), Docker, and Node.js for `npx`.

### Setup

```bash
python -m venv .venv
source .venv/Scripts/activate    # Git Bash on Windows; bin/activate elsewhere
pip install -r requirements.txt
cp .env.example .env
```

Fill in `.env`. `DATABASE_URL` works as shipped; the Stripe values come from
a free test-mode account (no card required):

| Variable | Where it comes from |
| --- | --- |
| `STRIPE_SECRET_KEY` | Stripe dashboard → Developers → API keys (`sk_test_…`) |
| `STRIPE_PRICE_ID_PRO` | The `price_…` of a recurring monthly product you create |
| `STRIPE_WEBHOOK_SECRET` | Printed by `stripe listen` — see below |

### Terminal 1 — database, migrations, API

```bash
docker compose up -d && alembic upgrade head && python -m uvicorn app.main:app --reload
```

Then seed the plans and a demo tenant:

```bash
python -m scripts.seed
```

It prints the demo tenant's UUID. Every request below needs it.

### Terminal 2 — the job runner

```bash
npx inngest-cli@latest dev -u http://localhost:8000/api/inngest
```

Dashboard at `http://localhost:8288`. Webhook processing runs here, so
subscription changes do not apply without it.

### Terminal 3 — Stripe webhook delivery

```bash
stripe listen \
  --events checkout.session.completed,customer.subscription.updated,customer.subscription.deleted \
  --forward-to localhost:8000/webhooks/stripe
```

This prints a fresh `whsec_…` every time it starts. Put it in `.env` as
`STRIPE_WEBHOOK_SECRET` and restart Terminal 1 — `--reload` watches `.py`
files, not `.env`.

Three processes rather than one because `stripe listen` needs an interactive
`stripe login` against the operator's own Stripe account, so the webhook path
cannot be containerized away. Terminals 2 and 3 are only needed for the
Stripe probes; metering, quotas and the rollup work with Terminal 1 alone.

## Trying it

```bash
TENANT=<uuid printed by the seed>
```

**Meter some usage:**

```bash
curl -i -X POST http://localhost:8000/generate \
  -H "Content-Type: application/json" \
  -H "X-Tenant-Id: $TENANT" \
  -H "Idempotency-Key: demo-1" \
  -d '{"input_tokens":1000,"cached_input_tokens":500,"output_tokens":2000,"reasoning_tokens":300}'
```

`201`, with the cost and the quota remaining after this call.

**Send it again, unchanged.** `200`, `Idempotent-Replay: true`, and a body
identical to the first — same `event_id`, same cost. The database holds one
event.

**Reuse the key with a different body.** `422`: this is a client bug, not a
retry, and replaying the stored response would answer a question nobody
asked.

**See the rollup:**

```bash
curl -s http://localhost:8000/usage -H "X-Tenant-Id: $TENANT"
```

**Upgrade to Pro:**

```bash
curl -s -X POST http://localhost:8000/checkout -H "X-Tenant-Id: $TENANT"
```

Open the returned URL, pay with `4242 4242 4242 4242` and any future expiry.
The tenant stays on Free until the webhook is processed — that is the point,
not a delay. Watch Terminal 3 forward the event and Terminal 2 run the job,
then check `/usage` again for the new limits.

## Plans and pricing

| Plan | API calls / month | AI tokens / month | Price |
| --- | --- | --- | --- |
| Free | 1,000 | 100,000 | — |
| Pro | 50,000 | 5,000,000 | $29 / month |

Quotas reset on the UTC calendar month. Costs are stored as integer
micro-USD (1 µUSD = 10⁻⁶ USD) because a small request costs a fraction of a
cent and would round to zero in integer cents. Token rates are pinned in
`app/config.py`, mirroring Gemini 3.6 Flash pricing as published in
September 2026.

## Limitations

Honest list of what this does not do.

**No authentication.** Tenant identity is an unverified `X-Tenant-Id` header:
anyone who can reach the API can claim to be any tenant. Every query is still
scoped by tenant at the data layer, so adding auth means changing where the
id comes from, not rewriting the isolation.

**Quota and billing periods do not align.** Quotas reset on the 1st; Stripe
bills from the subscription's own anniversary. A tenant who subscribes on the
15th has a quota month and a billing month that disagree.

**No proration, invoicing or overage.** A mid-cycle upgrade takes effect
immediately with no partial charge. Requests over quota are refused, not
billed.

**Pro's price is not calibrated.** At the pinned rates, 5,000,000 output
tokens cost $37.50, so a worst-case Pro tenant is served at a loss on a $29
plan. Modelling margin was out of scope.

**Test mode only.** The Stripe account is registered in the United States
because Argentina is not a supported country for Stripe accounts. This has no
effect on test mode and the account is never activated for live payments.

**The quota lock serializes per tenant.** `SELECT ... FOR UPDATE` on the
tenant row makes boundary enforcement exact, at the cost of serializing that
tenant's billable requests. Correct at this scale; a high-volume metering
system would trade some exactness for throughput, which is a deliberate
trade-off rather than an oversight.
