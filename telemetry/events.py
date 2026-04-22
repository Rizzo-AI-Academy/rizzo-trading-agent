"""Trade event helpers. Thin wrappers that write into position_events
with a canonical schema so replay + audit tooling works without guessing.
"""
from __future__ import annotations

from typing import Any, Dict

from core.types import OrderResult, PositionSnapshot, TradeDecision
from persistence.repositories import PositionRepo


class TelemetryEmitter:
    def __init__(self, repo: PositionRepo):
        self.repo = repo

    def on_open(self, cpid: str, decision: TradeDecision, result: OrderResult) -> None:
        self.repo.log_event(cpid, "open", {
            "decision": decision.to_dict(),
            "order": {
                "client_order_id": result.client_order_id,
                "status": result.status.value,
                "filled_size": result.filled_size,
                "avg_price": result.avg_price,
            },
        })

    def on_close(self, cpid: str, reason: str, result: OrderResult) -> None:
        self.repo.log_event(cpid, "close", {
            "reason": reason,
            "order": {
                "client_order_id": result.client_order_id,
                "status": result.status.value,
            },
        })

    def on_trail(self, cpid: str, payload: Dict[str, Any]) -> None:
        self.repo.log_event(cpid, "trailing_update", payload)
