"""The runtime orchestrator: one full cycle, end-to-end.

Flow per cycle:
    1. fetch frames   (market/indicators)
    2. fetch exchange state + local open positions
    3. build PositionSnapshots (reconcile)
    4. build RiskState
    5. build ExternalContext (sentiment, news, forecasts)
    6. build DecisionPacket
    7. lifecycle.manage (deterministic SL/TP/trailing + force-close)
    8. decision kernel (LLM proposal + hard rules)
    9. risk gate + sizing
   10. place orders (entry + attach initial SL/TP)
   11. persist decision + drift check + reconcile
"""
from __future__ import annotations

import traceback
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from core.config import Settings
from core.logging import get_logger
from core.types import (
    ActionSpace,
    DecisionPacket,
    ExternalContext,
    MarketFrame,
    Operation,
    OrderIntent,
    OrderStatus,
    PositionSnapshot,
    RiskState,
    Side,
    TradeDecision,
    utcnow,
)
from decision.decision_inputs import build_decision_packet
from decision.kernel import LLMClient, decide
from execution.hyperliquid_adapter import HyperliquidAdapter
from execution.order_router import OrderRouter, make_order_id
from execution.stop_loss import initial_plan
from lifecycle.lifecycle_manager import LifecycleManager
from lifecycle.position_state import build_position_snapshots
from lifecycle.reconciler import Reconciler
from market.market_state import build_all_frames
from persistence.db import init_persistence, get_connection
from persistence.repositories import (
    DecisionRepo,
    OrderRepo,
    PositionRepo,
    ReconcileRepo,
)
from risk.risk_gate import check_close, check_open
from risk.sizing import size_position
from telemetry.events import TelemetryEmitter

logger = get_logger(__name__)


class Orchestrator:
    def __init__(self, settings: Settings,
                 adapter: Optional[HyperliquidAdapter] = None,
                 llm: Optional[LLMClient] = None,
                 db_factory=None):
        self.settings = settings
        self.adapter = adapter
        self.llm = llm
        self._db_factory = db_factory or (lambda: _default_conn_factory())
        self.position_repo = PositionRepo(self._db_factory)
        self.order_repo = OrderRepo(self._db_factory)
        self.decision_repo = DecisionRepo(self._db_factory)
        self.reconcile_repo = ReconcileRepo(self._db_factory)
        self.router = OrderRouter(adapter, self.order_repo, dry_run=settings.dry_run)
        self.lifecycle = LifecycleManager(settings, self.router, self.position_repo)
        self.reconciler = Reconciler(self.position_repo, self.reconcile_repo)
        self.telemetry = TelemetryEmitter(self.position_repo)

    # --------------------------------------------------------------
    def bootstrap(self) -> None:
        init_persistence()
        if self.adapter is None:
            logger.warning("no_adapter_bootstrap_skipped")
            return
        ex_positions = self.adapter.fetch_positions()
        local = self.position_repo.get_open()
        # build once to force reconcile side-effects; result not kept
        build_position_snapshots(
            ex_positions, local, frames={},
            position_repo=self.position_repo,
            reconcile_repo=self.reconcile_repo,
        )
        logger.info("bootstrap_done exchange_positions=%d local_open=%d",
                    len(ex_positions), len(local))

    # --------------------------------------------------------------
    def run_cycle(
        self,
        raw_indicators: List[Dict],
        external: ExternalContext,
        balance_usd: Optional[float] = None,
    ) -> Dict:
        run_id = f"cycle-{utcnow().strftime('%Y%m%dT%H%M%S')}"
        logger.info("cycle_start run_id=%s", run_id)

        frames: Dict[str, MarketFrame] = build_all_frames(raw_indicators)

        # --- state ---
        if self.adapter:
            ex_positions = self.adapter.fetch_positions()
            balance = balance_usd if balance_usd is not None else self.adapter.account_value()
        else:
            ex_positions, balance = [], (balance_usd or 0.0)

        local = self.position_repo.get_open()
        positions = build_position_snapshots(
            ex_positions, local, frames,
            position_repo=self.position_repo,
            reconcile_repo=self.reconcile_repo,
        )

        risk = self._compute_risk(balance, positions)
        packet = build_decision_packet(
            settings=self.settings,
            balance_usd=balance,
            frames=frames,
            positions=positions,
            risk=risk,
            external=external,
        )

        # --- lifecycle first (can force-close & update SL/TP) ---
        mgmt_actions = self.lifecycle.manage(positions, frames)

        # --- decision ---
        decision, llm_raw = decide(packet, self.settings, self.llm)
        self.decision_repo.save(
            run_id=packet.run_id,
            packet=packet.compact_dict(),
            llm_output=llm_raw,
            decision=decision,
        )

        exec_result = {}
        if decision.operation == Operation.OPEN:
            exec_result = self._execute_open(packet, decision)
        elif decision.operation == Operation.CLOSE:
            exec_result = self._execute_close(packet, decision)
        else:
            exec_result = {"operation": "hold", "reason": decision.reason}

        # --- drift detection after all actions ---
        drift = []
        if self.adapter:
            try:
                latest_ex = self.adapter.fetch_positions()
                drift = self.reconciler.detect_drift(latest_ex, positions)
            except Exception as e:
                logger.warning("drift_detection_failed err=%s", e)

        return {
            "run_id": packet.run_id,
            "decision": decision.to_dict(),
            "execution": exec_result,
            "mgmt_actions": [asdict(a) for a in mgmt_actions],
            "drift": drift,
        }

    # --------------------------------------------------------------
    def _compute_risk(self, balance: float, positions: Dict[str, PositionSnapshot]) -> RiskState:
        open_count = len(positions)
        used_margin = sum(p.size * p.entry_price / max(p.leverage, 1) for p in positions.values())
        exposure_ratio = used_margin / balance if balance > 0 else 0.0
        daily_pnl, daily_pnl_pct = self._fetch_daily_pnl(balance)
        consec_losses = self._consecutive_losses()
        cooldown = False
        cooldown_reason = None
        if daily_pnl_pct <= -self.settings.risk.daily_loss_halt_pct:
            cooldown = True
            cooldown_reason = f"daily_loss_halt:{daily_pnl_pct:.3f}"
        elif consec_losses >= self.settings.risk.consecutive_loss_halt:
            cooldown = True
            cooldown_reason = f"consec_losses:{consec_losses}"
        return RiskState(
            balance_usd=balance,
            available_usd=max(0.0, balance - used_margin),
            used_margin=used_margin,
            exposure_ratio=exposure_ratio,
            open_position_count=open_count,
            max_positions=self.settings.risk.max_open_positions,
            consecutive_losses=consec_losses,
            daily_pnl=daily_pnl,
            daily_pnl_pct=daily_pnl_pct,
            cooldown_active=cooldown,
            cooldown_reason=cooldown_reason,
        )

    def _fetch_daily_pnl(self, balance: float) -> tuple[float, float]:
        try:
            with get_connection() as conn, conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COALESCE(SUM(
                        CASE WHEN status='closed' AND closed_at > NOW() - INTERVAL '24 hours'
                             THEN (mfe_pct + mae_pct) * 0  -- placeholder
                        END
                    ), 0)
                    """
                )
                # We don't yet track realized PnL numerically; this is a safe 0 default
                return 0.0, 0.0
        except Exception:
            return 0.0, 0.0

    def _consecutive_losses(self) -> int:
        try:
            with get_connection() as conn, conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT mfe_pct, mae_pct FROM positions
                    WHERE status='closed'
                    ORDER BY closed_at DESC
                    LIMIT 10
                    """
                )
                rows = cur.fetchall() or []
            streak = 0
            for mfe, mae in rows:
                if (float(mae or 0.0)) < (float(mfe or 0.0)) and float(mae or 0.0) < -0.002:
                    streak += 1
                else:
                    break
            return streak
        except Exception:
            return 0

    # --------------------------------------------------------------
    def _execute_open(self, packet: DecisionPacket, decision: TradeDecision) -> Dict:
        frame = packet.market_frames[decision.symbol]
        side = decision.direction
        if side is None:
            return {"status": "error", "reason": "open_without_direction"}

        plan = initial_plan(
            frame, side, self.settings,
            sl_mode=decision.stop_loss_mode,
            tp_mode=decision.take_profit_mode,
            trailing_enabled=decision.trailing_enabled,
        )
        quality = packet.setup_quality[decision.symbol].score
        sizing = size_position(
            balance_usd=packet.balance_usd,
            frame=frame,
            side=side,
            sl_price=plan.sl_price,
            leverage=decision.leverage,
            llm_size_fraction=decision.size_fraction,
            quality_score=quality,
            settings=self.settings,
        )
        verdict = check_open(packet, decision, sizing, plan, self.settings)
        if not verdict.approved:
            return {"status": "blocked_by_risk", "reason": verdict.reason}

        size_units = sizing.size_units
        # entry intent
        entry_intent = OrderIntent(
            client_order_id=make_order_id("ent", decision.symbol),
            symbol=decision.symbol,
            side=side,
            size=size_units,
            order_kind="market",
            reduce_only=False,
            price=None,
            leverage=verdict.adjusted_leverage,
            meta={"run_id": packet.run_id, "reason": decision.reason, "sizing": asdict(sizing)},
        )
        entry_res = self.router.submit(entry_intent)
        if entry_res.status not in (OrderStatus.FILLED, OrderStatus.SUBMITTED):
            return {"status": "entry_failed", "result": entry_res.__dict__}

        # register position in local DB
        snap = PositionSnapshot(
            symbol=decision.symbol,
            side=side,
            size=size_units,
            entry_price=entry_res.avg_price or frame.price,
            mark_price=frame.price,
            leverage=float(verdict.adjusted_leverage),
            pnl_usd=0.0,
            pnl_pct=0.0,
            mae_pct=0.0,
            mfe_pct=0.0,
            bars_held=0,
            opened_at=datetime.now(timezone.utc),
            sl_price=plan.sl_price,
            tp_price=plan.tp_price,
            trailing_active=False,
            client_order_id=entry_intent.client_order_id,
        )
        cpid = self.position_repo.upsert_open(snap, meta={"run_id": packet.run_id})
        self.telemetry.on_open(cpid, decision, entry_res)

        # Attach initial SL and TP
        sl_intent = OrderIntent(
            client_order_id=make_order_id("sl", decision.symbol),
            symbol=decision.symbol,
            side=_opposite(side),
            size=size_units,
            order_kind="stop",
            reduce_only=True,
            price=plan.sl_price,
            leverage=None,
            meta={"cpid": cpid, "basis": plan.basis},
        )
        self.router.submit(sl_intent)

        if plan.tp_price is not None:
            tp_intent = OrderIntent(
                client_order_id=make_order_id("tp", decision.symbol),
                symbol=decision.symbol,
                side=_opposite(side),
                size=size_units,
                order_kind="take_profit",
                reduce_only=True,
                price=plan.tp_price,
                leverage=None,
                meta={"cpid": cpid, "basis": plan.basis},
            )
            self.router.submit(tp_intent)

        return {
            "status": "ok",
            "cpid": cpid,
            "entry": entry_res.__dict__,
            "sl_price": plan.sl_price,
            "tp_price": plan.tp_price,
            "sizing": asdict(sizing),
        }

    def _execute_close(self, packet: DecisionPacket, decision: TradeDecision) -> Dict:
        verdict = check_close(packet, decision)
        if not verdict.approved:
            return {"status": "blocked_by_risk", "reason": verdict.reason}
        pos = packet.positions[decision.symbol]
        intent = OrderIntent(
            client_order_id=make_order_id("cls", decision.symbol),
            symbol=decision.symbol,
            side=_opposite(pos.side),
            size=pos.size,
            order_kind="market",
            reduce_only=True,
            price=None,
            leverage=None,
            meta={"run_id": packet.run_id, "reason": decision.reason},
        )
        res = self.router.submit(intent)
        if pos.client_order_id:
            self.position_repo.mark_closed(pos.client_order_id, reason=decision.reason)
            self.telemetry.on_close(pos.client_order_id, decision.reason, res)
        return {"status": "ok", "order": res.__dict__}


def _opposite(side: Side) -> Side:
    return Side.SHORT if side == Side.LONG else Side.LONG


def _default_conn_factory():
    from persistence.db import get_dsn
    import psycopg2  # type: ignore
    dsn = get_dsn()
    if not dsn:
        raise RuntimeError("DATABASE_URL not set")
    return psycopg2.connect(dsn)
