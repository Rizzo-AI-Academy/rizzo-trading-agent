"""Lifecycle manager: reacts only when the plan changes.

Uses a FakeRouter + FakeRepo to assert SL updates, force-close triggers
and no premature winner-killing.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

from core.config import Settings
from core.types import OrderIntent, OrderResult, OrderStatus, Side
from lifecycle.lifecycle_manager import LifecycleManager
from tests.conftest import make_frame, make_position


class FakeRouter:
    def __init__(self):
        self.submitted: List[OrderIntent] = []

    def submit(self, intent: OrderIntent) -> OrderResult:
        self.submitted.append(intent)
        return OrderResult(
            client_order_id=intent.client_order_id,
            status=OrderStatus.SUBMITTED,
            exchange_order_id="x",
            filled_size=0.0, avg_price=None, raw={},
        )


class FakeRepo:
    def __init__(self):
        self.calls = []

    def update_excursion(self, cpid, mfe, mae, bars): self.calls.append(("exc", cpid, mfe, mae, bars))
    def update_sl_tp(self, cpid, sl, tp, trailing_active, basis, reason):
        self.calls.append(("sltp", cpid, sl, tp, trailing_active, basis, reason))
    def mark_closed(self, cpid, reason): self.calls.append(("closed", cpid, reason))
    def log_event(self, cpid, ev, p): self.calls.append(("event", cpid, ev))


def _mgr(settings: Settings):
    router = FakeRouter()
    repo = FakeRepo()
    return LifecycleManager(settings, router, repo), router, repo  # type: ignore[arg-type]


def test_no_action_when_trailing_not_ready(settings):
    mgr, router, repo = _mgr(settings)
    pos = make_position("BTC", Side.LONG, entry=100, mark=100.2, sl=98.0)
    frame = make_frame("BTC", price=100.2, atr_pct=0.01)
    actions = mgr.manage({"BTC": pos}, {"BTC": frame})
    assert actions == []
    assert not any(i.order_kind == "stop" for i in router.submitted)


def test_trailing_moves_sl_forward(settings):
    mgr, router, repo = _mgr(settings)
    pos = make_position("BTC", Side.LONG, entry=100, mark=103.0, sl=98.0)
    frame = make_frame("BTC", price=103.0, atr_pct=0.01)
    actions = mgr.manage({"BTC": pos}, {"BTC": frame})
    assert any(a.kind == "sl_update" for a in actions)
    # a new stop order was submitted
    assert any(i.order_kind == "stop" for i in router.submitted)


def test_force_close_on_structure_flip_when_losing(settings):
    mgr, router, repo = _mgr(settings)
    pos = make_position("BTC", Side.LONG, entry=100, mark=98.0, sl=97.0)
    frame = make_frame("BTC", price=98.0, bias=Side.SHORT)
    actions = mgr.manage({"BTC": pos}, {"BTC": frame})
    assert any(a.kind == "force_close" for a in actions)
    assert any(i.order_kind == "market" and i.reduce_only for i in router.submitted)


def test_winner_not_force_closed_on_single_bar_flip(settings):
    mgr, router, repo = _mgr(settings)
    pos = make_position("BTC", Side.LONG, entry=100, mark=102.5, sl=99.5)
    frame = make_frame("BTC", price=102.5, bias=Side.SHORT)
    actions = mgr.manage({"BTC": pos}, {"BTC": frame})
    assert not any(a.kind == "force_close" for a in actions)
