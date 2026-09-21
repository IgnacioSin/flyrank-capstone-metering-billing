"""Settings and pinned pricing constants. See DESIGN.md section 2.3.

Prices live here rather than in the database so that a historical rollup
cannot change because someone edited a row.
"""

import os

from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.environ["DATABASE_URL"]

# Stripe, test mode only. The brief is explicit: never switch to live.
STRIPE_SECRET_KEY = os.environ["STRIPE_SECRET_KEY"]
STRIPE_PRICE_ID_PRO = os.environ["STRIPE_PRICE_ID_PRO"]

# The Stripe CLI prints a fresh whsec_ every time `stripe listen` starts, so
# this one is read leniently and checked where it is used — a missing webhook
# secret should fail the webhook, not the whole application.
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")

APP_BASE_URL = os.environ.get("APP_BASE_URL", "http://localhost:8000")

# Micro-USD per 1,000,000 tokens. Mirrors Gemini 3.6 Flash as published in
# September 2026; pinned, not fetched.
TOKEN_RATES_UUSD_PER_MILLION: dict[str, int] = {
    "input_tokens": 1_500_000,
    "cached_input_tokens": 150_000,  # cached reads bill at 10% of input
    "output_tokens": 7_500_000,
    "reasoning_tokens": 7_500_000,  # billed as output, not a free category
}

# Micro-USD per API call. My own flat rate, no external source.
API_CALL_RATE_UUSD = 100

TOKEN_METRICS = tuple(TOKEN_RATES_UUSD_PER_MILLION)