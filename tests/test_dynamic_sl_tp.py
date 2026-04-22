"""SL / TP / trailing behavior: pure functions, no I/O."""
from __future__ import annotations

from core.types import Side
from execution.stop_loss import initial_plan, update_plan, should_force_close
from tests.conftest import make_frame, make_position


def test_initial_sl_atr_bounded(settings):
    frame = make_frame("BTC", price=100.0, atr_pct=0.01)
    plan = initial_plan(frame, Side.LONG, settings, sl_mode="atr")
    dist_pct = (frame.price - plan.sl_price) / frame.price
    assert settings.sl.min_sl_pct <= dist_pct <= settings.sl.max_sl_pct
    assert plan.sl_price < frame.price


def test_initial_sl_short_symmetric(settings):
    frame = make_frame("BTC", price=100.0, atr_pct=0.01)
    plan = initial_plan(frame, Side.SHORT, settings, sl_mode="atr")
    assert plan.sl_price > frame.price


def test_initial_tp_is_r_multiple(settings):
    frame = make_frame("BTC", price=100.0, atr_pct=0.01)
    plan = initial_plan(frame, Side.LONG, settings, sl_mode="atr", tp_mode="atr")
    sl_dist = frame.price - plan.sl_price
    tp_dist = plan.tp_price - frame.price
    # TP must be at least r_multiple * sl_dist
    assert tp_dist >= sl_dist * settings.tp.r_multiple - 1e-9


def test_initial_tp_none_when_disabled(settings):
    frame = make_frame("BTC", price=100.0)
    plan = initial_plan(frame, Side.LONG, settings, tp_mode="none")
    assert plan.tp_price is None


def test_initial_sl_fixed_mode_uses_fallback(settings):
    frame = make_frame("BTC", price=100.0, atr_pct=0.0)  # no ATR
    plan = initial_plan(frame, Side.LONG, settings, sl_mode="fixed")
    expected_dist = frame.price * settings.sl.fixed_pct_fallback
    assert abs((frame.price - plan.sl_price) - expected_dist) < 0.5
