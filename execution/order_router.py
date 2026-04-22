"""Idempotent order router.

Coordinates:
- deterministic client_order_id generation
- insert-pending in OrderRepo
- call adapter primitive
- persist OrderResult
- refuse duplicate submit (same client_order_id seen in repo with non-pending status)

This is the ONE path from decision -> exchange. No component bypasses it.
"""
from __future__ import annotations

import uuid
from typing import Optional

from core.logging import get_logger
from core.types import OrderIntent, OrderResult, OrderStatus, Side
from execution.hyperliquid_adapter import HyperliquidAdapter
from persistence.repositories import OrderRepo

logger = get_logger(__name__)


def make_order_id(prefix: str, symbol: str) -> str:
    return f"{prefix}-{symbol}-{uuid.uuid4().hex[:10]}"


class OrderRouter:
    def __init__(self, adapter: HyperliquidAdapter, order_repo: OrderRepo,
                 dry_run: bool = False):
        self.adapter = adapter
        self.repo = order_repo
        self.dry_run = dry_run

    def _check_duplicate(self, intent: OrderIntent) -> Optional[OrderResult]:
        existing = self.repo.get(intent.client_order_id)
        if existing and existing.get("status") not in (None, "pending"):
            logger.warning(
                "order_duplicate coid=%s status=%s",
                intent.client_order_id, existing.get("status"),
            )
            return OrderResult(
                client_order_id=intent.client_order_id,
                status=OrderStatus(existing["status"]),
                exchange_order_id=existing.get("exchange_order_id"),
                filled_size=float(existing.get("filled_size") or 0),
                avg_price=existing.get("avg_price"),
                raw=existing.get("raw") or {},
                error="duplicate_submit_ignored",
            )
        return None

    def submit(self, intent: OrderIntent) -> OrderResult:
        dup = self._check_duplicate(intent)
        if dup:
            return dup

        self.repo.insert_pending(intent)
        if self.dry_run:
            result = OrderResult(
                client_order_id=intent.client_order_id,
                status=OrderStatus.FILLED,
                exchange_order_id="dry-run",
                filled_size=intent.size,
                avg_price=intent.price,
                raw={"dry_run": True, "intent": intent.__dict__},
            )
        else:
            if intent.order_kind == "market":
                if intent.reduce_only:
                    result = self.adapter.market_out(intent)
                else:
                    result = self.adapter.market_in(intent)
            elif intent.order_kind in ("stop", "take_profit"):
                kind = "sl" if intent.order_kind == "stop" else "tp"
                result = self.adapter.place_trigger(intent, kind=kind)
            else:
                result = OrderResult(
                    client_order_id=intent.client_order_id,
                    status=OrderStatus.ERROR,
                    exchange_order_id=None,
                    filled_size=0.0,
                    avg_price=None,
                    raw={},
                    error=f"unknown_order_kind:{intent.order_kind}",
                )
        self.repo.update_result(result)
        logger.info(
            "order_routed coid=%s status=%s symbol=%s kind=%s reduce_only=%s",
            result.client_order_id, result.status.value, intent.symbol,
            intent.order_kind, intent.reduce_only,
        )
        return result
