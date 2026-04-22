"""Assembles ExternalContext from the existing fetcher modules.

Decoupling rationale: `news_feed`, `sentiment`, `forecaster` each returned
free-form strings; the decision layer needs structured facts. This module
normalizes them into the ExternalContext dataclass.
"""
from __future__ import annotations

from typing import Dict, List

from core.logging import get_logger
from core.types import ExternalContext

logger = get_logger(__name__)


def build_external_context(tickers: List[str]) -> ExternalContext:
    sentiment_value = None
    sentiment_label = None
    news_summary = ""
    forecasts: Dict[str, Dict[str, float]] = {}

    try:
        from sentiment import get_latest_fear_and_greed
        s = get_latest_fear_and_greed()
        if s:
            sentiment_value = int(s.get("valore")) if s.get("valore") is not None else None
            sentiment_label = s.get("classificazione")
    except Exception as e:
        logger.warning("sentiment_fetch_failed err=%s", e)

    try:
        from news_feed import fetch_latest_news
        news_summary = fetch_latest_news(max_chars=2000) or ""
    except Exception as e:
        logger.warning("news_fetch_failed err=%s", e)

    try:
        from forecaster import HyperliquidForecaster
        fc = HyperliquidForecaster(testnet=True)
        raws = fc.forecast_many(tickers, intervals=("15m", "1h"))
        for r in raws:
            t = r.get("Ticker")
            if not t:
                continue
            delta = r.get("Variazione %")
            if delta is None:
                continue
            horizon = "15m" if "15" in (r.get("Timeframe") or "") else "1h"
            forecasts.setdefault(t, {})[horizon] = float(delta)
    except Exception as e:
        logger.warning("forecast_fetch_failed err=%s", e)

    return ExternalContext(
        sentiment_value=sentiment_value,
        sentiment_label=sentiment_label,
        news_summary=news_summary,
        forecasts=forecasts,
    )
