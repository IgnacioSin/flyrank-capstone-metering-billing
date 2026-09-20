"""Request/response shapes. Validation happens here, at the boundary."""

from pydantic import BaseModel, Field, model_validator


class GenerateRequest(BaseModel):
    """Simulated token counts for one AI call.

    ge=0 rejects negatives before they reach the database. The CHECK
    constraint on usage_event_items catches them too — that one is the last
    line of defence, this one is the first.
    """

    input_tokens: int = Field(default=0, ge=0)
    cached_input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    reasoning_tokens: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def at_least_one_token(self) -> "GenerateRequest":
        if not any(
            (
                self.input_tokens,
                self.cached_input_tokens,
                self.output_tokens,
                self.reasoning_tokens,
            )
        ):
            raise ValueError("at least one token count must be greater than zero")
        return self

    def as_metrics(self) -> dict[str, int]:
        """Metric quantities for this request, including the API call itself."""
        return {
            "api_calls": 1,
            "input_tokens": self.input_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "output_tokens": self.output_tokens,
            "reasoning_tokens": self.reasoning_tokens,
        }