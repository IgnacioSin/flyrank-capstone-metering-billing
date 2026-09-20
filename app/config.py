"""Settings and pinned pricing constants. See DESIGN.md section 2.3.

Prices live here rather than in the database so that a historical rollup
cannot change because someone edited a row.
"""

import os

from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.environ["DATABASE_URL"]

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