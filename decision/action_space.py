"""Deterministic action-space builder.

Given current state + frames, returns the set of operations that are even
*syntactically* allowed before anything else runs. This is the first gate:
it collapses the combinatorial space and removes ambiguous LLM decisions.

Rules (hard, non-negotiable):
- Cannot open on a symbol where a position already exists on the same side.
- Cannot open opposite side without closing first (prevents hedge/conflict).
- Cannot close what you don't hold.
- If global cooldown is active -> only 'close' and 'hold' allowed.
- If max_open_positions reached -> no new opens, only manage existing.
- If setup_quality < threshold -> open blocked on that side/symbol.
- Reentry guard: recently closed same symbol within cooldown bars -> blocked.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from core.config import Settings
from core.types import (
    ActionSpace,
    MarketFrame,
    PositionSnapshot,
    RiskState,
    SetupQuality,
    Side,
)

OPEN_LONG = "open_long"
OPEN_SHORT = "open_short"
CLOSE = "close"
HOLD = "hold"

ALL_OPS = [OPEN_LONG, OPEN_SHORT, CLOSE, HOLD]


def build_action_space(
    frames: Dict[str, MarketFrame],
    positions: Dict[str, PositionSnapshot],
    quality: Dict[str, SetupQuality],
    risk: RiskState,
    settings: Settings,
    recent_closes: Optional[Dict[str, int]] = None,  # symbol -> bars_since_close
) -> ActionSpace:
    recent_closes = recent_closes or {}
    allowed: Dict[str, List[str]] = {}
    reasons: Dict[str, Dict[str, str]] = {}

    for ticker in frames.keys():
        tick_allowed: List[str] = [HOLD]
        tick_reasons: Dict[str, str] = {}
        pos = positions.get(ticker)
        q = quality.get(ticker)

        # CLOSE rules
        if pos is not None:
            tick_allowed.append(CLOSE)
        else:
            tick_reasons[CLOSE] = "no_open_position"

        # OPEN rules
        cooldown = risk.cooldown_active
        reached_cap = risk.open_position_count >= risk.max_positions

        for side_op, side_enum in ((OPEN_LONG, Side.LONG), (OPEN_SHORT, Side.SHORT)):
            if cooldown:
                tick_reasons[side_op] = f"risk_cooldown:{risk.cooldown_reason or 'halt'}"
                continue
            if pos is not None and pos.side == side_enum:
                tick_reasons[side_op] = "already_open_same_side"
                continue
            if pos is not None and pos.side != side_enum:
                tick_reasons[side_op] = "must_close_opposite_first"
                continue
            if reached_cap:
                tick_reasons[side_op] = "max_open_positions_reached"
                continue
            if q is not None and q.score < settings.risk.min_setup_quality:
                tick_reasons[side_op] = f"setup_quality_below_{settings.risk.min_setup_quality}"
                continue
            bars_since = recent_closes.get(ticker)
            if bars_since is not None and bars_since < settings.risk.reentry_cooldown_bars:
                tick_reasons[side_op] = f"reentry_cooldown_bars={bars_since}"
                continue
            tick_allowed.append(side_op)

        for op in ALL_OPS:
            if op not in tick_allowed and op not in tick_reasons:
                tick_reasons[op] = "forbidden"

        allowed[ticker] = tick_allowed
        reasons[ticker] = tick_reasons

    return ActionSpace(allowed=allowed, forbidden_reasons=reasons)
