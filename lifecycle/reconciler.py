"""Reconciler — bootstrap + continuous drift detection.

Flow:
- bootstrap(): called once at startup. Fetch exchange positions + local rows.
  Emit reconcile_log events for discovered/missing. Position state builder in
  `position_state.py` is the source of truth going forward.

- detect_drift(): after every cycle, compares final local snapshots to
  exchange state; if there's a size delta > tolerance, logs a drift event.
"""
from __future__ import annotations

from typing import Dict, List

from core.logging import get_logger
from core.types import PositionSnapshot
from persistence.repositories import PositionRepo, ReconcileRepo

logger = get_logger(__name__)


class Reconciler:
    def __init__(self, position_repo: PositionRepo, reconcile_repo: ReconcileRepo,
                 size_tolerance_pct: float = 0.005):
        self.positions = position_repo
        self.reconcile = reconcile_repo
        self.tol = size_tolerance_pct

    def detect_drift(self, exchange_positions: List[Dict],
                     snapshots: Dict[str, PositionSnapshot]) -> List[Dict]:
        drifts: List[Dict] = []
        by_exchange = {p["symbol"]: p for p in exchange_positions}
        for symbol, snap in snapshots.items():
            ex = by_exchange.get(symbol)
            if ex is None:
                drifts.append({"symbol": symbol, "kind": "missing_exchange"})
                self.reconcile.log(symbol, "missing_exchange", {"local_size": snap.size})
                continue
            delta = abs(float(ex["size"]) - snap.size)
            rel = delta / max(snap.size, 1e-9)
            if rel > self.tol:
                detail = {"local_size": snap.size, "exchange_size": ex["size"], "rel_delta": rel}
                self.reconcile.log(symbol, "drift", detail)
                drifts.append({"symbol": symbol, "kind": "drift", **detail})
        return drifts
