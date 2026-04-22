"""DecisionPacket is coherent: frames + positions + risk + quality + action_space
all populated and consistent."""
from __future__ import annotations

from core.types import Side
from decision.decision_inputs import build_decision_packet
from tests.conftest import make_frame, make_position


def test_packet_has_all_fields(settings, frames_all, risk_empty, external_empty):
    packet = build_decision_packet(
        settings=settings,
        balance_usd=1000.0,
        frames=frames_all,
        positions={},
        risk=risk_empty,
        external=external_empty,
    )
    assert set(packet.market_frames.keys()) == {"BTC", "ETH", "SOL"}
    assert set(packet.setup_quality.keys()) == {"BTC", "ETH", "SOL"}
    assert set(packet.action_space.allowed.keys()) == {"BTC", "ETH", "SOL"}
    assert packet.run_id.startswith("run-")


def test_action_space_blocks_same_side_when_open(settings, frames_all, risk_empty, external_empty):
    positions = {"BTC": make_position("BTC", Side.LONG, entry=90000, mark=91000, sl=88000)}
    risk_empty.open_position_count = 1
    packet = build_decision_packet(
        settings=settings, balance_usd=1000.0, frames=frames_all,
        positions=positions, risk=risk_empty, external=external_empty,
    )
    assert "open_long" not in packet.action_space.allowed["BTC"]
    assert "close" in packet.action_space.allowed["BTC"]
    assert "open_short" not in packet.action_space.allowed["BTC"]  # must close first


def test_action_space_blocks_opens_during_cooldown(settings, frames_all, risk_empty, external_empty):
    risk_empty.cooldown_active = True
    risk_empty.cooldown_reason = "daily_loss_halt"
    packet = build_decision_packet(
        settings=settings, balance_usd=1000.0, frames=frames_all,
        positions={}, risk=risk_empty, external=external_empty,
    )
    for sym in frames_all:
        assert "open_long" not in packet.action_space.allowed[sym]
        assert "open_short" not in packet.action_space.allowed[sym]


def test_action_space_respects_max_positions(settings, frames_all, risk_empty, external_empty):
    positions = {
        "BTC": make_position("BTC", Side.LONG, entry=90000, mark=91000),
        "ETH": make_position("ETH", Side.SHORT, entry=3100, mark=3080),
    }
    risk_empty.open_position_count = 2
    packet = build_decision_packet(
        settings=settings, balance_usd=1000.0, frames=frames_all,
        positions=positions, risk=risk_empty, external=external_empty,
    )
    assert "open_long" not in packet.action_space.allowed["SOL"]
    # can still manage existing positions
    assert "close" in packet.action_space.allowed["BTC"]
    assert "close" in packet.action_space.allowed["ETH"]


def test_setup_quality_penalises_chop_regime(settings, risk_empty, external_empty):
    from core.types import Regime
    frames = {"BTC": make_frame("BTC", atr_pct=0.0008, regime=Regime.CHOP, bias=None, macd_slope=0.0)}
    packet = build_decision_packet(
        settings=settings, balance_usd=1000.0, frames=frames,
        positions={}, risk=risk_empty, external=external_empty,
    )
    q = packet.setup_quality["BTC"]
    assert q.score < 0.5
    assert "regime_chop" in q.notes or "atr_too_low" in q.notes
