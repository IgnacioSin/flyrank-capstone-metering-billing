# Evidence

One pasted proof per requirement in Section 6 of the capstone brief.
Transcripts are unedited except for line wrapping.

Requirements with no proof yet are listed as pending rather than omitted, so
the gap is visible.

---

## Metering

### A billable action creates exactly one usage event, even under retries

Same request, same `Idempotency-Key`, sent twice.

**First call — 201, a new event:**

```bash
$ curl -i -X POST http://localhost:8000/generate \
  -H "Content-Type: application/json" \
  -H "X-Tenant-Id: 37e4cb17-1ecc-4af4-8eaf-5e0a450902fd" \
  -H "Idempotency-Key: probe-1" \
  -d '{"input_tokens":1000,"cached_input_tokens":500,"output_tokens":2000,"reasoning_tokens":300}'

HTTP/1.1 201 Created
idempotent-replay: false
content-type: application/json

{"event_id":"ce21e140-aad8-4c61-a3cb-818e8eb608b6","metrics":{"api_calls":1,
"input_tokens":1000,"cached_input_tokens":500,"output_tokens":2000,
"reasoning_tokens":300},"cost_uusd":18925,"cost_usd":"$0.018925"}
```

**Second call — identical command, 200 and the stored response replayed:**

```bash
HTTP/1.1 200 OK
idempotent-replay: true
content-type: application/json

{"event_id":"ce21e140-aad8-4c61-a3cb-818e8eb608b6","metrics":{"api_calls":1,
"input_tokens":1000,"cached_input_tokens":500,"output_tokens":2000,
"reasoning_tokens":300},"cost_uusd":18925,"cost_usd":"$0.018925"}
```

Same `event_id`, same cost. The status code distinguishes the two: `201` for
the event that was created, `200` for the one that already existed.

### Proof that double-counting cannot happen

The database after both calls:

```bash
$ docker compose exec db psql -U billing -d billing \
  -c "SELECT count(*) FROM usage_events;" \
  -c "SELECT metric, quantity FROM usage_event_items;"

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
$ curl -i -X POST http://localhost:8000/generate \
  -H "Content-Type: application/json" \
  -H "X-Tenant-Id: 37e4cb17-1ecc-4af4-8eaf-5e0a450902fd" \
  -H "Idempotency-Key: probe-1" \
  -d '{"input_tokens":9999,"output_tokens":1}'

HTTP/1.1 422 Unprocessable Content

{"detail":{"error":"idempotency_key_reused","message":"This Idempotency-Key
was already used with a different request body."}}
```

Replaying the stored response here would answer a question the caller never
asked. The check is a SHA-256 of the canonically serialized payload stored as
`request_hash`.

---

## Cost calculation

### Pricing constants are pinned in config, with proof of correct totals

Constants live in `app/config.py`, not in the database, so a historical
rollup cannot change because someone edited a row. Rates are micro-USD per
1,000,000 tokens.

Manual verification of the `cost_uusd: 18925` returned above:

| Metric | Quantity | Rate | Contribution (µUSD) |
| --- | --- | --- | --- |
| `input_tokens` | 1,000 | 1,500,000 / 1M | 1,500 |
| `cached_input_tokens` | 500 | 150,000 / 1M | 75 |
| `output_tokens` | 2,000 | 7,500,000 / 1M | 15,000 |
| `reasoning_tokens` | 300 | 7,500,000 / 1M | 2,250 |
| `api_calls` | 1 | 100 µUSD / call | 100 |
| **Total** | | | **18,925** |

The two rules the brief calls out are both visible here: 500 cached input
tokens contribute 75 µUSD while 1,000 fresh input tokens contribute 1,500 —
cached reads bill at 10% of the input rate; and 300 reasoning tokens are
priced at the output rate, not as a free category.

Rounding happens once, on the summed raw accumulator, using integer
arithmetic only — see `app/services/cost.py`.

### Monthly usage rolls up into a cost figure per tenant

Pending — `GET /usage` lands with the rollup slice.

---

## Shared requirements

### #2 — Validation at the boundary: bad input yields a clean 4xx, never a 500

Request with no `Idempotency-Key` header:

```bash
$ curl -i -X POST http://localhost:8000/generate \
  -H "Content-Type: application/json" \
  -H "X-Tenant-Id: 37e4cb17-1ecc-4af4-8eaf-5e0a450902fd" \
  -d '{"input_tokens":1000,"cached_input_tokens":500,"output_tokens":2000,"reasoning_tokens":300}'

HTTP/1.1 400 Bad Request

{"error":"invalid_input","field":"idempotency-key","message":"Field required"}
```

No event is created. The handler in `app/main.py` rewrites FastAPI's default
422 to a 400 with a single error shape, which leaves 422 free to mean
"idempotency key reused" and nothing else.

### #5 — Idempotency where it matters

Covered by the metering section above.

### #1 — Layered architecture

`app/routers/` issues no SQL; `app/services/` imports nothing from FastAPI;
`app/repositories/` is the only layer that writes queries. See DESIGN.md
section 5.

### #4 — Real persistence: schema as migrations, right indexes, isolated tenants

Schema is managed by Alembic. Tenant isolation is enforced at the schema
level: `tenant_id` is part of the uniqueness constraint on `usage_events`, so
two tenants can independently use the same idempotency key without
colliding.

```bash
docker compose exec db psql -U billing -d billing -c "\d usage_events"
```

```bash
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

### #3 — At least one background job

Pending — the Stripe webhook handler, Phase 3.

### #6 — Secrets clean

`.env` is git-ignored from the first commit; `.env.example` ships
placeholders only. `alembic.ini` carries no database URL — it is read from
the environment in `alembic/env.py`.

### #7 — Cost tracked, if AI is used

Not applicable: token counts are simulated, no model is called.

---

## Quotas

Pending — quota enforcement slice.

## Stripe integration

Pending — Phase 3.
