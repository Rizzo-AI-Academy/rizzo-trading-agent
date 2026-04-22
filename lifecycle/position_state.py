"""Canonical PositionSnapshot builder.

Merges authoritative data from three sources:
1. Exchange (size, entry, mark, leverage)   -> truth for existence
2. Local DB (client_position_id, sl/tp, trailing, bars_held, mfe/mae, opened_at)
3. Current frame (to compute pnl_pct)

If the exchange reports a size for a symbol but the DB has no matching open
row, we create one (discovered position) and mark a reconcile event; the
reverse triggers a 'closed-externally' event.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, List

from core.logging import get_logger
from core.types import MarketFrame, PositionSnapshot, Side
from persistence.repositories import PositionRepo, ReconcileRepo

logger = get_logger(__name__)


def build_position_snapshots(
    exchange_positions: List[Dict],
    local_open: List[Dict],
    frames: Dict[str, MarketFrame],
    position_repo: PositionRepo,
    reconcile_repo: ReconcileRepo,
) -> Dict[str, PositionSnapshot]:
    by_exchange = {p["symbol"]: p for p in exchange_positions}
    by_local = {p["symbol"]: p for p in local_open}

    out: Dict[str, PositionSnapshot] = {}

    for symbol, ex in by_exchange.items():
        frame = frames.get(symbol)
        local = by_local.get(symbol)
        side = Side(ex["side"])
        entry = float(ex["entry_price"])
        mark = float(ex["mark_price"])
        size = float(ex["size"])
        if frame is not None:
            mark = frame.price
        pnl_usd = (mark - entry) * size if side == Side.LONG else (entry - mark) * size
        pnl_pct = (pnl_usd / (entry * size)) if entry and size else 0.0

        if local is None:
            cpid = position_repo.new_client_position_id(symbol)
            reconcile_repo.log(symbol, "missing_local", {"exchange": ex})
            position_repo.upsert_open(
                PositionSnapshot(
                    symbol=symbol,
                    side=side,
                    size=size,
                    entry_price=entry,
                    mark_price=mark,
                    leverage=float(ex.get("leverage_value") or 1),
                    pnl_usd=pnl_usd,
                    pnl_pct=pnl_pct,
                    mae_pct=min(0.0, pnl_pct),
                    mfe_pct=max(0.0, pnl_pct),
                    bars_held=0,
                    opened_at=datetime.now(timezone.utc),
                    sl_price=None,
                    tp_price=None,
                    trailing_active=False,
                    client_order_id=cpid,
                ),
                meta={"source": "reconcile_discovered"},
            )
            local = {
                "client_position_id": cpid,
                "symbol": symbol,
                "side": side.value,
                "size": size,
                "entry_price": entry,
                "leverage": float(ex.get("leverage_value") or 1),
                "sl_price": None,
                "tp_price": None,
                "trailing_active": False,
                "mfe_pct": max(0.0, pnl_pct),
                "mae_pct": min(0.0, pnl_pct),
                "bars_held": 0,
                "opened_at": datetime.now(timezone.utc),
            }

        snap = PositionSnapshot(
            symbol=symbol,
            side=side,
            size=size,
            entry_price=entry,
            mark_price=mark,
            leverage=float(local.get("leverage") or ex.get("leverage_value") or 1),
            pnl_usd=pnl_usd,
            pnl_pct=pnl_pct,
            mae_pct=min(float(local.get("mae_pct") or 0.0), pnl_pct),
            mfe_pct=max(float(local.get("mfe_pct") or 0.0), pnl_pct),
            bars_held=int(local.get("bars_held") or 0) + 1,
            opened_at=local.get("opened_at") or datetime.now(timezone.utc),
            sl_price=float(local["sl_price"]) if local.get("sl_price") is not None else None,
            tp_price=float(local["tp_price"]) if local.get("tp_price") is not None else None,
            trailing_active=bool(local.get("trailing_active") or False),
            client_order_id=local.get("client_position_id"),
        )
        out[symbol] = snap

    # Detect positions present locally but gone on exchange (external close / SL hit)
    for symbol, local in by_local.items():
        if symbol not in by_exchange:
            reconcile_repo.log(symbol, "missing_exchange", {"local": local})
            position_repo.mark_closed(
                local["client_position_id"], reason="closed_externally"
            )
            position_repo.log_event(
                local["client_position_id"],
                "closed_externally",
                {"detected_at": datetime.now(timezone.utc).isoformat()},
            )

    return out
