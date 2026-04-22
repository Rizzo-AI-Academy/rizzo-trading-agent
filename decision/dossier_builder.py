"""Turn a DecisionPacket into a compact textual dossier for the LLM.

Key property: the dossier contains *only* canonicalized, decision-relevant
facts. No raw indicator blobs. Every piece has a clear role:

- market lines: regime, structure, ATR%, RSI, funding
- position lines: side, pnl%, mae/mfe, bars held, SL/TP, trailing state
- risk block: balance, exposure, cooldown, consecutive losses
- setup_quality block: score + notes per symbol
- external: sentiment, news headline digest, forecast deltas
- action_space: the authoritative set of allowed ops per symbol

The LLM is instructed to pick only within action_space; everything else is a
hard-override by the kernel.
"""
from __future__ import annotations

import json
from typing import Dict

from core.types import DecisionPacket, MarketFrame, PositionSnapshot


def _fmt_frame(f: MarketFrame) -> str:
    bias = f.structure_bias.value if f.structure_bias else "none"
    return (
        f"- {f.ticker}: px={f.price:.4f} regime={f.regime.value} bias={bias} "
        f"ema20={f.ema20:.4f} ema50={f.ema50:.4f} "
        f"atr14={f.atr14:.4f} atr_pct={f.atr_pct*100:.2f}% "
        f"rsi14={f.rsi14:.1f} macd={f.macd:.3f} macd_slope={f.macd_slope:.3f} "
        f"funding={f.funding_rate:.5f} vol_ratio={f.volume_ratio:.2f} "
        f"fee_per_unit={f.est_fee_cost:.4f} swing_hi={f.swing_high:.4f} "
        f"swing_lo={f.swing_low:.4f}"
    )


def _fmt_pos(p: PositionSnapshot) -> str:
    sl = f"{p.sl_price:.4f}" if p.sl_price else "none"
    tp = f"{p.tp_price:.4f}" if p.tp_price else "none"
    return (
        f"- {p.symbol} side={p.side.value} size={p.size} entry={p.entry_price:.4f} "
        f"mark={p.mark_price:.4f} pnl%={p.pnl_pct*100:.2f} "
        f"mfe%={p.mfe_pct*100:.2f} mae%={p.mae_pct*100:.2f} "
        f"bars={p.bars_held} sl={sl} tp={tp} trailing={p.trailing_active} "
        f"lev={p.leverage}"
    )


def build_dossier(packet: DecisionPacket) -> str:
    lines = []
    lines.append(f"run_id={packet.run_id} ts={packet.ts.isoformat()}")
    lines.append(
        f"balance_usd={packet.balance_usd:.2f} "
        f"exposure={packet.risk.exposure_ratio:.2f} "
        f"open_positions={packet.risk.open_position_count}/{packet.risk.max_positions} "
        f"daily_pnl%={packet.risk.daily_pnl_pct*100:.2f} "
        f"consecutive_losses={packet.risk.consecutive_losses} "
        f"cooldown={'YES:' + (packet.risk.cooldown_reason or '') if packet.risk.cooldown_active else 'no'}"
    )

    lines.append("\n[MARKET]")
    for t, f in packet.market_frames.items():
        lines.append(_fmt_frame(f))

    lines.append("\n[POSITIONS]")
    if packet.positions:
        for _, p in packet.positions.items():
            lines.append(_fmt_pos(p))
    else:
        lines.append("- none")

    lines.append("\n[SETUP_QUALITY]")
    for t, q in packet.setup_quality.items():
        lines.append(
            f"- {t}: score={q.score} trend={q.trend_alignment} mom={q.momentum} "
            f"vol_ok={q.volatility_ok} struct_ok={q.structure_ok} "
            f"fee_ok={q.fee_vs_target_ok} notes={','.join(q.notes) or '-'}"
        )

    lines.append("\n[EXTERNAL]")
    ext = packet.external
    lines.append(f"sentiment={ext.sentiment_value} ({ext.sentiment_label})")
    fc_lines = []
    for sym, fc in ext.forecasts.items():
        fc_lines.append(f"{sym}=" + ",".join(f"{k}:{v:+.2f}%" for k, v in fc.items()))
    lines.append("forecasts: " + " | ".join(fc_lines) if fc_lines else "forecasts: none")
    news = (ext.news_summary or "").strip()
    if news:
        lines.append("news_digest: " + news[:800].replace("\n", " / "))

    lines.append("\n[ACTION_SPACE]")
    for sym, ops in packet.action_space.allowed.items():
        lines.append(f"- {sym}: allowed={ops}")
    lines.append("forbidden_reasons=" + json.dumps(packet.action_space.forbidden_reasons))

    return "\n".join(lines)


SYSTEM_INSTRUCTIONS = (
    "You are the decision kernel of a deterministic trading bot. You receive a canonical, "
    "compact dossier. Your ONLY job is to pick ONE action from the symbol's action_space. "
    "Do NOT invent actions outside action_space. Hard constraints (risk, leverage cap, "
    "SL/TP policy, cooldowns) are enforced downstream - the system will override you if "
    "you violate them. Choose the action that maximises risk-adjusted expectancy given "
    "the dossier.\n\n"
    "Output JSON only, matching the schema.\n"
    "Policy:\n"
    "- Prefer HOLD when setup_quality is low or ambiguous.\n"
    "- Prefer CLOSE when structure reverses against an open position or mae deepens.\n"
    "- On OPEN, pick leverage modestly; the system clamps over-aggressive leverage.\n"
    "- Use size_fraction relative to balance: 0.05..0.3 normal, never >0.35.\n"
    "- Prefer 'atr' SL/TP modes unless clear structural levels are better.\n"
    "- Rationale must be one sentence, factual."
)
