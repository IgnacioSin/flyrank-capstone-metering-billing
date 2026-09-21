# Design — Usage Metering & Billing Engine

FlyRank Internship · Backend Track · Capstone
Author: Juan Ignacio Sin

This started as the one-page Phase 1 design document and has been kept
current as the build went on. Where an implementation decision contradicted
the original plan, the document was changed and the reason recorded rather
than leaving the plan and the code disagreeing.

## 1. The problem

A multi-tenant SaaS backend that answers three questions: how much has a
tenant used this period, what does that usage cost, and is the tenant allowed
to do this next action. Subscription state is synchronized from Stripe (test
mode) through signature-verified webhooks.

The system must stay correct under conditions that break naive
implementations: a network retry that must not double-charge, a webhook
delivered twice, two concurrent requests racing at the exact quota boundary.
Correctness here is not a nice-to-have — a bug either overcharges a customer
or gives away service for free.

## 2. Data model

Six tables. Postgres, schema managed as Alembic migrations.

| Table | Columns (essential) | Notes |
| --- | --- | --- |
| `tenants` | `id`, `name`, `plan_code`, `created_at` | One customer organization. |
| `plans` | `code`, `api_call_limit`, `token_limit` | Quotas only — no prices (§2.4). |
| `subscriptions` | `tenant_id`, `plan_code`, `status`, `stripe_customer_id`, `stripe_subscription_id`, `current_period_start`, `current_period_end` | Mirror of Stripe. Written only by verified webhooks. |
| `usage_events` | `id`, `tenant_id`, `idempotency_key`, `kind`, `request_hash`, `response_body`, `cost_uusd`, `created_at` | One row per billable *request*. |
| `usage_event_items` | `event_id`, `metric`, `quantity` | One row per metric within an event. |
| `processed_webhook_events` | `stripe_event_id` (PK), `event_type`, `payload`, `status`, `received_at`, `processed_at` | Webhook deduplication and audit trail. |

### 2.1 Why parent/child for usage events

An API call is one number. A token event is four numbers that cannot be
summed together (input, cached input, output, reasoning). Splitting them into
separate `usage_events` rows would break idempotency: the unit that must be
deduplicated is the *request*, not the metric. With a parent row carrying the
idempotency key and child rows carrying quantities, the uniqueness constraint
sits exactly on the thing being retried.

### 2.2 Why `tenants.plan_code` as well as `subscriptions`

Not in the original plan. `subscriptions` is the mirror of what Stripe says;
`tenants.plan_code` is the plan the API enforces right now. Quota checks read
the tenant, which keeps the hot path to one row and means the system still
answers correctly for a tenant who has never been through Stripe at all.
Webhook processing writes both, so they only diverge if processing failed —
which is visible as a `processed_webhook_events` row that never reached
`processed`.

### 2.3 Quota window

Quotas reset on the **UTC calendar month**. The rollup filter is
`created_at >= date_trunc('month', now())`.

This deliberately does not match Stripe's billing period: a tenant who
subscribes on the 15th is billed 15th-to-15th while their quota resets on the
1st. Aligning the two is a non-goal (§6). UTC rather than local time so the
reset does not move with the server's timezone.

### 2.4 Money

All money is stored as **integer micro-USD** (1 µUSD = 10⁻⁶ USD). Cents are
too coarse: 2,500 input tokens at $1.50 per million cost $0.00375 — under one
cent, which rounds to zero in an integer-cents column, and the system
silently bills nothing.

Pricing constants live in application config, not in the database, so that a
historical rollup cannot change because someone edited a row. Rates are
expressed as µUSD per 1,000,000 tokens:

| Metric | Rate (µUSD / 1M tokens) | Source |
| --- | --- | --- |
| `input_tokens` | 1_500_000 | Gemini 3.6 Flash standard input rate ($1.50 / 1M) |
| `cached_input_tokens` | 150_000 | Cached reads bill at 10% of the input rate |
| `output_tokens` | 7_500_000 | Gemini 3.6 Flash output rate ($7.50 / 1M) |
| `reasoning_tokens` | 7_500_000 | Billed at the output rate — not a separate free category |
| `api_calls` | 100 µUSD per call | My own flat rate ($0.0001 / call); no external source |

Rates mirror Gemini 3.6 Flash as published in September 2026. They are pinned
in config rather than fetched, so a historical rollup stays reproducible even
after the provider changes its prices.

**Rounding happens once.** Each category contributes `quantity × rate` to a
raw accumulator (units: µUSD × 10⁶); the sum is divided by 1,000,000 and
rounded only when the total is produced, using integer arithmetic throughout.
`round(raw / 1_000_000)` would route the value through a float and apply
banker's rounding on the way.

Worked example — 2,500 output tokens:
`2500 × 7_500_000 = 18_750_000_000` → `÷ 1_000_000` = **18,750 µUSD =
$0.01875**.

### 2.5 Cost is frozen on the event

`usage_events.cost_uusd` stores what each event was billed at, and the rollup
sums that column rather than repricing from current constants. Recomputing
would mean a closed period changes the day a rate does — an invoice issued
last month rewriting itself.

Added as a dedicated column rather than read out of `response_body`, where
the figure also appears: a billing record should not be coupled to the shape
of an HTTP response.

### 2.6 Plans

| Plan | API calls / month | AI tokens / month | Price |
| --- | --- | --- | --- |
| Free | 1,000 | 100,000 | — |
| Pro | 50,000 | 5,000,000 | $29 / month |

Pro limits are a deliberate choice, not a published figure: 50× Free, large
enough that the two tiers are visibly different in testing without making the
boundary probe slow to reach.

The $29 price is not calibrated against the plan's maximum cost. At the
pinned rates, 5,000,000 output tokens would cost $37.50, so a worst-case Pro
tenant is served at a loss. Modelling margin is out of scope; the figure
exists so Checkout has something to sell.

## 3. API surface

| Method | Path | In | Out | Errors |
| --- | --- | --- | --- | --- |
| `POST` | `/generate` | `X-Tenant-Id`, `Idempotency-Key`, token counts | usage recorded, cost, quota after this event | `400` · `402` · `404` · `422` · `429` |
| `GET` | `/usage` | `X-Tenant-Id` | used, limit, per-metric breakdown, cost for the period | `400` · `404` |
| `POST` | `/checkout` | `X-Tenant-Id` | Stripe Checkout URL | `400` · `404` · `502` |
| `POST` | `/webhooks/stripe` | raw body + `Stripe-Signature` | `200` | `400` bad signature |
| `GET` | `/health` | — | `{"status": "ok"}` | — |

Tenant identity travels in an `X-Tenant-Id` header. Real authentication is
out of scope: anyone who can reach the API can claim to be any tenant. Every
query is still scoped by tenant at the data layer, so adding auth later means
replacing where the id comes from, not rewriting the isolation.

### 3.1 The boundary rule

```text
current_usage + requested <= limit  ->  allowed
```

At 999 of 1,000, a request for 1 is allowed and leaves the tenant at exactly
1,000. At 1,000, a request for 1 is rejected. The quota is a ceiling that may
be reached but not crossed.

### 3.2 402 vs 429

Refined from the original plan, which tied 402 to a broken subscription only.
The brief describes 402 as "upgrade/payment required", and a Free tenant at
its limit has exactly that remedy.

- **402** — a payment action unblocks the caller: they are on Free and have
  exhausted it, or their subscription is `past_due`, `unpaid` or cancelled.
- **429** — they are on a paid, healthy plan and the period is spent. There
  is nothing to buy; the quota resets next month.

Mechanically, a subscription whose status is not `active` or `trialing`
drops the tenant to Free limits, which makes the 402 path fall out of the
same rule rather than needing a second one.

Every rejection body carries `limit_name`, `limit`, `used` and `requested`,
so a client can act on the refusal instead of guessing.

### 3.3 Byte-identical replays

The acceptance probe requires a retried request's response to mirror the
first. JSONB does not preserve key order, so a response read back from
storage came out reordered. Every response is recursively key-sorted before
it leaves the router, which makes both paths produce identical bytes rather
than merely equivalent JSON.

## 4. Idempotency and concurrency

**Key source.** Client-supplied `Idempotency-Key` header. A request without
one is rejected with `400` — generating one silently would defeat the
purpose, since a retry would arrive with a different key and be recorded as
new usage.

**Where the guarantee lives.** In the database:

```sql
UNIQUE (tenant_id, idempotency_key)
```

Writes use `INSERT ... ON CONFLICT DO NOTHING RETURNING id`. No row returned
means a duplicate. A `SELECT`-then-`INSERT` check would let two concurrent
retries both find nothing and both insert. `tenant_id` is part of the
constraint so two tenants can independently use the same key.

**What the retry returns.** The original response, stored as `response_body`
and replayed verbatim. Recomputing it would report a different `remaining` if
other usage landed in between.

**Same key, different body.** `request_hash` stores a SHA-256 of the
canonically serialized payload — `sort_keys` on the way in, so the same
metrics in a different JSON order still hash the same. A key reused with
genuinely different content returns `422`.

**Order of operations.** The replay lookup runs *before* the quota check. A
retry of a request that was already recorded must never be rejected: that
usage is already counted, and answering `429` to a retry would break
idempotency for any tenant sitting exactly at its limit.

**The quota race.** Quota is an aggregate, and an aggregate cannot be
protected by a constraint on a row that does not exist yet. Two concurrent
requests with different keys would both read the same total, both conclude
there is room, and both record. Metering reads the tenant with
`SELECT ... FOR UPDATE`, so quota checks for one tenant serialize while
different tenants never block each other.

Alternatives considered and rejected: `SERIALIZABLE` isolation, which is more
general but pushes retry logic into every caller; and tolerating a small
overshoot, which is what high-volume metering systems do and which
contradicts the point of this capstone.

## 5. Layers

```text
HTTP       FastAPI routers — validate, translate, return status codes
             ↓
Services   MeterService · QuotaService · CostCalculator · Rollup · Billing
             ↓
Data       repositories · migrations · Postgres
```

Routers never issue SQL; services never import from FastAPI. A quota
rejection carries a `remedy` ("upgrade" or "wait") and the router decides
which status code that deserves — the service has no opinion about HTTP.

### 5.1 The background job

`POST /webhooks/stripe` verifies the signature, records the event id, returns
`200`, and hands the work to an Inngest function. The plan update runs off
the request path.

This is not decoration. Applying a checkout event makes an API call back to
Stripe for the authoritative subscription status, and that call dominates the
run — roughly a second of the job's 1.2s. A second of network I/O inside the
webhook handler is a second Stripe spends waiting, and Stripe retries
deliveries that are slow.

Inngest over FastAPI's `BackgroundTasks` because the requirement asks for
retries and a failure alert: `BackgroundTasks` has neither, and loses the
work entirely if the process restarts.

### 5.2 Webhook trust and ordering

The signature is verified against the **raw request bytes**. Parsing to a
model and re-serializing changes whitespace and key order and makes
verification fail on legitimate payloads, so this is the one endpoint that
deliberately skips Pydantic.

Deduplication is the primary key on `stripe_event_id` plus the same
`ON CONFLICT DO NOTHING` used for metering. A duplicate still answers `200`:
an error response tells Stripe the delivery failed and it redelivers an event
already handled.

Stripe does not guarantee event order — a subscription change can arrive
before the checkout that created its metadata. Rather than reordering, such
an event fails and is retried with backoff; by the second attempt the other
event has usually landed.

The full event payload is stored, so a billing decision can be traced back to
the exact event it was made on, and a failed event can be reprocessed without
asking the customer to pay again.

### 5.3 Reading Stripe objects

The Stripe SDK returns resource objects that support `obj["key"]` but reject
`obj.get()`, while stored webhook payloads are plain dicts. Code that touches
both converts to a dict first rather than handling two shapes. The billing
period is read from the subscription and, failing that, from its items, since
the account's API version reports it on the item.

## 6. Non-goals

- **Authentication.** Tenant identity is an unverified header (§3).
- **Proration.** A mid-cycle upgrade takes effect immediately with no
  partial charge.
- **Invoicing and overage.** No monthly statements, no billing beyond the
  limit — requests over quota are rejected, not charged.
- **Quota/billing period alignment.** Quotas reset on the calendar month
  regardless of when the subscription period starts (§2.3).
- **Live mode.** Stripe test mode only, per the brief. The Stripe account is
  registered in the United States because Argentina is not a supported
  country for Stripe accounts; this has no effect on test mode, and the
  account is never activated for real payments.
