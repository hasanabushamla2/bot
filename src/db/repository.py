"""Database repository — async data access layer.

Provides async methods for persisting and querying all domain entities.
Uses SQLAlchemy async sessions.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import (
    AccountSnapshot,
    AuditEvent,
    Fill,
    Instrument,
    Opportunity,
    OpportunityStatus,
    Order,
    OrderState,
    Position,
    Signal,
    StrategyMetric,
)


class Repository:
    """Async repository for all database operations."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # --- Signals ---

    async def save_signal(self, signal: Signal) -> Signal:
        self.session.add(signal)
        await self.session.flush()
        return signal

    # --- Opportunities ---

    async def save_opportunity(self, opp: Opportunity) -> Opportunity:
        self.session.add(opp)
        await self.session.flush()
        return opp

    async def get_recent_opportunities(
        self, limit: int = 100, status: OpportunityStatus | None = None
    ) -> Sequence[Opportunity]:
        stmt = select(Opportunity).order_by(Opportunity.created_at.desc()).limit(limit)
        if status:
            stmt = stmt.where(Opportunity.status == status)
        result = await self.session.execute(stmt)
        return result.scalars().all()

    # --- Orders ---

    async def save_order(self, order: Order) -> Order:
        self.session.add(order)
        await self.session.flush()
        return order

    async def get_order_by_client_id(self, client_order_id: str) -> Order | None:
        stmt = select(Order).where(Order.client_order_id == client_order_id)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_open_orders(self) -> Sequence[Order]:
        stmt = select(Order).where(
            Order.state.in_([OrderState.OPEN, OrderState.PARTIALLY_FILLED, OrderState.PENDING])
        )
        result = await self.session.execute(stmt)
        return result.scalars().all()

    # --- Fills ---

    async def save_fill(self, fill: Fill) -> Fill:
        self.session.add(fill)
        await self.session.flush()
        return fill

    # --- Positions ---

    async def save_position(self, position: Position) -> Position:
        self.session.add(position)
        await self.session.flush()
        return position

    async def get_open_positions(self) -> Sequence[Position]:
        stmt = select(Position).where(Position.is_open.is_(True))
        result = await self.session.execute(stmt)
        return result.scalars().all()

    # --- Account Snapshots ---

    async def save_snapshot(self, snapshot: AccountSnapshot) -> AccountSnapshot:
        self.session.add(snapshot)
        await self.session.flush()
        return snapshot

    # --- Audit ---

    async def save_audit_event(self, event: AuditEvent) -> AuditEvent:
        self.session.add(event)
        await self.session.flush()
        return event

    # --- Instruments ---

    async def save_instrument(self, instrument: Instrument) -> Instrument:
        self.session.add(instrument)
        await self.session.flush()
        return instrument

    async def get_instruments(self, exchange_id: str | None = None) -> Sequence[Instrument]:
        stmt = select(Instrument)
        if exchange_id:
            stmt = stmt.where(Instrument.exchange_id == exchange_id)
        result = await self.session.execute(stmt)
        return result.scalars().all()

    # --- Strategy Metrics ---

    async def save_strategy_metric(self, metric: StrategyMetric) -> StrategyMetric:
        self.session.add(metric)
        await self.session.flush()
        return metric

    # --- Health ---

    async def health_check(self) -> bool:
        """Check database connectivity."""
        try:
            await self.session.execute(text("SELECT 1"))
            return True
        except Exception:
            return False
