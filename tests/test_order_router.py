"""Order router: idempotency + dry-run path, using a fake repo + fake adapter."""
from __future__ import annotations

from core.types import OrderIntent, OrderResult, OrderStatus, Side
from execution.order_router import OrderRouter, make_order_id


class FakeRepo:
    def __init__(self):
        self.rows = {}

    def insert_pending(self, intent: OrderIntent) -> None:
        self.rows.setdefault(intent.client_order_id, {
            "client_order_id": intent.client_order_id,
            "status": "pending", "filled_size": 0, "raw": {},
        })

    def update_result(self, result: OrderResult) -> None:
        self.rows[result.client_order_id] = {
            "client_order_id": result.client_order_id,
            "status": result.status.value,
            "exchange_order_id": result.exchange_order_id,
            "filled_size": result.filled_size,
            "avg_price": result.avg_price,
            "error": result.error,
            "raw": result.raw,
        }

    def get(self, coid: str):
        return self.rows.get(coid)


class FakeAdapter:
    def market_in(self, intent: OrderIntent) -> OrderResult:
        return OrderResult(
            client_order_id=intent.client_order_id,
            status=OrderStatus.FILLED,
            exchange_order_id="x1",
            filled_size=intent.size,
            avg_price=100.0,
            raw={"ok": True},
        )

    def market_out(self, intent: OrderIntent) -> OrderResult:
        return OrderResult(
            client_order_id=intent.client_order_id,
            status=OrderStatus.FILLED,
            exchange_order_id="x2",
            filled_size=intent.size,
            avg_price=None,
            raw={"ok": True},
        )

    def place_trigger(self, intent: OrderIntent, kind: str) -> OrderResult:
        return OrderResult(
            client_order_id=intent.client_order_id,
            status=OrderStatus.SUBMITTED,
            exchange_order_id="t1",
            filled_size=0.0,
            avg_price=None,
            raw={"kind": kind},
        )


def _intent(kind="market", reduce_only=False, coid=None):
    return OrderIntent(
        client_order_id=coid or make_order_id("ent", "BTC"),
        symbol="BTC",
        side=Side.LONG,
        size=0.01,
        order_kind=kind,
        reduce_only=reduce_only,
        price=99.0 if kind == "stop" else None,
        leverage=2,
    )


def test_router_happy_path_market():
    repo = FakeRepo()
    router = OrderRouter(FakeAdapter(), repo)  # type: ignore[arg-type]
    result = router.submit(_intent())
    assert result.status == OrderStatus.FILLED


def test_router_idempotent_duplicate():
    repo = FakeRepo()
    router = OrderRouter(FakeAdapter(), repo)  # type: ignore[arg-type]
    coid = make_order_id("ent", "BTC")
    first = router.submit(_intent(coid=coid))
    second = router.submit(_intent(coid=coid))
    assert second.error == "duplicate_submit_ignored"
    assert second.status == first.status


def test_router_dry_run_bypasses_adapter():
    repo = FakeRepo()
    router = OrderRouter(FakeAdapter(), repo, dry_run=True)  # type: ignore[arg-type]
    result = router.submit(_intent(kind="stop"))
    assert result.status == OrderStatus.FILLED
    assert result.raw.get("dry_run") is True
