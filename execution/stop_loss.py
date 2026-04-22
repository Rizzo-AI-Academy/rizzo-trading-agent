"""Dynamic SL/TP/trailing engine.

Pure functions: given a frame + position + settings, compute the new plan.
No exchange calls here; the execution layer submits the resulting orders.

Design:
- Initial SL:
    * atr    -> price - k_atr * ATR (for long); symmetric for short
              bounded by [min_sl_pct, max_sl_pct]
    * structure -> swing_low - 0.25 * ATR (for long); swing_high + 0.25 ATR short
                 bounded by the ATR bounds
    * fixed  -> fixed_pct_fallback
- Initial TP:
    * atr    -> sl_distance * r_multiple OR k_atr_tp * ATR, whichever is further
    * structure -> pivot R1/S1 aligned with direction
    * none   -> no TP (let trailing handle exits)
- Trailing:
    * inactive until pnl >= activation_r_multiple * initial_sl_distance
    * once active, new_sl = max(old_sl, price - trail_dist_atr * ATR) for long
    * if chandelier_enabled, also consider highest_close - k * ATR
    * never_loosen: SL cannot move backwards
    * min_profit_lock: after activation ensure SL locks at least small profit
"""
from __future__ import annotations

from dataclasses import replace
from typing import Optional

from core.config import Settings
from core.types import (
    MarketFrame,
    PositionSnapshot,
    Side,
    StopLossPlan,
)


def _atr_or_fallback(frame: MarketFrame, settings: Settings) -> float:
    if frame.atr14 and frame.atr14 > 0:
        return frame.atr14
    return frame.price * settings.sl.fixed_pct_fallback


def _bound_sl_distance(price: float, raw_distance: float, settings: Settings) -> float:
    min_d = price * settings.sl.min_sl_pct
    max_d = price * settings.sl.max_sl_pct
    return max(min_d, min(raw_distance, max_d))


def initial_plan(
    frame: MarketFrame,
    side: Side,
    settings: Settings,
    sl_mode: str = "atr",
    tp_mode: str = "atr",
    trailing_enabled: bool | None = None,
) -> StopLossPlan:
    price = frame.price
    atr = _atr_or_fallback(frame, settings)
    notes: list[str] = []

    # SL
    if sl_mode == "fixed":
        sl_dist = price * settings.sl.fixed_pct_fallback
    elif sl_mode == "structure":
        if side == Side.LONG:
            sl_dist_raw = price - (frame.swing_low - 0.25 * atr)
        else:
            sl_dist_raw = (frame.swing_high + 0.25 * atr) - price
        sl_dist = abs(sl_dist_raw)
    else:
        sl_dist = settings.sl.atr_multiple * atr

    sl_dist = _bound_sl_distance(price, sl_dist, settings)
    sl_price = price - sl_dist if side == Side.LONG else price + sl_dist

    # TP
    tp_price: Optional[float] = None
    if tp_mode == "none":
        tp_price = None
        notes.append("tp_disabled")
    elif tp_mode == "structure":
        if side == Side.LONG:
            tp_price = max(frame.pivots.r1, price + settings.tp.r_multiple * sl_dist)
        else:
            tp_price = min(frame.pivots.s1, price - settings.tp.r_multiple * sl_dist)
    else:  # atr
        tp_dist_r = settings.tp.r_multiple * sl_dist
        tp_dist_atr = settings.tp.atr_multiple * atr
        tp_dist = max(tp_dist_r, tp_dist_atr)
        # bounds
        tp_dist = max(price * settings.tp.min_tp_pct, min(tp_dist, price * settings.tp.max_tp_pct))
        tp_price = price + tp_dist if side == Side.LONG else price - tp_dist

    enabled = settings.trailing.enabled_default if trailing_enabled is None else trailing_enabled
    return StopLossPlan(
        sl_price=sl_price,
        tp_price=tp_price,
        trailing_enabled=enabled,
        trailing_activation_pct=settings.trailing.activation_r_multiple * (sl_dist / price),
        trailing_distance_atr=settings.trailing.trail_distance_atr,
        basis=sl_mode,
        notes=notes,
    )


def update_plan(
    position: PositionSnapshot,
    frame: MarketFrame,
    settings: Settings,
) -> StopLossPlan | None:
    """Return a new StopLossPlan only if SL/trail state should move; else None.

    Rules enforced:
    - never_loosen: SL can only move favorably.
    - min_tighten_ticks: skip sub-tick moves.
    - activation threshold: trailing stays off until PnL exceeds threshold.
    - min_profit_lock_pct: after activation SL locks a minimum profit.
    """
    entry = position.entry_price
    price = frame.price
    atr = _atr_or_fallback(frame, settings)

    # Initial SL distance for R multiple reference.
    if position.sl_price is None:
        return None  # nothing to compare against

    initial_sl_dist = abs(entry - position.sl_price)
    r = initial_sl_dist / entry if entry else 0.0
    activation_pct = settings.trailing.activation_r_multiple * r

    pnl_pct = position.pnl_pct
    trailing_ready = pnl_pct >= activation_pct

    if not position.trailing_active and not trailing_ready:
        return None
    trailing_active = True

    new_sl: float
    if position.side == Side.LONG:
        trail_stop = price - settings.trailing.trail_distance_atr * atr
        # chandelier against last close
        chandelier = max(frame.recent_closes[-20:] or [price]) - settings.trailing.trail_distance_atr * atr \
            if settings.trailing.chandelier_enabled else trail_stop
        candidate = max(trail_stop, chandelier)
        # profit lock
        min_lock = entry * (1 + settings.trailing.min_profit_lock_pct)
        candidate = max(candidate, min_lock)
        # never loosen
        if settings.trailing.never_loosen:
            candidate = max(candidate, position.sl_price)
        new_sl = candidate
    else:
        trail_stop = price + settings.trailing.trail_distance_atr * atr
        chandelier = min(frame.recent_closes[-20:] or [price]) + settings.trailing.trail_distance_atr * atr \
            if settings.trailing.chandelier_enabled else trail_stop
        candidate = min(trail_stop, chandelier)
        min_lock = entry * (1 - settings.trailing.min_profit_lock_pct)
        candidate = min(candidate, min_lock)
        if settings.trailing.never_loosen:
            candidate = min(candidate, position.sl_price)
        new_sl = candidate

    # Skip if the change is smaller than min_tighten_ticks fraction of price
    if abs(new_sl - position.sl_price) < settings.trailing.min_tighten_ticks * entry:
        # might still need to flip trailing_active flag
        if position.trailing_active == trailing_active:
            return None

    return StopLossPlan(
        sl_price=new_sl,
        tp_price=position.tp_price,
        trailing_enabled=True,
        trailing_activation_pct=activation_pct,
        trailing_distance_atr=settings.trailing.trail_distance_atr,
        basis="trailing",
        notes=[
            f"pnl_pct={pnl_pct:.4f}",
            f"activation_pct={activation_pct:.4f}",
        ],
    )


def should_force_close(position: PositionSnapshot, frame: MarketFrame) -> Optional[str]:
    """Soft structural exits that don't depend on the exchange SL.

    Called by lifecycle on each cycle. Returns a reason string if we should
    close, else None.
    """
    if position.side == Side.LONG and frame.structure_bias == Side.SHORT \
            and position.pnl_pct < 0:
        return "structure_flipped_short_with_loss"
    if position.side == Side.SHORT and frame.structure_bias == Side.LONG \
            and position.pnl_pct < 0:
        return "structure_flipped_long_with_loss"
    return None
