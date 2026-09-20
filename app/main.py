"""Application entrypoint."""

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.routers import generate, usage

app = FastAPI(title="Usage Metering & Billing Engine")

app.include_router(generate.router)
app.include_router(usage.router)


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