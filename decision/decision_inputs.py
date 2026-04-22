"""Decision Inputs assembler.

This is the ONLY place where external pieces (market frames, position state,
risk state, external context, setup quality, action space) are glued into a
single immutable DecisionPacket. Downstream code is forbidden from reassembling
this data: if something is missing from a packet, the fix happens here.
"""
from __future__ import annotations

import uuid
from typing import Dict, List, Optional

from core.config import Settings
from core.logging import get_logger
from core.types import (
    ActionSpace,
    DecisionPacket,
    ExternalContext,
    MarketFrame,
    PositionSnapshot,
    RiskState,
    SetupQuality,
    Side,
    utcnow,
)
from decision.action_space import build_action_space
from decision.setup_quality import score_all

logger = get_logger(__name__)


def new_run_id() -> str:
    return f"run-{utcnow().strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:6]}"


def _intended_sides_for_scoring(
    frames: Dict[str, MarketFrame],
    positions: Dict[str, PositionSnapshot],
) -> Dict[str, Side | None]:
    """When scoring setup quality we align the intended side with the
    structural bias of the frame — unless a position is already open, in
    which case we score from the position's perspective (i.e. 'is it still
    a good setup to hold?')."""
    out: Dict[str, Side | None] = {}
    for t, f in frames.items():
        if t in positions:
            out[t] = positions[t].side
        else:
            out[t] = f.structure_bias
    return out


def build_decision_packet(
    *,
    settings: Settings,
    balance_usd: float,
    frames: Dict[str, MarketFrame],
    positions: Dict[str, PositionSnapshot],
    risk: RiskState,
    external: ExternalContext,
    recent_closes_bars: Optional[Dict[str, int]] = None,
) -> DecisionPacket:
    intended = _intended_sides_for_scoring(frames, positions)
    quality = score_all(frames, settings, intended)
    action_space: ActionSpace = build_action_space(
        frames=frames,
        positions=positions,
        quality=quality,
        risk=risk,
        settings=settings,
        recent_closes=recent_closes_bars,
    )

    packet = DecisionPacket(
        ts=utcnow(),
        balance_usd=balance_usd,
        tickers=list(frames.keys()),
        market_frames=frames,
        positions=positions,
        risk=risk,
        setup_quality=quality,
        external=external,
        action_space=action_space,
        run_id=new_run_id(),
    )

    logger.info(
        "decision_packet_ready run_id=%s tickers=%s positions=%s allowed=%s",
        packet.run_id,
        list(frames.keys()),
        list(positions.keys()),
        {k: v for k, v in action_space.allowed.items()},
    )
    return packet
