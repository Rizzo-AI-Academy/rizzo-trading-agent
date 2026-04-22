"""Canonical typed models used across the trading system.

Every cross-module contract flows through these dataclasses. Raw market data,
LLM output, exchange payloads are normalized into these structures as early as
possible so downstream code works against a stable schema.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional


class Side(str, Enum):
    LONG = "long"
    SHORT = "short"


class Regime(str, Enum):
    TRENDING_UP = "trending_up"
    TRENDING_DOWN = "trending_down"
    RANGE = "range"
    CHOP = "chop"


class Operation(str, Enum):
    OPEN = "open"
    CLOSE = "close"
    HOLD = "hold"


class OrderStatus(str, Enum):
    PENDING = "pending"
    SUBMITTED = "submitted"
    FILLED = "filled"
    PARTIAL = "partial"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    ERROR = "error"


@dataclass
class PivotPoints:
    pp: float
    s1: float
    s2: float
    r1: float
    r2: float


@dataclass
class MarketFrame:
    """Normalized snapshot of one ticker at decision time."""
    ticker: str
    ts: datetime
    price: float
    ema20: float
    ema50: float
    atr14: float
    atr_pct: float
    rsi14: float
    rsi7: float
    macd: float
    macd_slope: float
    funding_rate: float
    open_interest: float
    pivots: PivotPoints
    volume_ratio: float
    regime: Regime
    structure_bias: Side | None
    swing_high: float
    swing_low: float
    recent_closes: List[float]
    est_fee_cost: float
    spread_pct: float

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["ts"] = self.ts.isoformat()
        d["regime"] = self.regime.value
        d["structure_bias"] = self.structure_bias.value if self.structure_bias else None
        return d


@dataclass
class PositionSnapshot:
    symbol: str
    side: Side
    size: float
    entry_price: float
    mark_price: float
    leverage: float
    pnl_usd: float
    pnl_pct: float
    mae_pct: float
    mfe_pct: float
    bars_held: int
    opened_at: datetime
    sl_price: Optional[float]
    tp_price: Optional[float]
    trailing_active: bool
    client_order_id: Optional[str]

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["side"] = self.side.value
        d["opened_at"] = self.opened_at.isoformat()
        return d


@dataclass
class RiskState:
    balance_usd: float
    available_usd: float
    used_margin: float
    exposure_ratio: float
    open_position_count: int
    max_positions: int
    consecutive_losses: int
    daily_pnl: float
    daily_pnl_pct: float
    cooldown_active: bool
    cooldown_reason: Optional[str] = None


@dataclass
class ExternalContext:
    sentiment_value: Optional[int]
    sentiment_label: Optional[str]
    news_summary: str
    forecasts: Dict[str, Dict[str, float]]  # ticker -> {horizon: change_pct}

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class SetupQuality:
    score: float            # 0..1
    trend_alignment: float  # -1..1
    momentum: float         # -1..1
    volatility_ok: bool
    structure_ok: bool
    fee_vs_target_ok: bool
    notes: List[str] = field(default_factory=list)


@dataclass
class ActionSpace:
    """Deterministic set of allowed operations per symbol."""
    allowed: Dict[str, List[str]]
    forbidden_reasons: Dict[str, Dict[str, str]]

    def is_allowed(self, symbol: str, op: str) -> bool:
        return op in self.allowed.get(symbol, [])

    def reason(self, symbol: str, op: str) -> str:
        return self.forbidden_reasons.get(symbol, {}).get(op, "ok")


@dataclass
class DecisionPacket:
    """Compact, canonical input for the decision kernel."""
    ts: datetime
    balance_usd: float
    tickers: List[str]
    market_frames: Dict[str, MarketFrame]
    positions: Dict[str, PositionSnapshot]
    risk: RiskState
    setup_quality: Dict[str, SetupQuality]
    external: ExternalContext
    action_space: ActionSpace
    run_id: str

    def compact_dict(self) -> Dict[str, Any]:
        return {
            "ts": self.ts.isoformat(),
            "run_id": self.run_id,
            "balance_usd": round(self.balance_usd, 2),
            "risk": asdict(self.risk),
            "positions": {k: v.to_dict() for k, v in self.positions.items()},
            "market_frames": {k: v.to_dict() for k, v in self.market_frames.items()},
            "setup_quality": {k: asdict(v) for k, v in self.setup_quality.items()},
            "external": self.external.to_dict(),
            "action_space": asdict(self.action_space),
        }


@dataclass
class TradeDecision:
    operation: Operation
    symbol: str
    direction: Optional[Side]
    size_fraction: float
    leverage: int
    stop_loss_mode: str = "atr"          # 'atr' | 'structure' | 'fixed'
    take_profit_mode: str = "atr"        # 'atr' | 'structure' | 'none'
    trailing_enabled: bool = True
    reason: str = ""
    raw_llm: Optional[Dict[str, Any]] = None
    deterministic_override: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "operation": self.operation.value,
            "symbol": self.symbol,
            "direction": self.direction.value if self.direction else None,
            "size_fraction": self.size_fraction,
            "leverage": self.leverage,
            "stop_loss_mode": self.stop_loss_mode,
            "take_profit_mode": self.take_profit_mode,
            "trailing_enabled": self.trailing_enabled,
            "reason": self.reason,
            "deterministic_override": self.deterministic_override,
        }


@dataclass
class StopLossPlan:
    sl_price: float
    tp_price: Optional[float]
    trailing_enabled: bool
    trailing_activation_pct: float
    trailing_distance_atr: float
    basis: str  # 'atr' | 'structure' | 'fixed'
    notes: List[str] = field(default_factory=list)


@dataclass
class OrderIntent:
    client_order_id: str
    symbol: str
    side: Side
    size: float
    order_kind: str        # 'market' | 'stop' | 'take_profit'
    reduce_only: bool
    price: Optional[float]  # trigger price for stop/tp
    leverage: Optional[int]
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass
class OrderResult:
    client_order_id: str
    status: OrderStatus
    exchange_order_id: Optional[str]
    filled_size: float
    avg_price: Optional[float]
    raw: Dict[str, Any]
    error: Optional[str] = None


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
