"""Typed runtime settings for the trading system.

Every tunable knob flows through `Settings.load()` so modules never read env
vars directly. This avoids config drift across layers.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List, Optional

try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv()
except Exception:  # pragma: no cover
    pass


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass
class RiskSettings:
    max_open_positions: int = 2
    max_portfolio_exposure: float = 0.6     # % balance usable as margin
    max_single_position: float = 0.35       # % balance per trade
    max_leverage: int = 5
    daily_loss_halt_pct: float = 0.04       # -4% day → cooldown
    consecutive_loss_halt: int = 3
    cooldown_minutes_after_halt: int = 60
    min_setup_quality: float = 0.35         # below → block open
    reentry_cooldown_bars: int = 3
    min_notional_usd: float = 11.0          # HL requires > $10


@dataclass
class SLSettings:
    mode_default: str = "atr"               # 'atr' | 'structure' | 'fixed'
    atr_multiple: float = 1.8
    fixed_pct_fallback: float = 0.015       # 1.5% if ATR unavailable
    min_sl_pct: float = 0.004               # never tighter than 0.4%
    max_sl_pct: float = 0.05                # never wider than 5%


@dataclass
class TPSettings:
    mode_default: str = "atr"               # 'atr' | 'structure' | 'none'
    r_multiple: float = 2.5                 # TP at 2.5R
    atr_multiple: float = 3.5
    min_tp_pct: float = 0.006
    max_tp_pct: float = 0.2


@dataclass
class TrailingSettings:
    enabled_default: bool = True
    activation_r_multiple: float = 1.0      # activate after price moves 1R in favor
    trail_distance_atr: float = 1.4
    chandelier_enabled: bool = True
    min_tighten_ticks: float = 0.0005       # don't move SL for sub-tick changes
    never_loosen: bool = True               # SL can only move in the favorable direction
    min_profit_lock_pct: float = 0.002      # after activation lock at least +0.2%


@dataclass
class DecisionSettings:
    min_bars_between_trades: int = 2
    require_agreement_from_llm: bool = True
    llm_fallback_to_hold: bool = True
    llm_model: str = "gpt-5.1"


@dataclass
class Settings:
    tickers: List[str] = field(default_factory=lambda: ["BTC", "ETH", "SOL"])
    testnet: bool = True
    dry_run: bool = False                   # if True, no order is sent
    private_key: Optional[str] = None
    wallet_address: Optional[str] = None
    database_url: Optional[str] = None
    openai_api_key: Optional[str] = None
    cmc_api_key: Optional[str] = None
    run_interval_seconds: int = 900
    risk: RiskSettings = field(default_factory=RiskSettings)
    sl: SLSettings = field(default_factory=SLSettings)
    tp: TPSettings = field(default_factory=TPSettings)
    trailing: TrailingSettings = field(default_factory=TrailingSettings)
    decision: DecisionSettings = field(default_factory=DecisionSettings)

    @classmethod
    def load(cls) -> "Settings":
        return cls(
            tickers=os.getenv("TICKERS", "BTC,ETH,SOL").split(","),
            testnet=_env_bool("TESTNET", True),
            dry_run=_env_bool("DRY_RUN", False),
            private_key=os.getenv("PRIVATE_KEY"),
            wallet_address=os.getenv("WALLET_ADDRESS"),
            database_url=os.getenv("DATABASE_URL"),
            openai_api_key=os.getenv("OPENAI_API_KEY"),
            cmc_api_key=os.getenv("CMC_PRO_API_KEY"),
            run_interval_seconds=_env_int("RUN_INTERVAL_SECONDS", 900),
            risk=RiskSettings(
                max_open_positions=_env_int("RISK_MAX_OPEN_POSITIONS", 2),
                max_portfolio_exposure=_env_float("RISK_MAX_EXPOSURE", 0.6),
                max_single_position=_env_float("RISK_MAX_SINGLE", 0.35),
                max_leverage=_env_int("RISK_MAX_LEVERAGE", 5),
                daily_loss_halt_pct=_env_float("RISK_DAILY_LOSS_HALT", 0.04),
                consecutive_loss_halt=_env_int("RISK_CONSEC_LOSS", 3),
                min_setup_quality=_env_float("RISK_MIN_SETUP", 0.35),
            ),
            sl=SLSettings(
                mode_default=os.getenv("SL_MODE", "atr"),
                atr_multiple=_env_float("SL_ATR_MULT", 1.8),
            ),
            tp=TPSettings(
                mode_default=os.getenv("TP_MODE", "atr"),
                r_multiple=_env_float("TP_R_MULT", 2.5),
                atr_multiple=_env_float("TP_ATR_MULT", 3.5),
            ),
            trailing=TrailingSettings(
                enabled_default=_env_bool("TRAIL_ENABLED", True),
                activation_r_multiple=_env_float("TRAIL_ACT_R", 1.0),
                trail_distance_atr=_env_float("TRAIL_DIST_ATR", 1.4),
            ),
        )

    def validate_for_live(self) -> None:
        missing = []
        if not self.private_key:
            missing.append("PRIVATE_KEY")
        if not self.wallet_address:
            missing.append("WALLET_ADDRESS")
        if not self.openai_api_key:
            missing.append("OPENAI_API_KEY")
        if missing:
            raise RuntimeError(f"Missing required env vars: {', '.join(missing)}")
