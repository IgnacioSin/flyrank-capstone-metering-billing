"""Application entrypoint."""

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

import inngest.fast_api

from app.jobs import apply_stripe_event, inngest_client
from app.routers import checkout, generate, usage, webhooks

app = FastAPI(title="Usage Metering & Billing Engine")

app.include_router(generate.router)
app.include_router(usage.router)
app.include_router(checkout.router)
app.include_router(webhooks.router)

# Declares the function at /api/inngest so the Dev Server can discover and
# invoke it. A function missing from this list does not exist for Inngest.
inngest.fast_api.serve(app, inngest_client, [apply_stripe_event])


@app.exception_handler(RequestValidationError)
async def validation_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Bad input is a clean 400, never a 500.

    FastAPI answers 422 by default. Rewriting to 400 keeps a single error
    shape across the API — and 422 stays reserved for a reused idempotency
    key, which is a different failure entirely.
    """
    first = exc.errors()[0]
    field = ".".join(str(part) for part in first["loc"][1:]) or "body"
    return JSONResponse(
        status_code=400,
        content={
            "error": "invalid_input",
            "field": field,
            "message": first["msg"],
        },
    )


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}