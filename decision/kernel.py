"""Decision kernel: consumes a DecisionPacket, produces a TradeDecision.

Flow:
1. Build dossier from packet.
2. Ask LLM for proposal (schema-constrained).
3. Validate LLM proposal against action_space + hard rules.
4. If LLM unavailable / invalid -> deterministic fallback (hold or manage).

The kernel NEVER bypasses hard_rules.enforce. All overrides are logged.
"""
from __future__ import annotations

import json
from typing import Optional, Tuple

from core.config import Settings
from core.logging import get_logger
from core.types import (
    DecisionPacket,
    Operation,
    Side,
    TradeDecision,
)
from decision.dossier_builder import SYSTEM_INSTRUCTIONS, build_dossier
from decision.hard_rules import enforce

logger = get_logger(__name__)


LLM_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "operation": {"type": "string", "enum": ["open", "close", "hold"]},
        "symbol": {"type": "string"},
        "direction": {"type": ["string", "null"], "enum": ["long", "short", None]},
        "size_fraction": {"type": "number", "minimum": 0.0, "maximum": 0.35},
        "leverage": {"type": "integer", "minimum": 1, "maximum": 10},
        "stop_loss_mode": {"type": "string", "enum": ["atr", "structure", "fixed"]},
        "take_profit_mode": {"type": "string", "enum": ["atr", "structure", "none"]},
        "trailing_enabled": {"type": "boolean"},
        "reason": {"type": "string", "minLength": 1, "maxLength": 300},
    },
    "required": [
        "operation", "symbol", "size_fraction", "leverage",
        "stop_loss_mode", "take_profit_mode", "trailing_enabled", "reason",
    ],
    "additionalProperties": False,
}


class LLMClient:
    """Thin OpenAI wrapper. Swappable for tests."""

    def __init__(self, api_key: Optional[str], model: str):
        self.model = model
        self._client = None
        self._api_key = api_key

    def _ensure(self):
        if self._client is None:
            if not self._api_key:
                raise RuntimeError("openai api key not set")
            from openai import OpenAI  # type: ignore
            self._client = OpenAI(api_key=self._api_key)
        return self._client

    def propose(self, dossier: str) -> dict:
        client = self._ensure()
        resp = client.responses.create(
            model=self.model,
            input=SYSTEM_INSTRUCTIONS + "\n\n" + dossier,
            text={
                "format": {
                    "type": "json_schema",
                    "name": "trade_decision",
                    "strict": True,
                    "schema": LLM_JSON_SCHEMA,
                },
                "verbosity": "low",
            },
            reasoning={"effort": "medium"},
            tools=[],
            store=True,
        )
        return json.loads(resp.output_text)


def _deterministic_fallback(packet: DecisionPacket) -> TradeDecision:
    """No LLM available -> manage-open-positions-only policy.

    If we have any open position, emit HOLD (lifecycle manager handles SL/TP).
    Otherwise pick the ticker with best setup_quality that allows an open;
    if none passes threshold, HOLD on the first ticker.
    """
    # prefer HOLD — kernel never opens without LLM
    default_symbol = next(iter(packet.market_frames.keys()))
    return TradeDecision(
        operation=Operation.HOLD,
        symbol=default_symbol,
        direction=None,
        size_fraction=0.0,
        leverage=1,
        stop_loss_mode="atr",
        take_profit_mode="atr",
        trailing_enabled=False,
        reason="deterministic_fallback_no_llm",
        deterministic_override="llm_unavailable",
    )


def _parse_llm(raw: dict, packet: DecisionPacket) -> TradeDecision:
    op = Operation(raw["operation"])
    direction = None
    if raw.get("direction") in ("long", "short"):
        direction = Side(raw["direction"])
    return TradeDecision(
        operation=op,
        symbol=raw["symbol"],
        direction=direction,
        size_fraction=float(raw.get("size_fraction") or 0.0),
        leverage=int(raw.get("leverage") or 1),
        stop_loss_mode=raw.get("stop_loss_mode", "atr"),
        take_profit_mode=raw.get("take_profit_mode", "atr"),
        trailing_enabled=bool(raw.get("trailing_enabled", True)),
        reason=raw.get("reason", ""),
        raw_llm=raw,
    )


def decide(packet: DecisionPacket, settings: Settings,
           llm: Optional[LLMClient] = None) -> Tuple[TradeDecision, Optional[dict]]:
    """Returns (final_decision, llm_raw_output)."""
    dossier = build_dossier(packet)
    logger.info("kernel_dossier_ready run_id=%s bytes=%d", packet.run_id, len(dossier))

    llm_raw: Optional[dict] = None
    if llm is not None:
        try:
            llm_raw = llm.propose(dossier)
            proposed = _parse_llm(llm_raw, packet)
        except Exception as e:
            logger.warning("llm_failed err=%s run_id=%s", e, packet.run_id)
            proposed = _deterministic_fallback(packet)
    else:
        proposed = _deterministic_fallback(packet)

    final, override = enforce(packet, proposed, settings)
    if override:
        logger.warning(
            "kernel_override run_id=%s reason=%s orig=%s final=%s",
            packet.run_id, override, proposed.to_dict(), final.to_dict(),
        )
    else:
        logger.info("kernel_decision run_id=%s decision=%s", packet.run_id, final.to_dict())
    return final, llm_raw
