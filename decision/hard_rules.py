"""Hard deterministic rules applied *after* LLM proposal.

These rules have authority over the LLM: if the LLM proposes an action that
violates them, the system overrides with a safer deterministic action and
logs the override reason. This is the guarantee that the LLM cannot produce
a catastrophic order.
"""
from __future__ import annotations

from typing import Tuple

from core.config import Settings
from core.types import (
    ActionSpace,
    DecisionPacket,
    Operation,
    Side,
    TradeDecision,
)


def _op_key(operation: Operation, direction: Side | None) -> str:
    if operation == Operation.OPEN:
        return "open_long" if direction == Side.LONG else "open_short"
    if operation == Operation.CLOSE:
        return "close"
    return "hold"


def enforce(packet: DecisionPacket, proposed: TradeDecision,
            settings: Settings) -> Tuple[TradeDecision, str | None]:
    """Return (final_decision, override_reason|None)."""
    symbol = proposed.symbol
    frame = packet.market_frames.get(symbol)
    action = _op_key(proposed.operation, proposed.direction)

    # Symbol must be in managed universe
    if frame is None:
        return _hold(proposed, f"symbol_not_in_universe:{symbol}"), \
            "symbol_not_in_universe"

    # Action must be in the action space
    if not packet.action_space.is_allowed(symbol, action):
        reason = packet.action_space.reason(symbol, action)
        return _hold(proposed, f"action_forbidden:{reason}"), reason

    # Leverage cap
    if proposed.leverage > settings.risk.max_leverage:
        proposed.leverage = settings.risk.max_leverage
        proposed.reason = (proposed.reason or "") + f" [clamp_lev={settings.risk.max_leverage}]"

    # Size cap
    if proposed.size_fraction > settings.risk.max_single_position:
        proposed.size_fraction = settings.risk.max_single_position
        proposed.reason = (proposed.reason or "") + f" [clamp_size={settings.risk.max_single_position}]"

    # Notional minimum — only relevant for opens
    if proposed.operation == Operation.OPEN:
        est_notional = packet.balance_usd * proposed.size_fraction * max(1, proposed.leverage)
        if est_notional < settings.risk.min_notional_usd:
            return _hold(proposed, f"notional_below_min:{est_notional:.2f}"), \
                "notional_below_min"

    # Risk cooldown -> no opens
    if proposed.operation == Operation.OPEN and packet.risk.cooldown_active:
        return _hold(proposed, f"risk_cooldown:{packet.risk.cooldown_reason}"), \
            "risk_cooldown"

    # Setup quality already enforced in action_space; double-check here
    if proposed.operation == Operation.OPEN:
        q = packet.setup_quality.get(symbol)
        if q and q.score < settings.risk.min_setup_quality:
            return _hold(proposed, f"low_setup_quality:{q.score}"), "low_setup_quality"

    return proposed, None


def _hold(proposed: TradeDecision, reason: str) -> TradeDecision:
    return TradeDecision(
        operation=Operation.HOLD,
        symbol=proposed.symbol,
        direction=None,
        size_fraction=0.0,
        leverage=1,
        stop_loss_mode=proposed.stop_loss_mode,
        take_profit_mode=proposed.take_profit_mode,
        trailing_enabled=False,
        reason=f"override:{reason}",
        raw_llm=proposed.raw_llm,
        deterministic_override=reason,
    )
