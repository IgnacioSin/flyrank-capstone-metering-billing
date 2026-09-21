# Build log

Where AI helped, where it was wrong, and what I changed. Honesty is graded,
perfection is not.

I used Claude throughout as a pair, not as a code generator I copied from
blindly. The pattern that worked: I described the problem and the
constraints, it proposed a design or an implementation, and I pushed back or
verified before accepting. The pattern that wasted time is recorded below
too.

---

## Phase 1 — Design

`DESIGN.md` was drafted by Claude from the capstone brief and my scope
decisions. The structure and prose are its work.

Decisions I did not make myself: the token pricing rates came from Gemini's
published pricing, which Claude looked up; the Pro plan limits (50,000 calls
/ 5,000,000 tokens) were Claude's suggestion and I accepted them. The Free
limits come from the brief.

Things I reviewed and can explain without rereading: the parent/child shape
of `usage_events`, why the idempotency guarantee has to live in a database
constraint rather than in application code, and why money is stored in
micro-USD with a single rounding step.

---

## Phase 2 — Metering and quotas

### What Claude got right in Stripe integration

The `INSERT ... ON CONFLICT DO NOTHING RETURNING id` pattern, and the
explanation of why a `SELECT`-then-`INSERT` check is not equivalent. That
distinction is the core of the capstone and I would not have arrived at it
on my own.

The ordering inside `record()`: look up the idempotency key *before*
checking quota, so a retry from a tenant sitting at its limit is replayed
instead of rejected. I would have written the quota check first, which
passes every obvious test and breaks the one case that matters.

### What Claude got wrong

**Duplicate UUID generation.** The first version of `meter.py` generated an
event id in the service and `usage.py` generated another one in the
repository. The response stored in the database carried one id and the
response returned to the caller carried the other, so a replayed request
would have reported an `event_id` that did not exist. Claude caught this
while writing the second file and fixed it by passing the id in, but the
first version was wrong.

**A redundant index.** `models.py` declared an index on
`usage_event_items.event_id` on top of the `UNIQUE (event_id, metric)`
constraint, which already covers lookups on the leading column. Extra writes
on every insert for no read benefit. Removed from both the model and the
generated migration.

**Key ordering in replayed responses.** Claude's first fix was
`dict(sorted(...))`, which sorts only the top level. My own test output
showed the nested `metrics` and `quota` objects still coming back in a
different order, because JSONB does not preserve insertion order. The
working fix is a recursive sort. The lesson: the acceptance probe says the
replay "mirrors" the original, and I verified that with `diff` on two saved
bodies rather than by eyeballing two blobs of JSON.

**A bad arithmetic example.** When walking me through the quota boundary
test, Claude computed the remaining allowance without counting usage already
in the database, so its predicted status codes did not match what I got. The
system was right and the explanation was wrong.

---

## Phase 3 — Stripe

### What Claude got right

The insistence that `POST /checkout` must not touch the database. My instinct
was to flip the plan there; the payment has not happened yet at that point,
and the plan changes because a signed webhook says so.

Sending the tenant id in `subscription_data.metadata` as well as
`client_reference_id`. Later `customer.subscription.*` events carry the
subscription, not the session, so without it those events arrive with no way
to tell whose they are.

Verifying the signature against the raw request bytes. This is the one
endpoint that skips Pydantic, deliberately.

### Where the debugging went wrong — the useful part

The Stripe webhook job failed three times and the plan stayed on Free. What
happened next is the most instructive thing in this build.

I asked another AI assistant to diagnose it from the source. It produced a
confident, specific answer: two bugs, one in the `on_failure` key path and
one where `obj["subscription"]` supposedly raised `KeyError` under a newer
API version.

Claude disagreed with the first claim and showed why from evidence I already
had: my database query showed the event at status `failed`, and only
`on_failure` writes that status, so `on_failure` had clearly run without
error. The double-nesting in `ctx.event.data["event"]["data"]` is correct —
the failure handler receives an `inngest/function.failed` event that wraps
the original, which is the same structure I used in A7.

Claude was also unsure about the second claim, and said so.

Both of us then proposed a defensive fix for the billing period, which Stripe
moved onto subscription items. It did not fix anything.

**The actual error, once I read the traceback:**

```text
AttributeError: 'get' is a dict method, but a Subscription is not a dict.
Use .to_dict() to convert it.
```

`_billing_period()` was called with a plain dict from a stored webhook
payload in one path and with a Stripe resource object from `retrieve()` in
the other. The two look alike; the Stripe object supports `obj["key"]` and
rejects `obj.get()`.

Neither hypothesis was close. Claude had asked me for the traceback four
times before I went and got it, and it had the answer in one line.

**What I take from this:** a confident explanation from an AI that has read
your source is still a hypothesis. The runtime knows. I now go to the logs
first and ask second — which is exactly the habit A7 was teaching with its
Inngest dashboard, and I did not transfer it until it cost me an hour.

### What I changed myself

The `hasattr(obj, "to_dict")` guard is a patch, not a fix. The real problem
is a function that accepts two different types depending on who calls it.
Converting at the boundary — immediately after `retrieve()` — so everything
inside `billing.py` is a plain dict is the cleaner shape, and it is on my
list.

---

## Tools

- Claude (this build log, `DESIGN.md`, and most implementation code)
- A second AI assistant, used once for diagnosis, with the result above
- Stripe CLI for local webhook delivery
- Inngest Dev Server for the background job and its dashboard
