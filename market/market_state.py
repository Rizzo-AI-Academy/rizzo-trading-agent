"""Derives canonical MarketFrame objects from raw indicator payloads.

Reuses the existing `indicators.CryptoTechnicalAnalysisHL` to fetch data, then
normalizes it into a compact MarketFrame with regime, structure bias and
execution cost fields. This is the single entry point used by the Decision
Inputs layer; nothing else should read raw indicator dicts.
"""
from __future__ import annotations

from datetime import datetime
from statistics import mean
from typing import Dict, List, Optional, Tuple

from core.logging import get_logger
from core.types import MarketFrame, PivotPoints, Regime, Side, utcnow

logger = get_logger(__name__)


TAKER_FEE_RATE = 0.00035


def _slope(series: List[float]) -> float:
    if len(series) < 2:
        return 0.0
    return (series[-1] - series[0]) / max(1.0, abs(series[0])) * 100.0


def _classify_regime(ema20: float, ema50: float, atr_pct: float,
                     macd_slope: float) -> Regime:
    if ema20 > ema50 and macd_slope > 0 and atr_pct > 0.002:
        return Regime.TRENDING_UP
    if ema20 < ema50 and macd_slope < 0 and atr_pct > 0.002:
        return Regime.TRENDING_DOWN
    if atr_pct < 0.0015:
        return Regime.CHOP
    return Regime.RANGE


def _structure_bias(closes: List[float], lookback: int = 10) -> Tuple[Optional[Side], float, float]:
    window = closes[-lookback:] if len(closes) >= lookback else closes
    if len(window) < 3:
        return None, max(window), min(window)
    swing_high = max(window)
    swing_low = min(window)
    mid = (swing_high + swing_low) / 2.0
    last = window[-1]
    if last > mid and window[-1] > window[-2]:
        return Side.LONG, swing_high, swing_low
    if last < mid and window[-1] < window[-2]:
        return Side.SHORT, swing_high, swing_low
    return None, swing_high, swing_low


def build_market_frame(raw: Dict) -> MarketFrame:
    """Convert one ticker's raw indicator payload into a MarketFrame.

    Raw layout must match `indicators.CryptoTechnicalAnalysisHL.get_complete_analysis`.
    """
    ticker = raw["ticker"]
    current = raw.get("current") or {}
    pivots = raw.get("pivot_points") or {}
    deriv = raw.get("derivatives") or {}
    intraday = raw.get("intraday") or {}
    lt = raw.get("longer_term_15m") or {}

    price = float(current.get("price") or 0.0)
    ema20 = float(lt.get("ema_20_current") or current.get("ema20") or price)
    ema50 = float(lt.get("ema_50_current") or ema20)
    atr14 = float(lt.get("atr_14_current") or 0.0)
    atr_pct = atr14 / price if price else 0.0

    rsi14 = float(lt.get("rsi_14_series", [None])[-1] or 50.0)
    rsi7 = float(current.get("rsi_7") or 50.0)
    macd_series = lt.get("macd_series") or [current.get("macd") or 0.0]
    macd = float(macd_series[-1])
    macd_slope = _slope([float(x) for x in macd_series[-5:]])

    closes = [float(x) for x in intraday.get("mid_prices") or []]
    bias, sh, sl = _structure_bias(closes)
    regime = _classify_regime(ema20, ema50, atr_pct, macd_slope)

    vol_cur = float(lt.get("volume_current") or 0.0)
    vol_avg = float(lt.get("volume_average") or 0.0) or 1e-9
    vol_ratio = vol_cur / vol_avg

    # spread estimate from orderbook string "Bid Vol: x, Ask Vol: y" has no
    # price — default to 2 bps when unknown (HL perps typically ~1-5 bps)
    spread_pct = 0.0002

    est_fee = price * TAKER_FEE_RATE

    pivots_obj = PivotPoints(
        pp=float(pivots.get("pp") or price),
        s1=float(pivots.get("s1") or price),
        s2=float(pivots.get("s2") or price),
        r1=float(pivots.get("r1") or price),
        r2=float(pivots.get("r2") or price),
    )

    ts_raw = raw.get("timestamp")
    if isinstance(ts_raw, str):
        try:
            ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
        except Exception:
            ts = utcnow()
    else:
        ts = utcnow()

    return MarketFrame(
        ticker=ticker,
        ts=ts,
        price=price,
        ema20=ema20,
        ema50=ema50,
        atr14=atr14,
        atr_pct=atr_pct,
        rsi14=rsi14,
        rsi7=rsi7,
        macd=macd,
        macd_slope=macd_slope,
        funding_rate=float(deriv.get("funding_rate") or 0.0),
        open_interest=float(deriv.get("open_interest_latest") or 0.0),
        pivots=pivots_obj,
        volume_ratio=vol_ratio,
        regime=regime,
        structure_bias=bias,
        swing_high=sh,
        swing_low=sl,
        recent_closes=closes[-20:],
        est_fee_cost=est_fee,
        spread_pct=spread_pct,
    )


def build_all_frames(raw_list: List[Dict]) -> Dict[str, MarketFrame]:
    frames: Dict[str, MarketFrame] = {}
    for raw in raw_list or []:
        try:
            fr = build_market_frame(raw)
            frames[fr.ticker] = fr
        except Exception as e:
            logger.warning("Skipping frame %s: %s", raw.get("ticker"), e)
    return frames
