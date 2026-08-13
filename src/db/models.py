"""SQLAlchemy ORM models for the quant trading system.

Every entity is modeled as a table. This module defines the schema;
migrations are managed via Alembic.

Key design decisions:
- UUIDs for primary keys (no sequential ID leakage).
- JSONB for flexible metadata/extras.
- Timestamps always UTC.
- Immutable audit log events are append-only.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import Enum as PyEnum
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    """Return current UTC datetime."""
    return datetime.now(UTC)


def new_uuid() -> str:
    """Generate a new UUID4 as string."""
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    """Base for all ORM models."""


# --- Enums ---


class OrderSide(str, PyEnum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, PyEnum):
    MARKET = "market"
    LIMIT = "limit"


class OrderState(str, PyEnum):
    PENDING = "pending"
    OPEN = "open"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"
    EXPIRED = "expired"


class PositionSide(str, PyEnum):
    LONG = "long"
    SHORT = "short"


class SignalDirection(str, PyEnum):
    LONG = "long"
    SHORT = "short"
    NEUTRAL = "neutral"


class OpportunityStatus(str, PyEnum):
    DETECTED = "detected"
    RANKED = "ranked"
    APPROVED = "approved"  # passed risk
    REJECTED = "rejected"  # failed risk
    EXECUTING = "executing"
    EXECUTED = "executed"
    EXPIRED = "expired"


class RejectionReason(str, PyEnum):
    RISK_LIMIT = "risk_limit"
    CIRCUIT_BREAKER = "circuit_breaker"
    KILL_SWITCH = "kill_switch"
    INSUFFICIENT_CAPITAL = "insufficient_capital"
    LIQUIDITY = "liquidity"
    VOLATILITY = "volatility"
    CORRELATION = "correlation"
    STALE_DATA = "stale_data"
    EXPIRED_SIGNAL = "expired_signal"
    STRATEGY_DISABLED = "strategy_disabled"
    INSUFFICIENT_EXPECTED_EDGE = "insufficient_expected_edge"
    INSUFFICIENT_CONFIDENCE = "insufficient_confidence"
    LOW_SCORE = "low_score"
    EXCHANGE_ERROR = "exchange_error"
    OTHER = "other"


class Mode(str, PyEnum):
    PAPER = "paper"
    LIVE = "live"


# --- Models ---


class Exchange(Base):
    """Registered exchange configuration."""

    __tablename__ = "exchanges"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=new_uuid)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    full_name: Mapped[str] = mapped_column(String(128), nullable=False)
    market_type: Mapped[str] = mapped_column(String(32), nullable=False)  # "crypto", "forex", etc.
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    rest_base_url: Mapped[str | None] = mapped_column(String(256))
    ws_base_url: Mapped[str | None] = mapped_column(String(256))
    rate_limit_rps: Mapped[float | None] = mapped_column(Float)
    metadata_: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    instruments: Mapped[list[Instrument]] = relationship(back_populates="exchange")


class Instrument(Base):
    """Normalized instrument across all exchanges."""

    __tablename__ = "instruments"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=new_uuid)
    exchange_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("exchanges.id"), nullable=False
    )
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    base_asset: Mapped[str] = mapped_column(String(16), nullable=False)
    quote_asset: Mapped[str] = mapped_column(String(16), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    min_order_size: Mapped[float | None] = mapped_column(Float)
    price_precision: Mapped[int | None] = mapped_column(Integer)
    quantity_precision: Mapped[int | None] = mapped_column(Integer)
    maker_fee: Mapped[float] = mapped_column(Float, default=0.0)
    taker_fee: Mapped[float] = mapped_column(Float, default=0.0)
    metadata_: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    exchange: Mapped[Exchange] = relationship(back_populates="instruments")

    __table_args__ = (
        UniqueConstraint("exchange_id", "symbol", name="uq_exchange_symbol"),
        Index("ix_instruments_symbol", "symbol"),
    )


class Signal(Base):
    """Raw signal produced by a strategy plugin."""

    __tablename__ = "signals"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=new_uuid)
    strategy_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    exchange_id: Mapped[str | None] = mapped_column(UUID(as_uuid=False))
    instrument_id: Mapped[str | None] = mapped_column(UUID(as_uuid=False))
    market: Mapped[str | None] = mapped_column(String(32))
    symbol: Mapped[str | None] = mapped_column(String(32))
    direction: Mapped[SignalDirection] = mapped_column(Enum(SignalDirection), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    estimated_return: Mapped[float | None] = mapped_column(Float)
    estimated_risk: Mapped[float | None] = mapped_column(Float)
    required_capital: Mapped[float | None] = mapped_column(Float)
    entry_logic: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    exit_logic: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    signal_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    metadata_: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )

    __table_args__ = (Index("ix_signals_strategy_created", "strategy_id", "created_at"),)


class Opportunity(Base):
    """Ranked and evaluated opportunity produced by the Opportunity Engine."""

    __tablename__ = "opportunities"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=new_uuid)
    signal_id: Mapped[str | None] = mapped_column(UUID(as_uuid=False), ForeignKey("signals.id"))
    strategy_id: Mapped[str] = mapped_column(String(64), nullable=False)
    exchange_id: Mapped[str | None] = mapped_column(UUID(as_uuid=False))
    instrument_id: Mapped[str | None] = mapped_column(UUID(as_uuid=False))
    symbol: Mapped[str | None] = mapped_column(String(32))
    direction: Mapped[SignalDirection] = mapped_column(Enum(SignalDirection), nullable=False)
    status: Mapped[OpportunityStatus] = mapped_column(
        Enum(OpportunityStatus), default=OpportunityStatus.DETECTED, nullable=False
    )

    # Scoring
    opportunity_score: Mapped[float | None] = mapped_column(Float)
    expected_gross_return: Mapped[float | None] = mapped_column(Float)
    expected_fees: Mapped[float | None] = mapped_column(Float)
    expected_spread_cost: Mapped[float | None] = mapped_column(Float)
    expected_slippage: Mapped[float | None] = mapped_column(Float)
    expected_net_return: Mapped[float | None] = mapped_column(Float)
    estimated_fill_probability: Mapped[float | None] = mapped_column(Float)
    available_liquidity: Mapped[float | None] = mapped_column(Float)
    correlation_score: Mapped[float | None] = mapped_column(Float)

    # Rejection
    rejection_reason: Mapped[RejectionReason | None] = mapped_column(Enum(RejectionReason))

    metadata_: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    __table_args__ = (
        Index("ix_opps_strategy_status", "strategy_id", "status"),
        Index("ix_opps_created", "created_at"),
    )


class Order(Base):
    """Order record — complete audit trail."""

    __tablename__ = "orders"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=new_uuid)
    client_order_id: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False, index=True
    )
    exchange_order_id: Mapped[str | None] = mapped_column(String(64), index=True)
    opportunity_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False), ForeignKey("opportunities.id")
    )
    strategy_id: Mapped[str | None] = mapped_column(String(64))
    exchange_id: Mapped[str | None] = mapped_column(UUID(as_uuid=False))
    instrument_id: Mapped[str | None] = mapped_column(UUID(as_uuid=False))
    symbol: Mapped[str | None] = mapped_column(String(32))
    side: Mapped[OrderSide] = mapped_column(Enum(OrderSide), nullable=False)
    order_type: Mapped[OrderType] = mapped_column(Enum(OrderType), nullable=False)
    state: Mapped[OrderState] = mapped_column(
        Enum(OrderState), default=OrderState.PENDING, nullable=False, index=True
    )
    mode: Mapped[Mode] = mapped_column(Enum(Mode), default=Mode.PAPER, nullable=False)

    quantity: Mapped[float] = mapped_column(Float, nullable=False)
    price: Mapped[float | None] = mapped_column(Float)
    filled_quantity: Mapped[float] = mapped_column(Float, default=0.0)
    filled_avg_price: Mapped[float | None] = mapped_column(Float)
    total_fees: Mapped[float] = mapped_column(Float, default=0.0)
    fee_currency: Mapped[str | None] = mapped_column(String(16))

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_fill_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    metadata_: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSONB)

    __table_args__ = (Index("ix_orders_created", "created_at"),)


class Fill(Base):
    """Individual fill event — exchange-reported."""

    __tablename__ = "fills"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=new_uuid)
    order_id: Mapped[str] = mapped_column(UUID(as_uuid=False), ForeignKey("orders.id"), index=True)
    exchange_fill_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    quantity: Mapped[float] = mapped_column(Float, nullable=False)
    fee: Mapped[float] = mapped_column(Float, default=0.0)
    fee_currency: Mapped[str | None] = mapped_column(String(16))
    is_maker: Mapped[bool] = mapped_column(Boolean, default=False)
    filled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Position(Base):
    """Current and historical positions."""

    __tablename__ = "positions"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=new_uuid)
    exchange_id: Mapped[str | None] = mapped_column(UUID(as_uuid=False))
    instrument_id: Mapped[str | None] = mapped_column(UUID(as_uuid=False))
    symbol: Mapped[str | None] = mapped_column(String(32))
    side: Mapped[PositionSide] = mapped_column(Enum(PositionSide), nullable=False)
    mode: Mapped[Mode] = mapped_column(Enum(Mode), default=Mode.PAPER, nullable=False)

    entry_quantity: Mapped[float] = mapped_column(Float, nullable=False)
    current_quantity: Mapped[float] = mapped_column(Float, nullable=False)
    entry_avg_price: Mapped[float] = mapped_column(Float, nullable=False)
    current_price: Mapped[float | None] = mapped_column(Float)
    stop_loss_price: Mapped[float | None] = mapped_column(Float)
    take_profit_price: Mapped[float | None] = mapped_column(Float)

    unrealized_pnl: Mapped[float | None] = mapped_column(Float)
    realized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    total_fees: Mapped[float] = mapped_column(Float, default=0.0)

    is_open: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    close_reason: Mapped[str | None] = mapped_column(String(128))

    metadata_: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSONB)

    __table_args__ = (Index("ix_positions_open", "is_open"),)


class AccountSnapshot(Base):
    """Periodic account balance snapshot."""

    __tablename__ = "account_snapshots"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=new_uuid)
    exchange_id: Mapped[str | None] = mapped_column(UUID(as_uuid=False))
    mode: Mapped[Mode] = mapped_column(Enum(Mode), default=Mode.PAPER, nullable=False)

    total_equity: Mapped[float] = mapped_column(Float, nullable=False)
    available_balance: Mapped[float] = mapped_column(Float, nullable=False)
    reserved_balance: Mapped[float] = mapped_column(Float, default=0.0)
    unrealized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    daily_pnl: Mapped[float | None] = mapped_column(Float)
    cumulative_pnl: Mapped[float | None] = mapped_column(Float)

    snapshot_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class StrategyMetric(Base):
    """Rolling strategy performance metrics."""

    __tablename__ = "strategy_metrics"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=new_uuid)
    strategy_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    mode: Mapped[Mode] = mapped_column(Enum(Mode), default=Mode.PAPER, nullable=False)

    total_signals: Mapped[int] = mapped_column(Integer, default=0)
    total_trades: Mapped[int] = mapped_column(Integer, default=0)
    winning_trades: Mapped[int] = mapped_column(Integer, default=0)
    losing_trades: Mapped[int] = mapped_column(Integer, default=0)
    win_rate: Mapped[float | None] = mapped_column(Float)
    gross_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    net_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    total_fees: Mapped[float] = mapped_column(Float, default=0.0)
    total_spread_cost: Mapped[float] = mapped_column(Float, default=0.0)
    total_slippage: Mapped[float] = mapped_column(Float, default=0.0)
    profit_factor: Mapped[float | None] = mapped_column(Float)
    sharpe_ratio: Mapped[float | None] = mapped_column(Float)
    sortino_ratio: Mapped[float | None] = mapped_column(Float)
    max_drawdown_pct: Mapped[float | None] = mapped_column(Float)
    expectancy: Mapped[float | None] = mapped_column(Float)
    avg_win: Mapped[float | None] = mapped_column(Float)
    avg_loss: Mapped[float | None] = mapped_column(Float)
    avg_trade_return_pct: Mapped[float | None] = mapped_column(Float)
    compounded_return_pct: Mapped[float | None] = mapped_column(Float)

    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        Index("ix_metrics_strategy_period", "strategy_id", "period_start", "period_end"),
    )


class AuditEvent(Base):
    """Immutable audit log — append-only, never deleted."""

    __tablename__ = "audit_events"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=new_uuid)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    severity: Mapped[str] = mapped_column(
        String(16), default="INFO"
    )  # INFO, WARNING, ERROR, CRITICAL
    component: Mapped[str] = mapped_column(String(64), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    details: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )

    __table_args__ = (Index("ix_audit_type_created", "event_type", "created_at"),)
