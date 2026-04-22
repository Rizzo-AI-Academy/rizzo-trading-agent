"""Risk gate: last deterministic check before an order goes out.

Responsibilities:
- Refuse opens that would push exposure over cap.
- Refuse sizes that result in notional < exchange minimum.
- Refuse when cooldown active.
- Clamp leverage.
- Confirm SL distance is within bounds.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from core.config import Settings
from core.types import (
    DecisionPacket,
    Operation,
    StopLossPlan,
    TradeDecision,
)
from risk.sizing import SizingResult


@dataclass
class RiskVerdict:
    approved: bool
    reason: Optional[str]
    adjusted_notional: float = 0.0
    adjusted_leverage: int = 1


def check_open(
    packet: DecisionPacket,
    decision: TradeDecision,
    sizing: SizingResult,
    plan: StopLossPlan,
    settings: Settings,
) -> RiskVerdict:
    # cooldown
    if packet.risk.cooldown_active:
        return RiskVerdict(False, f"cooldown:{packet.risk.cooldown_reason}")

    # exposure
    projected_exposure = (
        packet.risk.used_margin + sizing.notional_usd / max(1, decision.leverage)
    ) / max(packet.balance_usd, 1e-6)
    if projected_exposure > settings.risk.max_portfolio_exposure:
        return RiskVerdict(False, f"exposure_cap:{projected_exposure:.3f}")

    # notional minimum
    if sizing.notional_usd < settings.risk.min_notional_usd:
        return RiskVerdict(False, f"min_notional:{sizing.notional_usd:.2f}")

    # SL sanity
    frame = packet.market_frames[decision.symbol]
    sl_dist_pct = abs(frame.price - plan.sl_price) / frame.price
    if sl_dist_pct < settings.sl.min_sl_pct:
        return RiskVerdict(False, f"sl_too_tight:{sl_dist_pct:.4f}")
    if sl_dist_pct > settings.sl.max_sl_pct:
        return RiskVerdict(False, f"sl_too_wide:{sl_dist_pct:.4f}")

    leverage = min(decision.leverage, settings.risk.max_leverage)
    return RiskVerdict(
        approved=True,
        reason=None,
        adjusted_notional=sizing.notional_usd,
        adjusted_leverage=leverage,
    )


def check_close(packet: DecisionPacket, decision: TradeDecision) -> RiskVerdict:
    pos = packet.positions.get(decision.symbol)
    if pos is None:
        return RiskVerdict(False, "no_position_to_close")
    return RiskVerdict(True, None, adjusted_notional=pos.size * pos.mark_price,
                       adjusted_leverage=int(pos.leverage) or 1)
