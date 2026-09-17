# Design — Usage Metering & Billing Engine

FlyRank Internship · Backend Track · Capstone
Author: Juan Ignacio Sin

## 1. The problem

A multi-tenant SaaS backend that answers three questions: how much has a tenant used
this period, what does that usage cost, and is the tenant allowed to do this next
action. Subscription state is synchronized from Stripe (test mode) through
signature-verified webhooks.

The system must stay correct under conditions that break naive implementations: a
network retry that must not double-charge, a webhook delivered twice, two concurrent
requests racing at the exact quota boundary. Correctness here is not a nice-to-have —
a bug either overcharges a customer or gives away service for free.

## 2. Data model

Six tables. Postgres, schema managed as migrations.

| Table | Columns (essential) | Notes |
| --- | --- | --- |
| `tenants` | `id`, `name`, `created_at` | One customer organization. |
| `plans` | `code`, `api_call_limit`, `token_limit` | Quotas only — no prices (see §2.3). |
| `subscriptions` | `tenant_id`, `plan_code`, `status`, `stripe_customer_id`, `stripe_subscription_id`, `current_period_start`, `current_period_end` | Mirror of Stripe. Written only by verified webhooks. |
| `usage_events` | `id`, `tenant_id`, `idempotency_key`, `kind`, `request_hash`, `response_body`, `created_at` | One row per billable *request*. |
| `usage_event_items` | `event_id`, `metric`, `quantity` | One row per metric within an event. |
| `processed_webhook_events` | `stripe_event_id` (PK), `processed_at` | Webhook deduplication. |

### 2.1 Why parent/child for usage events

An API call is one number. A token event is four numbers that cannot be summed
together (input, cached input, output, reasoning). Splitting them into separate
`usage_events` rows would break idempotency: the unit that must be deduplicated is
the *request*, not the metric. With a parent row carrying the idempotency key and
child rows carrying quantities, the uniqueness constraint sits exactly on the thing
being retried.

### 2.2 Quota window

Quotas reset on the **calendar month**. The rollup filter is
`created_at >= date_trunc('month', now())`.

This deliberately does not match Stripe's billing period: a tenant who subscribes on
the 15th is billed 15th-to-15th while their quota resets on the 1st. Aligning the two
is listed as a non-goal (§6).

### 2.3 Money

All money is stored as **integer micro-USD** (1 µUSD = 10⁻⁶ USD). Cents are too
coarse: 2,500 input tokens at $1.50 per million cost $0.00375 — under one cent, which
rounds to zero in an integer-cents column, and the system silently bills nothing.

Pricing constants live in application config, not in the database, so that a
historical rollup cannot change because someone edited a row. Rates are expressed as
µUSD per 1,000,000 tokens:

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

**Rounding happens once.** Each category contributes `quantity × rate` to a raw
accumulator (units: µUSD × 10⁶); the sum is divided by 1,000,000 and rounded only
when the total is produced. Rounding per event would accumulate error across
thousands of rows, always in the direction that loses revenue.

Worked example — 2,500 output tokens:
`2500 × 7_500_000 = 18_750_000_000` → `÷ 1_000_000` = **18,750 µUSD = $0.01875**.

### 2.4 Plans

| Plan | API calls / month | AI tokens / month |
| --- | --- | --- |
| Free | 1,000 | 100,000 |
| Pro | 50,000 | 5,000,000 |

Pro limits are a deliberate choice, not a published figure: 50× Free, large
enough that the two tiers are visibly different in testing without making the
boundary probe slow to reach.

## 3. API surface

| Method | Path | In | Out | Errors |
| --- | --- | --- | --- | --- |
| `POST` | `/generate` | `Idempotency-Key` header, token counts in body | usage recorded + cost | `400` bad input · `402` · `429` |
| `GET` | `/usage` | — | `{used, limit, cost}` for the current period | `404` unknown tenant |
| `POST` | `/checkout` | target plan | Stripe Checkout URL | `400` |
| `POST` | `/webhooks/stripe` | raw body + `Stripe-Signature` | `200` | `400` bad signature |
| `GET` | `/health` | — | `{"status": "ok"}` | — |

### 3.1 The boundary rule

```text
current_usage + requested <= limit  →  allowed
```

At 999 of 1,000, a request for 1 is allowed and leaves the tenant at exactly 1,000.
At 1,000, a request for 1 is rejected. The quota is a ceiling that may be reached but
not crossed.

### 3.2 402 vs 429

- **429** — the subscription is healthy; the period's allowance is exhausted.
  Retrying next period will work.
- **402** — the subscription itself is the problem: `past_due`, cancelled, or the
  plan does not cover the action. Retrying will never work until payment changes.

Every rejection body names which rule fired and the numbers behind it, so a human
debugging the integration does not have to guess.

## 4. Idempotency strategy

**Key source.** Client-supplied `Idempotency-Key` header. A request without one is
rejected with `400` — silently generating a key would defeat the purpose, since a
retry would arrive with a different key and be recorded as new usage.

**Where the guarantee lives.** In the database:

```sql
UNIQUE (tenant_id, idempotency_key)
```

Writes use `INSERT ... ON CONFLICT DO NOTHING RETURNING id`. No row returned means a
duplicate. A `SELECT`-then-`INSERT` check would not be enough: two concurrent retries
both read, both find nothing, both insert. Postgres resolves the race; the
application never sees it. `tenant_id` is part of the constraint so two tenants can
independently use the same key.

**What the retry returns.** The original response, stored as `response_body` on the
event row and replayed verbatim. Recomputing it would produce a different `remaining`
if other usage landed in between, and the acceptance probe requires the second
response to mirror the first.

**Same key, different body.** `request_hash` stores a hash of the request payload. A
key reused with a different body returns `422` rather than silently replaying an
unrelated response.

## 5. Layers

```text
HTTP       FastAPI routers — validate, translate, return status codes
             ↓
Services   MeterService · QuotaService · CostCalculator · BillingService
             ↓
Data       repositories · migrations · Postgres
```

Routers never issue SQL; services never import from FastAPI. A service raising
`HTTPException` is the signal that the boundary leaked.

**Background job.** `POST /webhooks/stripe` verifies the signature, records the event
id, and returns `200` immediately; the plan/status update runs in a background job.
Stripe retries deliveries that are slow or fail, so doing the work inline turns a slow
database write into duplicate deliveries.

## 6. Non-goals

Explicitly out of scope for the core build:

- **Proration.** A mid-cycle upgrade takes effect immediately with no partial charge.
- **Invoicing and overage.** No monthly statements, no billing beyond the limit —
  requests over quota are rejected, not charged.
- **Quota/billing period alignment.** Quotas reset on the calendar month regardless
  of when the subscription period starts (§2.2).
