"""Lifecycle manager — deterministic active management of open positions.

Runs BEFORE the LLM is consulted each cycle. Responsibilities:
1. For each open position:
   - update MFE/MAE, bars_held (in PositionRepo)
   - evaluate new SL/TP plan (stop_loss.update_plan)
   - if plan changes SL -> cancel old stop, place new stop, log sl_tp_update
   - if plan changes TP -> same
   - if should_force_close -> emit a deterministic close intent
2. Return a list of additional TradeDecision-like actions the orchestrator
   must execute regardless of the LLM.

This module is the ONLY place that actively moves SLs and TPs. The LLM has
no authority over SL/TP levels — it only selects SL/TP MODE at open time.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

from core.config import Settings
from core.logging import get_logger
from core.types import (
    MarketFrame,
    Operation,
    OrderIntent,
    PositionSnapshot,
    Side,
    StopLossPlan,
    TradeDecision,
)
from execution.order_router import OrderRouter, make_order_id
from execution.stop_loss import should_force_close, update_plan
from persistence.repositories import PositionRepo

logger = get_logger(__name__)


@dataclass
class ManagementAction:
    kind: str                    # 'sl_update' | 'tp_update' | 'force_close'
    symbol: str
    reason: str
    new_sl: Optional[float] = None
    new_tp: Optional[float] = None


def _opposite(side: Side) -> Side:
    return Side.SHORT if side == Side.LONG else Side.LONG


class LifecycleManager:
    def __init__(self, settings: Settings, router: OrderRouter, repo: PositionRepo):
        self.settings = settings
        self.router = router
        self.repo = repo

    def manage(
        self,
        positions: Dict[str, PositionSnapshot],
        frames: Dict[str, MarketFrame],
    ) -> List[ManagementAction]:
        actions: List[ManagementAction] = []

        for symbol, pos in positions.items():
            frame = frames.get(symbol)
            if frame is None or pos.client_order_id is None:
                continue

            # Update MFE/MAE/bars_held persistently
            self.repo.update_excursion(
                pos.client_order_id, pos.mfe_pct, pos.mae_pct, pos.bars_held,
            )

            # Structural force-close
            force_reason = should_force_close(pos, frame)
            if force_reason:
                self._force_close(pos)
                actions.append(ManagementAction(
                    kind="force_close", symbol=symbol, reason=force_reason,
                ))
                continue

            # Dynamic SL/TP update
            new_plan = update_plan(pos, frame, self.settings)
            if new_plan is None:
                continue

            sl_changed = pos.sl_price is None or abs(new_plan.sl_price - (pos.sl_price or 0.0)) > 1e-12
            tp_changed = (new_plan.tp_price is not None
                          and (pos.tp_price is None or abs(new_plan.tp_price - pos.tp_price) > 1e-12))

            if sl_changed:
                self._replace_stop(pos, new_plan)
                actions.append(ManagementAction(
                    kind="sl_update", symbol=symbol,
                    reason=",".join(new_plan.notes) or "trailing",
                    new_sl=new_plan.sl_price,
                ))
            if tp_changed:
                self._replace_tp(pos, new_plan)
                actions.append(ManagementAction(
                    kind="tp_update", symbol=symbol,
                    reason="plan_update", new_tp=new_plan.tp_price,
                ))

            if sl_changed or tp_changed:
                self.repo.update_sl_tp(
                    pos.client_order_id, new_plan.sl_price, new_plan.tp_price,
                    trailing_active=True, basis=new_plan.basis,
                    reason=",".join(new_plan.notes) or "trailing",
                )

        return actions

    # --- helpers ---
    def _force_close(self, pos: PositionSnapshot) -> None:
        intent = OrderIntent(
            client_order_id=make_order_id("cls", pos.symbol),
            symbol=pos.symbol,
            side=_opposite(pos.side),
            size=pos.size,
            order_kind="market",
            reduce_only=True,
            price=None,
            leverage=None,
            meta={"trigger": "lifecycle_force_close", "cpid": pos.client_order_id},
        )
        res = self.router.submit(intent)
        self.repo.mark_closed(pos.client_order_id, reason="lifecycle_force_close")
        self.repo.log_event(pos.client_order_id, "force_close", {
            "order_result": res.__dict__,
        })

    def _replace_stop(self, pos: PositionSnapshot, plan: StopLossPlan) -> None:
        # place new SL first, then best-effort cancel old is done by exchange
        # side of the SL order is opposite of position side (reduce_only)
        intent = OrderIntent(
            client_order_id=make_order_id("sl", pos.symbol),
            symbol=pos.symbol,
            side=_opposite(pos.side),
            size=pos.size,
            order_kind="stop",
            reduce_only=True,
            price=plan.sl_price,
            leverage=None,
            meta={"cpid": pos.client_order_id, "basis": plan.basis},
        )
        self.router.submit(intent)

    def _replace_tp(self, pos: PositionSnapshot, plan: StopLossPlan) -> None:
        if plan.tp_price is None:
            return
        intent = OrderIntent(
            client_order_id=make_order_id("tp", pos.symbol),
            symbol=pos.symbol,
            side=_opposite(pos.side),
            size=pos.size,
            order_kind="take_profit",
            reduce_only=True,
            price=plan.tp_price,
            leverage=None,
            meta={"cpid": pos.client_order_id, "basis": plan.basis},
        )
        self.router.submit(intent)
