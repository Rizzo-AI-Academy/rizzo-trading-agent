"""Entrypoint — runs one full trading cycle through the Orchestrator.

Usage:
    python main.py                 # one cycle
    RUN_INTERVAL_SECONDS=0 ...     # explicit single-shot
"""
from __future__ import annotations

import json
import sys

from core.config import Settings
from core.logging import get_logger, setup_logging
from decision.kernel import LLMClient
from execution.hyperliquid_adapter import HyperliquidAdapter
from indicators import analyze_multiple_tickers
from market.context import build_external_context
from orchestrator.runtime import Orchestrator


def main() -> int:
    setup_logging()
    log = get_logger("main")
    settings = Settings.load()
    settings.validate_for_live() if not settings.dry_run else None

    # market data (legacy fetcher; returns (text, list[dict])
    _, indicator_rows = analyze_multiple_tickers(settings.tickers, testnet=settings.testnet)

    # external context
    ext = build_external_context(settings.tickers)

    # adapter + llm
    adapter = HyperliquidAdapter(
        secret_key=settings.private_key,
        account_address=settings.wallet_address,
        testnet=settings.testnet,
    )
    llm = LLMClient(api_key=settings.openai_api_key, model=settings.decision.llm_model)

    orch = Orchestrator(settings, adapter=adapter, llm=llm)
    orch.bootstrap()

    result = orch.run_cycle(indicator_rows, ext)
    log.info("cycle_result %s", json.dumps(result, default=str)[:2000])
    print(json.dumps(result, default=str, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
