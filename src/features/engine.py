"""Incremental Feature Engine."""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


@dataclass
class InstrumentFeatures:
    symbol: str = ""
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    last_price: float = 0.0
    return_1m_pct: float = 0.0
    return_5m_pct: float = 0.0
    return_15m_pct: float = 0.0
    return_1h_pct: float = 0.0
    momentum_1m: float = 0.0
    momentum_5m: float = 0.0
    acceleration: float = 0.0
    atr_pct: float = 0.0
    volatility_5m_pct: float = 0.0
    volume_24h: float = 0.0
    volume_1h: float = 0.0
    volume_5m: float = 0.0
    relative_volume: float = 1.0
    vwap_deviation_pct: float = 0.0
    high_5m: float = 0.0
    low_5m: float = 0.0
    breakout_range_pct: float = 0.0
    breakout_position_pct: float = 0.0
    bid: float = 0.0
    ask: float = 0.0
    spread_bps: float = 0.0
    bid_depth_10bps: float = 0.0
    ask_depth_10bps: float = 0.0
    bid_ask_ratio: float = 1.0
    buy_volume_5m: float = 0.0
    sell_volume_5m: float = 0.0
    trade_flow_ratio: float = 1.0
    liquidity_score: float = 0.0
    trend_strength: float = 0.0
    sample_count: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


class FeatureEngine:
    def __init__(self, max_instruments: int = 500):
        self.max_instruments = max_instruments
        self._pw: dict[str, deque[tuple[float, float]]] = {}
        self._vw: dict[str, deque[tuple[float, float]]] = {}
        self._tw: dict[str, deque[tuple[float, bool, float]]] = {}
        self._va: dict[str, tuple[float, float]] = {}
        self._features: dict[str, InstrumentFeatures] = {}

    def get(self, symbol: str) -> InstrumentFeatures:
        if symbol not in self._features:
            if len(self._features) >= self.max_instruments:
                oldest = min(self._features.items(), key=lambda kv: kv[1].updated_at)[0]
                del self._features[oldest]
            self._features[symbol] = InstrumentFeatures(symbol=symbol)
        return self._features[symbol]

    def update_price(
        self, symbol: str, price: float, timestamp: datetime | None = None
    ) -> InstrumentFeatures:
        feat = self.get(symbol)
        ts = (timestamp or datetime.now(UTC)).timestamp()
        if symbol not in self._pw:
            self._pw[symbol] = deque(maxlen=2000)
        pw = self._pw[symbol]
        pw.append((ts, price))
        self._prune(pw, ts)
        feat.last_price = price
        feat.return_1m_pct = self._ret(pw, ts, 60)
        feat.return_5m_pct = self._ret(pw, ts, 300)
        feat.return_15m_pct = self._ret(pw, ts, 900)
        feat.return_1h_pct = self._ret(pw, ts, 3600)
        feat.momentum_1m = feat.return_1m_pct
        feat.momentum_5m = feat.return_5m_pct
        old = self._ret(pw, ts - 60, 60)
        feat.acceleration = feat.momentum_1m - old
        p5 = [p for t, p in pw if ts - t <= 300]
        if p5:
            feat.high_5m = max(p5)
            feat.low_5m = min(p5)
            rng = feat.high_5m - feat.low_5m
            feat.breakout_range_pct = (rng / price * 100) if price > 0 else 0
            feat.breakout_position_pct = ((price - feat.low_5m) / rng * 100) if rng > 0 else 50
        if len(p5) >= 5:
            rets = [
                (p5[i] - p5[i - 1]) / p5[i - 1] * 100 for i in range(1, len(p5)) if p5[i - 1] > 0
            ]
            if rets:
                m = sum(rets) / len(rets)
                feat.volatility_5m_pct = math.sqrt(sum((r - m) ** 2 for r in rets) / len(rets))
                # Tick-based ATR proxy: mean absolute realized movement.  It
                # uses only completed observations already in the rolling
                # buffer and is intentionally distinct from dispersion.
                feat.atr_pct = sum(abs(r) for r in rets) / len(rets)
        feat.trend_strength = self._trend(pw, ts)
        k = symbol
        if k not in self._va:
            self._va[k] = (0.0, 0.0)
        cp, cv = self._va[k]
        cp += price
        cv += 1.0
        self._va[k] = (cp, cv)
        v = cp / cv if cv > 0 else price
        feat.vwap_deviation_pct = (price - v) / v * 100 if v > 0 else 0
        feat.sample_count += 1
        feat.updated_at = datetime.now(UTC)
        return feat

    def update_order_book(
        self, symbol: str, bid: float, ask: float, bid_depth: float = 0.0, ask_depth: float = 0.0
    ) -> InstrumentFeatures:
        feat = self.get(symbol)
        feat.bid = bid
        feat.ask = ask
        mid = (bid + ask) / 2 if bid > 0 and ask > 0 else 0
        feat.spread_bps = (ask - bid) / mid * 10000 if mid > 0 else 0
        feat.bid_depth_10bps = bid_depth
        feat.ask_depth_10bps = ask_depth
        feat.bid_ask_ratio = bid_depth / ask_depth if ask_depth > 0 else 1.0
        feat.updated_at = datetime.now(UTC)
        return feat

    def update_volume(
        self, symbol: str, volume_24h: float, volume_1h: float = 0.0
    ) -> InstrumentFeatures:
        feat = self.get(symbol)
        feat.volume_24h = volume_24h
        feat.volume_1h = volume_1h
        if symbol not in self._vw:
            self._vw[symbol] = deque(maxlen=50)
        vw = self._vw[symbol]
        now = datetime.now(UTC).timestamp()
        if volume_24h > 0:
            vw.append((now, volume_24h))
        recent = [v for _, v in list(vw)[-5:]]
        avg = sum(recent) / len(recent) if recent else volume_24h
        feat.relative_volume = volume_24h / avg if avg > 0 else 1.0
        feat.volume_5m = volume_1h / 12.0 if volume_1h > 0 else 0.0
        feat.updated_at = datetime.now(UTC)
        return feat

    def update_trade_flow(self, symbol: str, is_buy: bool, volume: float) -> InstrumentFeatures:
        feat = self.get(symbol)
        if symbol not in self._tw:
            self._tw[symbol] = deque(maxlen=5000)
        tw = self._tw[symbol]
        now = datetime.now(UTC).timestamp()
        tw.append((now, is_buy, volume))
        cutoff = now - 300
        bv = sum(v for t, b, v in tw if b and t >= cutoff)
        sv = sum(v for t, b, v in tw if not b and t >= cutoff)
        feat.buy_volume_5m = bv
        feat.sell_volume_5m = sv
        feat.trade_flow_ratio = bv / sv if sv > 0 else 1.0
        feat.updated_at = datetime.now(UTC)
        return feat

    def _ret(self, pw: deque[tuple[float, float]], now: float, sec: float) -> float:
        if not pw:
            return 0.0
        cutoff = now - sec
        old = None
        for t, p in pw:
            if t >= cutoff:
                if old is None:
                    old = p
                break
            old = p
        if old is None or old <= 0:
            return 0.0
        return (pw[-1][1] - old) / old * 100.0

    def _prune(self, pw: deque[tuple[float, float]], now: float) -> None:
        cutoff = now - 3600
        while pw and pw[0][0] < cutoff:
            pw.popleft()

    def _trend(self, pw: deque[tuple[float, float]], now: float) -> float:
        p15 = [p for t, p in pw if now - t <= 900]
        if len(p15) < 10:
            return 0.0
        first = p15[0]
        last = p15[-1]
        if first <= 0:
            return 0.0
        return max(-1.0, min(1.0, (last - first) / first * 20.0))
