"""Hard rules override LLM proposals that violate action_space or risk."""
from __future__ import annotations

from core.types import Operation, Side, TradeDecision
from decision.decision_inputs import build_decision_packet
from decision.hard_rules import enforce
from tests.conftest import make_position


def _decision(op: Operation, sym: str, side: Side | None = None, lev: int = 3,
              size: float = 0.2) -> TradeDecision:
    return TradeDecision(
        operation=op, symbol=sym, direction=side, size_fraction=size,
        leverage=lev, stop_loss_mode="atr", take_profit_mode="atr",
        trailing_enabled=True, reason="test",
    )


def test_override_open_when_position_already_same_side(settings, frames_all, risk_empty, external_empty):
    positions = {"BTC": make_position("BTC", Side.LONG)}
    risk_empty.open_position_count = 1
    packet = build_decision_packet(
        settings=settings, balance_usd=1000.0, frames=frames_all,
        positions=positions, risk=risk_empty, external=external_empty,
    )
    prop = _decision(Operation.OPEN, "BTC", Side.LONG)
    final, override = enforce(packet, prop, settings)
    assert final.operation == Operation.HOLD
    assert override  # some reason string


def test_leverage_is_clamped(settings, frames_all, risk_empty, external_empty):
    packet = build_decision_packet(
        settings=settings, balance_usd=1000.0, frames=frames_all,
        positions={}, risk=risk_empty, external=external_empty,
    )
    prop = _decision(Operation.OPEN, "BTC", Side.LONG, lev=99)
    final, override = enforce(packet, prop, settings)
    if final.operation == Operation.OPEN:
        assert final.leverage <= settings.risk.max_leverage


def test_hold_passes_through(settings, frames_all, risk_empty, external_empty):
    packet = build_decision_packet(
        settings=settings, balance_usd=1000.0, frames=frames_all,
        positions={}, risk=risk_empty, external=external_empty,
    )
    prop = _decision(Operation.HOLD, "BTC", None)
    final, override = enforce(packet, prop, settings)
    assert final.operation == Operation.HOLD


def test_close_without_position_is_blocked(settings, frames_all, risk_empty, external_empty):
    packet = build_decision_packet(
        settings=settings, balance_usd=1000.0, frames=frames_all,
        positions={}, risk=risk_empty, external=external_empty,
    )
    prop = _decision(Operation.CLOSE, "BTC", Side.LONG)
    final, override = enforce(packet, prop, settings)
    assert final.operation == Operation.HOLD
    assert override
