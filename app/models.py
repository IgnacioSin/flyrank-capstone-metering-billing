"""Database schema. See DESIGN.md section 2.

Subscriptions and webhook deduplication arrive in Phase 3 as a second
migration — this file covers metering and quotas only.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Tenant(Base):
    """One customer organization. Every usage event belongs to exactly one."""

    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    plan_code: Mapped[str] = mapped_column(
        ForeignKey("plans.code"), nullable=False, server_default="free"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Plan(Base):
    """Quotas only. Prices live in config — see DESIGN.md section 2.3."""

    __tablename__ = "plans"

    code: Mapped[str] = mapped_column(String(32), primary_key=True)
    api_call_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    token_limit: Mapped[int] = mapped_column(BigInteger, nullable=False)


class UsageEvent(Base):
    """One row per billable *request*.

    The unique constraint on (tenant_id, idempotency_key) is not a validation
    — it is the exactly-once guarantee itself. Writes go through
    INSERT ... ON CONFLICT DO NOTHING, so two concurrent retries cannot both
    land a row.
    """

    __tablename__ = "usage_events"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)

    # Hash of the request payload. A key reused with a different body is a
    # client bug, not a retry, and must not replay an unrelated response.
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    # The original response, replayed verbatim on a duplicate. Recomputing it
    # would report a different `remaining` if other usage landed in between.
    response_body: Mapped[dict] = mapped_column(JSONB, nullable=False)

    # The cost this event was billed at, frozen. Recomputing a rollup from
    # current rates would rewrite the price of a month that already closed.
    cost_uusd: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0"
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    items: Mapped[list["UsageEventItem"]] = relationship(
        back_populates="event", cascade="all, delete-orphan", lazy="selectin"
    )

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "idempotency_key", name="uq_usage_events_tenant_key"
        ),
        CheckConstraint(
            "kind IN ('api_call', 'ai_tokens')", name="ck_usage_events_kind"
        ),
        # The rollup always asks "this tenant, this period".
        Index("ix_usage_events_tenant_created", "tenant_id", "created_at"),
    )


class UsageEventItem(Base):
    """One metric within an event.

    Token usage is four numbers that price differently and cannot be summed
    together, so quantities live in child rows while the idempotency key stays
    on the parent — the unit being retried is the request, not the metric.
    """

    __tablename__ = "usage_event_items"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("usage_events.id", ondelete="CASCADE"), nullable=False
    )
    metric: Mapped[str] = mapped_column(String(32), nullable=False)
    quantity: Mapped[int] = mapped_column(BigInteger, nullable=False)

    event: Mapped["UsageEvent"] = relationship(back_populates="items")

    __table_args__ = (
        # An event cannot carry two rows for the same metric — which one would
        # the rollup count?
        UniqueConstraint("event_id", "metric", name="uq_usage_event_items_metric"),
        # A negative quantity would be a credit. Not in scope, and it would
        # silently subtract from quota usage.
        CheckConstraint("quantity >= 0", name="ck_usage_event_items_nonneg"),
        CheckConstraint(
            "metric IN ('api_calls', 'input_tokens', 'cached_input_tokens', "
            "'output_tokens', 'reasoning_tokens')",
            name="ck_usage_event_items_metric",
        ),
    )