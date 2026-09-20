# Evidence

One pasted proof per requirement in Section 6 of the capstone brief.
Transcripts are real terminal output, unedited except for line wrapping.

Requirements with no proof yet are listed as pending rather than omitted, so
the gap is visible.

All transcripts use the seeded demo tenant
`37e4cb17-1ecc-4af4-8eaf-5e0a450902fd`.

---

## Metering

### A billable action creates exactly one usage event, even under retries

Same request, same `Idempotency-Key`, sent twice. The bodies are written to
files and compared, because "the second response mirrors the first" is a
claim about bytes, not about looking similar.

```bash
KEY=normalize-check

curl -s -X POST http://localhost:8000/generate \
  -H "Content-Type: application/json" \
  -H "X-Tenant-Id: $TENANT" \
  -H "Idempotency-Key: $KEY" \
  -d '{"input_tokens":100,"output_tokens":200}' > /tmp/first.json

curl -s -X POST http://localhost:8000/generate \
  -H "Content-Type: application/json" \
  -H "X-Tenant-Id: $TENANT" \
  -H "Idempotency-Key: $KEY" \
  -d '{"input_tokens":100,"output_tokens":200}' > /tmp/second.json

diff /tmp/first.json /tmp/second.json && echo "IDENTICAL"
```

```text
IDENTICAL
```

`diff` printed nothing: the replayed body is byte-for-byte the original.
This needed a fix — JSONB does not preserve key order, so the stored
response came back with its keys shuffled. Every response now passes through
a recursive sort in `app/routers/generate.py` before it leaves the API.

The two calls are distinguishable by status code and header:

```text
First call:   HTTP/1.1 201 Created    idempotent-replay: false
Second call:  HTTP/1.1 200 OK         idempotent-replay: true
```

`201` for the event that was created, `200` for the one that already
existed. A replay is not a creation.

### Proof that double-counting cannot happen

The database after a request and its retry:

```bash
docker compose exec db psql -U billing -d billing \
  -c "SELECT count(*) FROM usage_events;" \
  -c "SELECT metric, quantity FROM usage_event_items;"
```

```text
 count
-------
     1
(1 row)

       metric        | quantity
---------------------+----------
 api_calls           |        1
 input_tokens        |     1000
 cached_input_tokens |      500
 output_tokens       |     2000
 reasoning_tokens    |      300
(5 rows)
```

One event row, five metric rows — the second request wrote nothing.

The guarantee is `UNIQUE (tenant_id, idempotency_key)` plus
`INSERT ... ON CONFLICT DO NOTHING RETURNING id` in
`app/repositories/usage.py`. A `SELECT`-then-`INSERT` would let two
concurrent retries both find nothing and both insert; here Postgres
arbitrates and exactly one wins.

### A reused key with a different body is rejected, not replayed

Same `Idempotency-Key`, different token counts:

```bash
curl -i -X POST http://localhost:8000/generate \
  -H "Content-Type: application/json" \
  -H "X-Tenant-Id: $TENANT" \
  -H "Idempotency-Key: probe-1" \
  -d '{"input_tokens":9999,"output_tokens":1}'
```

```text
HTTP/1.1 422 Unprocessable Content

{"detail":{"error":"idempotency_key_reused","message":"This Idempotency-Key
was already used with a different request body."}}
```

Replaying the stored response here would answer a question the caller never
asked. The check compares a SHA-256 of the canonically serialized payload,
stored as `request_hash`. `sort_keys` is used on the way in, so the same
metrics in a different JSON order still hash the same and a legitimate retry
is never mistaken for key reuse.

---

## Quotas

### Usage is checked against the tenant's plan; requests over the limit are rejected

Free plan: 1,000 API calls and 100,000 tokens per month. Three requests of
40,000 output tokens each, on top of 3,800 tokens already recorded:

```bash
for i in 1 2 3; do
  echo "--- request $i ---"
  curl -s -o /dev/null -w "%{http_code}\n" -X POST http://localhost:8000/generate \
    -H "Content-Type: application/json" \
    -H "X-Tenant-Id: $TENANT" \
    -H "Idempotency-Key: quota-$i" \
    -d '{"output_tokens":40000}'
done
```

```text
--- request 1 ---
201
--- request 2 ---
201
--- request 3 ---
402
```

3,800 + 40,000 + 40,000 = 83,800. The third request would reach 123,800 and
is refused.

### The request at the boundary behaves per the documented rule

The rule, from DESIGN.md section 3.1:

```text
current_usage + requested <= limit  ->  allowed
```

At 83,800 used, a request for exactly the remaining 16,200:

```bash
curl -i -X POST http://localhost:8000/generate \
  -H "Content-Type: application/json" \
  -H "X-Tenant-Id: $TENANT" \
  -H "Idempotency-Key: boundary-exact" \
  -d '{"output_tokens":16200}'
```

```text
HTTP/1.1 201 Created
idempotent-replay: false

{"cost_usd":"$0.121600","cost_uusd":121600,
"event_id":"f19c65f8-c53c-414f-b591-f2999c6d5bd3",
"metrics":{"api_calls":1,"input_tokens":0,"cached_input_tokens":0,
"output_tokens":16200,"reasoning_tokens":0},
"quota":{"api_calls":{"used":4,"limit":1000,"remaining":996},
"tokens":{"used":100000,"limit":100000,"remaining":0}}}
```

Allowed, landing on exactly 100,000 with `remaining: 0`. The quota is a
ceiling that may be reached but not crossed.

### A retry at the exact limit is replayed, not rejected

Same command again, with the tenant now sitting at 100,000 of 100,000:

```text
HTTP/1.1 200 OK
idempotent-replay: true
```

This is the case that separates a correct implementation from one that was
adjusted until the tests passed. That usage is already counted; answering
`402` to a retry would break idempotency for every tenant at its limit. The
service looks up the idempotency key *before* it checks quota, and the
ordering is the reason — see the docstring on `record()` in
`app/services/meter.py`.

### Responses carry the correct status codes and a message explaining why

**402 — the tenant is on Free, and a payment action unblocks it:**

```bash
curl -i -X POST http://localhost:8000/generate \
  -H "Content-Type: application/json" \
  -H "X-Tenant-Id: $TENANT" \
  -H "Idempotency-Key: boundary-over" \
  -d '{"output_tokens":1}'
```

```text
HTTP/1.1 402 Payment Required

{"detail":{"error":"quota_exceeded","limit_name":"tokens","limit":100000,
"used":100000,"requested":1,"message":"This request needs 1 tokens but only
0 remain of 100000 this period. Upgrade to Pro for a higher limit."}}
```

**429 — the tenant is on a paid plan and the period is spent:**

Note: Pro's real token limit is 5,000,000, which would take 125 requests to
exhaust. The limit was temporarily lowered to 90,000 to make this path
observable, then restored by re-running the seed.

```bash
docker compose exec db psql -U billing -d billing \
  -c "UPDATE tenants SET plan_code='pro' WHERE id='$TENANT';" \
  -c "UPDATE plans SET token_limit=90000 WHERE code='pro';"

curl -i -X POST http://localhost:8000/generate \
  -H "Content-Type: application/json" \
  -H "X-Tenant-Id: $TENANT" \
  -H "Idempotency-Key: paid-over" \
  -d '{"output_tokens":1}'
```

```text
HTTP/1.1 429 Too Many Requests

{"detail":{"error":"quota_exceeded","limit_name":"tokens","limit":90000,
"used":100000,"requested":1,"message":"This request needs 1 tokens but only
0 remain of 90000 this period. The quota resets at the start of next
month."}}
```

Same rejection, different remedy. `402` means a payment unblocks the caller;
`429` means there is nothing to buy and the quota resets next month. Both
bodies carry `used`, `limit` and `requested`, so a client can act on the
refusal instead of guessing.

### Concurrency at the boundary

Quota is an aggregate, and an aggregate cannot be protected by a constraint
on a row that does not exist yet. Two concurrent requests with different
idempotency keys would both read the same total, both conclude there is
room, and both record.

`app/repositories/tenants.py` takes a `SELECT ... FOR UPDATE` row lock on the
tenant before reading usage, so quota checks for one tenant serialize while
different tenants never block each other. Alternatives considered:
`SERIALIZABLE` isolation with retry logic, and tolerating a small overshoot
the way high-volume metering systems do. Both are recorded in DESIGN.md.

---

## Cost calculation

### Pricing constants are pinned in config, with proof of correct totals

Constants live in `app/config.py`, not in the database, so a historical
rollup cannot change because someone edited a row. Rates are micro-USD per
1,000,000 tokens.

Manual verification of a request with 1,000 input, 500 cached input, 2,000
output and 300 reasoning tokens, which returned `cost_uusd: 18925`:

| Metric | Quantity | Rate | Contribution (µUSD) |
| --- | --- | --- | --- |
| `input_tokens` | 1,000 | 1,500,000 / 1M | 1,500 |
| `cached_input_tokens` | 500 | 150,000 / 1M | 75 |
| `output_tokens` | 2,000 | 7,500,000 / 1M | 15,000 |
| `reasoning_tokens` | 300 | 7,500,000 / 1M | 2,250 |
| `api_calls` | 1 | 100 µUSD / call | 100 |
| **Total** | | | **18,925** |

Both rules the brief calls out are visible: 500 cached input tokens
contribute 75 µUSD while 1,000 fresh input tokens contribute 1,500 — cached
reads bill at 10% of the input rate; and 300 reasoning tokens are priced at
the output rate, not as a free category. The four categories are never
summed into a single quantity before pricing.

Rounding happens once, on the summed raw accumulator, using integer
arithmetic only. `round(raw / 1_000_000)` would route the value through a
float on the way — see `app/services/cost.py`.

### Monthly usage rolls up into a cost figure per tenant

Pending — `GET /usage`.

---

## Stripe integration

Pending — Phase 3.

- Subscription checkout in test mode
- Webhook signature verification
- Duplicate event rejection
- Plan/status synchronization

---

## Shared requirements

### #1 — Layered architecture: data / logic / HTTP separated

`app/routers/` issues no SQL. `app/services/` imports nothing from FastAPI —
a quota rejection carries a `remedy` ("upgrade" or "wait") and the router
decides which status code that deserves. `app/repositories/` is the only
layer that writes queries. See DESIGN.md section 5.

### #2 — Validation at the boundary: bad input yields a clean 4xx, never a 500

Request with no `Idempotency-Key` header:

```bash
curl -i -X POST http://localhost:8000/generate \
  -H "Content-Type: application/json" \
  -H "X-Tenant-Id: $TENANT" \
  -d '{"input_tokens":1000,"cached_input_tokens":500,"output_tokens":2000,"reasoning_tokens":300}'
```

```text
HTTP/1.1 400 Bad Request

{"error":"invalid_input","field":"idempotency-key","message":"Field required"}
```

No event is created. The handler in `app/main.py` rewrites FastAPI's default
422 into a 400 with a single error shape, which leaves 422 free to mean
"idempotency key reused" and nothing else.

Negative token counts are rejected twice over: `ge=0` in the Pydantic model
at the edge, and a `CHECK (quantity >= 0)` constraint in the database as the
last line of defence.

### #3 — At least one background job

Pending — the Stripe webhook handler, Phase 3. The handler will verify the
signature, record the event id and return `200` immediately, with the
plan/status update running off the request path. Stripe retries deliveries
that are slow, so doing the work inline turns a slow write into duplicate
deliveries.

### #4 — Real persistence: schema as migrations, right indexes, isolated tenants

Schema is managed by Alembic. Tenant isolation is enforced at the schema
level: `tenant_id` is part of the uniqueness constraint on `usage_events`, so
two tenants can independently use the same idempotency key without
colliding.

```bash
docker compose exec db psql -U billing -d billing -c "\d usage_events"
```

```text
                          Table "public.usage_events"
     Column      |           Type           | Nullable |  Default
-----------------+--------------------------+----------+-----------
 id              | uuid                     | not null |
 tenant_id       | uuid                     | not null |
 idempotency_key | character varying(255)   | not null |
 kind            | character varying(32)    | not null |
 request_hash    | character varying(64)    | not null |
 response_body   | jsonb                    | not null |
 created_at      | timestamp with time zone | not null | now()
Indexes:
    "usage_events_pkey" PRIMARY KEY, btree (id)
    "ix_usage_events_tenant_created" btree (tenant_id, created_at)
    "uq_usage_events_tenant_key" UNIQUE CONSTRAINT, btree (tenant_id, idempotency_key)
Check constraints:
    "ck_usage_events_kind" CHECK (kind::text = ANY (ARRAY['api_call'::character varying, 'ai_tokens'::character varying]::text[]))
Foreign-key constraints:
    "usage_events_tenant_id_fkey" FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE
Referenced by:
    TABLE "usage_event_items" CONSTRAINT "usage_event_items_event_id_fkey" FOREIGN KEY (event_id) REFERENCES usage_events(id) ON DELETE CASCADE
```

Three things in that output are load-bearing:

- `uq_usage_events_tenant_key` is the exactly-once guarantee itself, not a
  validation. Both columns are in it, so tenants are isolated by
  construction rather than by application code remembering to filter.
- `ix_usage_events_tenant_created` matches the only question the rollup ever
  asks — this tenant, this period. Without it every `GET /usage` scans the
  table.
- `ON DELETE CASCADE` on both foreign keys means a removed tenant cannot
  leave orphaned events, and a removed event cannot leave orphaned metric
  rows that would still be summed.

### #5 — Idempotency where it matters

Covered by the metering section above.

### #6 — Secrets clean

`.env` is git-ignored from the first commit; `.env.example` ships
placeholder values only. `alembic.ini` carries no database URL — it is read
from the environment in `alembic/env.py`. No secret is logged.

### #7 — Cost tracked, if AI is used

Not applicable: token counts are simulated and no model is called. Cost
attribution per request exists anyway — every response carries `cost_uusd`
for that call, stored on the event row.
