"""Setup quality score: combines trend alignment, momentum, volatility and
execution cost into a single 0..1 number per ticker.

Used both by hard rules (block low-quality opens) and by the dossier (so the
LLM sees *why* a setup scored low).
"""
from __future__ import annotations

from typing import Dict

from core.config import Settings
from core.types import MarketFrame, Regime, SetupQuality, Side


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def score_ticker(frame: MarketFrame, settings: Settings,
                 intended_side: Side | None = None) -> SetupQuality:
    notes: list[str] = []

    # 1) trend alignment: EMA20 vs EMA50 and structure bias
    ema_diff = 0.0
    if frame.ema50:
        ema_diff = (frame.ema20 - frame.ema50) / frame.ema50
    trend_alignment = max(-1.0, min(1.0, ema_diff * 200))  # ~scale

    if intended_side == Side.LONG:
        trend_score = _clamp(0.5 + trend_alignment * 0.5)
    elif intended_side == Side.SHORT:
        trend_score = _clamp(0.5 - trend_alignment * 0.5)
    else:
        trend_score = _clamp(0.5 + abs(trend_alignment) * 0.5)

    # 2) momentum: RSI distance from 50 + macd slope sign
    rsi_dev = (frame.rsi14 - 50.0) / 50.0
    if intended_side == Side.LONG:
        momentum = _clamp(0.5 + rsi_dev * 0.5 + (1 if frame.macd_slope > 0 else -1) * 0.25)
    elif intended_side == Side.SHORT:
        momentum = _clamp(0.5 - rsi_dev * 0.5 + (1 if frame.macd_slope < 0 else -1) * 0.25)
    else:
        momentum = _clamp(0.5 + abs(rsi_dev) * 0.5)

    # 3) volatility: ATR% must be in a usable band
    vol_ok = 0.002 <= frame.atr_pct <= 0.05
    if not vol_ok:
        if frame.atr_pct < 0.002:
            notes.append("atr_too_low")
        else:
            notes.append("atr_too_high")

    # 4) structure ok: structure_bias must align with intended_side (if any)
    structure_ok = True
    if intended_side is not None and frame.structure_bias is not None:
        structure_ok = frame.structure_bias == intended_side
        if not structure_ok:
            notes.append("structure_vs_intent_mismatch")

    # 5) fee vs target: ATR move must be materially larger than fee cost
    min_target_pct = max(settings.sl.min_sl_pct * settings.tp.r_multiple, 0.003)
    min_target_abs = frame.price * min_target_pct
    fee_ok = frame.est_fee_cost * 2 < min_target_abs * 0.3   # fees < 30% of target
    if not fee_ok:
        notes.append("fee_vs_target_tight")

    # regime penalty
    regime_score = 0.5
    if frame.regime in (Regime.TRENDING_UP, Regime.TRENDING_DOWN):
        regime_score = 0.75
    elif frame.regime == Regime.CHOP:
        regime_score = 0.2
        notes.append("regime_chop")

    base = 0.35 * trend_score + 0.25 * momentum + 0.2 * regime_score
    adj = 0.1 * (1 if vol_ok else 0) + 0.05 * (1 if structure_ok else 0) + 0.05 * (1 if fee_ok else 0)
    score = _clamp(base + adj)

    return SetupQuality(
        score=round(score, 3),
        trend_alignment=round(trend_alignment, 3),
        momentum=round(momentum, 3),
        volatility_ok=vol_ok,
        structure_ok=structure_ok,
        fee_vs_target_ok=fee_ok,
        notes=notes,
    )


def score_all(frames: Dict[str, MarketFrame], settings: Settings,
              intended_sides: Dict[str, Side | None] | None = None) -> Dict[str, SetupQuality]:
    sides = intended_sides or {}
    return {t: score_ticker(f, settings, sides.get(t)) for t, f in frames.items()}
