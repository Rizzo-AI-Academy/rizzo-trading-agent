"""Trailing logic: activation threshold, never-loosen, profit lock."""
from __future__ import annotations

from core.types import Side
from execution.stop_loss import update_plan, should_force_close
from tests.conftest import make_frame, make_position


def test_trailing_inactive_before_activation(settings):
    # position barely in profit (below activation R multiple)
    pos = make_position("BTC", Side.LONG, entry=100, mark=100.2, sl=98.0)
    frame = make_frame("BTC", price=100.2, atr_pct=0.02)
    plan = update_plan(pos, frame, settings)
    assert plan is None  # no tightening yet


def test_trailing_activates_after_r_multiple(settings):
    # initial SL at 98 (2% risk), activation at 1R -> price needs ~102
    pos = make_position("BTC", Side.LONG, entry=100, mark=102.5, sl=98.0)
    frame = make_frame("BTC", price=102.5, atr_pct=0.01)
    plan = update_plan(pos, frame, settings)
    assert plan is not None
    assert plan.trailing_enabled is True
    # new SL should be strictly above the original (never loosen)
    assert plan.sl_price > pos.sl_price


def test_trailing_never_loosens(settings):
    # trailing already active, price retraces a bit; SL should not move back
    pos = make_position("BTC", Side.LONG, entry=100, mark=102.0, sl=100.5, trailing=True)
    frame = make_frame("BTC", price=101.8, atr_pct=0.01)
    plan = update_plan(pos, frame, settings)
    if plan is not None:
        assert plan.sl_price >= pos.sl_price


def test_trailing_locks_minimum_profit(settings):
    pos = make_position("BTC", Side.LONG, entry=100, mark=103.0, sl=98.0)
    frame = make_frame("BTC", price=103.0, atr_pct=0.005)  # tight ATR -> trail very tight
    plan = update_plan(pos, frame, settings)
    assert plan is not None
    # new SL must be above entry * (1 + min_profit_lock_pct)
    min_lock = pos.entry_price * (1 + settings.trailing.min_profit_lock_pct)
    assert plan.sl_price >= min_lock - 1e-9


def test_short_trailing_mirrors_long(settings):
    pos = make_position("BTC", Side.SHORT, entry=100, mark=97.0, sl=102.0)
    frame = make_frame("BTC", price=97.0, atr_pct=0.01, bias=Side.SHORT)
    plan = update_plan(pos, frame, settings)
    assert plan is not None
    assert plan.sl_price < pos.sl_price  # SL moves down for shorts


def test_structure_flip_forces_close_when_losing():
    pos = make_position("BTC", Side.LONG, entry=100, mark=98.0, sl=97.0)
    frame = make_frame("BTC", price=98.0, bias=Side.SHORT)
    reason = should_force_close(pos, frame)
    assert reason is not None


def test_structure_flip_does_not_force_close_when_winning():
    pos = make_position("BTC", Side.LONG, entry=100, mark=102.0, sl=101.0)
    frame = make_frame("BTC", price=102.0, bias=Side.SHORT)
    reason = should_force_close(pos, frame)
    assert reason is None  # winners are not killed by a single bar bias flip
