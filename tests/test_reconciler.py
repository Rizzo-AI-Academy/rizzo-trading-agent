"""Drift detection: local state vs exchange state."""
from __future__ import annotations

from lifecycle.reconciler import Reconciler
from tests.conftest import make_position


class FakeReconcileRepo:
    def __init__(self):
        self.events = []

    def log(self, symbol, kind, detail):
        self.events.append({"symbol": symbol, "kind": kind, "detail": detail})


class FakePosRepo:
    def mark_closed(self, *_, **__): pass
    def log_event(self, *_, **__): pass
    def upsert_open(self, *_, **__): return "cpid"
    @staticmethod
    def new_client_position_id(symbol):
        return f"pos-{symbol}-test"


def test_drift_detects_missing_on_exchange():
    repo = FakeReconcileRepo()
    rec = Reconciler(FakePosRepo(), repo)  # type: ignore[arg-type]
    snap = make_position("BTC")
    drift = rec.detect_drift([], {"BTC": snap})
    assert len(drift) == 1
    assert drift[0]["kind"] == "missing_exchange"


def test_drift_detects_size_delta():
    repo = FakeReconcileRepo()
    rec = Reconciler(FakePosRepo(), repo, size_tolerance_pct=0.005)  # type: ignore[arg-type]
    snap = make_position("BTC", size=1.0)
    ex = [{"symbol": "BTC", "size": 2.0, "side": "long", "entry_price": 100, "mark_price": 101}]
    drift = rec.detect_drift(ex, {"BTC": snap})
    assert len(drift) == 1
    assert drift[0]["kind"] == "drift"


def test_no_drift_when_within_tolerance():
    repo = FakeReconcileRepo()
    rec = Reconciler(FakePosRepo(), repo, size_tolerance_pct=0.01)  # type: ignore[arg-type]
    snap = make_position("BTC", size=1.000)
    ex = [{"symbol": "BTC", "size": 1.005, "side": "long", "entry_price": 100, "mark_price": 101}]
    drift = rec.detect_drift(ex, {"BTC": snap})
    assert drift == []
