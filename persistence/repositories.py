"""Repositories encapsulate all SQL for the refactored domain.

Each repo accepts a connection factory so callers can inject a transaction
or a fake. Connections are per-call to keep tests simple.
"""
from __future__ import annotations

import json
import uuid
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterator, List, Optional

try:
    from psycopg2.extras import Json  # type: ignore
except Exception:  # pragma: no cover
    def Json(x):  # type: ignore
        return json.dumps(x)

from core.logging import get_logger
from core.types import (
    OrderIntent,
    OrderResult,
    OrderStatus,
    PositionSnapshot,
    Side,
    TradeDecision,
)

logger = get_logger(__name__)


ConnFactory = Callable[[], Any]


@contextmanager
def _conn(factory: ConnFactory) -> Iterator[Any]:
    from contextlib import closing
    with closing(factory()) as conn:
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise


class PositionRepo:
    def __init__(self, conn_factory: ConnFactory):
        self._cf = conn_factory

    @staticmethod
    def new_client_position_id(symbol: str) -> str:
        return f"pos-{symbol}-{uuid.uuid4().hex[:10]}"

    def upsert_open(self, snap: PositionSnapshot, meta: Dict[str, Any]) -> str:
        cpid = snap.client_order_id or self.new_client_position_id(snap.symbol)
        with _conn(self._cf) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO positions (
                    client_position_id, symbol, side, size, entry_price,
                    leverage, sl_price, tp_price, trailing_active,
                    mfe_pct, mae_pct, bars_held, status, opened_at, meta
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'open',%s,%s)
                ON CONFLICT (client_position_id) DO UPDATE
                SET size = EXCLUDED.size,
                    sl_price = EXCLUDED.sl_price,
                    tp_price = EXCLUDED.tp_price,
                    trailing_active = EXCLUDED.trailing_active,
                    mfe_pct = EXCLUDED.mfe_pct,
                    mae_pct = EXCLUDED.mae_pct,
                    bars_held = EXCLUDED.bars_held,
                    updated_at = NOW();
                """,
                (
                    cpid, snap.symbol, snap.side.value, snap.size, snap.entry_price,
                    snap.leverage, snap.sl_price, snap.tp_price, snap.trailing_active,
                    snap.mfe_pct, snap.mae_pct, snap.bars_held, snap.opened_at, Json(meta),
                ),
            )
        return cpid

    def update_sl_tp(self, cpid: str, sl: Optional[float], tp: Optional[float],
                     trailing_active: bool, basis: str, reason: str) -> None:
        with _conn(self._cf) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT sl_price, tp_price FROM positions WHERE client_position_id=%s",
                (cpid,),
            )
            row = cur.fetchone()
            if not row:
                return
            old_sl, old_tp = row
            cur.execute(
                """
                UPDATE positions
                SET sl_price=%s, tp_price=%s, trailing_active=%s, updated_at=NOW()
                WHERE client_position_id=%s
                """,
                (sl, tp, trailing_active, cpid),
            )
            if sl is not None and (old_sl is None or float(old_sl) != float(sl)):
                cur.execute(
                    """
                    INSERT INTO sl_tp_updates (client_position_id, field, old_price, new_price, basis, reason)
                    VALUES (%s,'sl',%s,%s,%s,%s)
                    """,
                    (cpid, old_sl, sl, basis, reason),
                )
            if tp is not None and (old_tp is None or float(old_tp) != float(tp)):
                cur.execute(
                    """
                    INSERT INTO sl_tp_updates (client_position_id, field, old_price, new_price, basis, reason)
                    VALUES (%s,'tp',%s,%s,%s,%s)
                    """,
                    (cpid, old_tp, tp, basis, reason),
                )

    def update_excursion(self, cpid: str, mfe_pct: float, mae_pct: float, bars_held: int) -> None:
        with _conn(self._cf) as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE positions
                SET mfe_pct = GREATEST(mfe_pct, %s),
                    mae_pct = LEAST(mae_pct, %s),
                    bars_held = %s,
                    updated_at = NOW()
                WHERE client_position_id = %s
                """,
                (mfe_pct, mae_pct, bars_held, cpid),
            )

    def mark_closed(self, cpid: str, reason: str) -> None:
        with _conn(self._cf) as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE positions
                SET status='closed', closed_at=NOW(), close_reason=%s, updated_at=NOW()
                WHERE client_position_id=%s AND status='open'
                """,
                (reason, cpid),
            )

    def get_open(self) -> List[Dict[str, Any]]:
        with _conn(self._cf) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT client_position_id, symbol, side, size, entry_price, leverage,
                       sl_price, tp_price, trailing_active, mfe_pct, mae_pct,
                       bars_held, opened_at, meta
                FROM positions WHERE status='open'
                """
            )
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]

    def log_event(self, cpid: str, event_type: str, payload: Dict[str, Any]) -> None:
        with _conn(self._cf) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO position_events (client_position_id, event_type, payload)
                VALUES (%s,%s,%s)
                """,
                (cpid, event_type, Json(payload)),
            )


class OrderRepo:
    def __init__(self, conn_factory: ConnFactory):
        self._cf = conn_factory

    def insert_pending(self, intent: OrderIntent) -> None:
        with _conn(self._cf) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO orders (
                    client_order_id, symbol, side, size, order_kind, reduce_only,
                    trigger_price, leverage, status, raw
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'pending',%s)
                ON CONFLICT (client_order_id) DO NOTHING
                """,
                (
                    intent.client_order_id, intent.symbol, intent.side.value, intent.size,
                    intent.order_kind, intent.reduce_only, intent.price, intent.leverage,
                    Json(intent.meta),
                ),
            )

    def update_result(self, result: OrderResult) -> None:
        with _conn(self._cf) as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE orders
                SET status=%s, exchange_order_id=%s, filled_size=%s, avg_price=%s,
                    error=%s, raw=%s, updated_at=NOW()
                WHERE client_order_id=%s
                """,
                (
                    result.status.value, result.exchange_order_id, result.filled_size,
                    result.avg_price, result.error, Json(result.raw),
                    result.client_order_id,
                ),
            )

    def get(self, client_order_id: str) -> Optional[Dict[str, Any]]:
        with _conn(self._cf) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM orders WHERE client_order_id=%s", (client_order_id,)
            )
            row = cur.fetchone()
            if not row:
                return None
            cols = [d[0] for d in cur.description]
            return dict(zip(cols, row))


class DecisionRepo:
    def __init__(self, conn_factory: ConnFactory):
        self._cf = conn_factory

    def save(self, run_id: str, packet: Dict[str, Any], llm_output: Optional[Dict[str, Any]],
             decision: TradeDecision) -> int:
        with _conn(self._cf) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO decisions (run_id, packet, llm_output, final_decision, deterministic_override)
                VALUES (%s,%s,%s,%s,%s) RETURNING id
                """,
                (
                    run_id, Json(packet),
                    Json(llm_output) if llm_output is not None else None,
                    Json(decision.to_dict()),
                    decision.deterministic_override,
                ),
            )
            return cur.fetchone()[0]


class ReconcileRepo:
    def __init__(self, conn_factory: ConnFactory):
        self._cf = conn_factory

    def log(self, symbol: Optional[str], kind: str, detail: Dict[str, Any]) -> None:
        with _conn(self._cf) as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO reconcile_log (symbol, kind, detail) VALUES (%s,%s,%s)",
                (symbol, kind, Json(detail)),
            )
