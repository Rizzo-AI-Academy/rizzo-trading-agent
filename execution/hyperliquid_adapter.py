"""Hyperliquid adapter — refactored around typed intents.

Differences vs legacy `hyperliquid_trader.py`:
- Accepts OrderIntent / returns OrderResult.
- Never implicitly places a SL of its own; SL/TP come from the StopLossPlan.
- Exposes separate primitives: market_in, market_out, place_trigger,
  cancel_trigger, set_leverage, fetch_positions, fetch_mids.
- Rounds sizes using szDecimals from meta — no guessed decimals.
"""
from __future__ import annotations

import time
from decimal import Decimal, ROUND_DOWN
from typing import Any, Dict, List, Optional

try:
    import eth_account  # type: ignore
    from eth_account.signers.local import LocalAccount  # type: ignore
    from hyperliquid.info import Info  # type: ignore
    from hyperliquid.exchange import Exchange  # type: ignore
    from hyperliquid.utils import constants  # type: ignore
    HL_AVAILABLE = True
except Exception:
    HL_AVAILABLE = False

from core.logging import get_logger
from core.types import OrderIntent, OrderResult, OrderStatus, Side

logger = get_logger(__name__)


class HyperliquidAdapter:
    def __init__(self, secret_key: str, account_address: str,
                 testnet: bool = True, skip_ws: bool = True):
        if not HL_AVAILABLE:
            raise RuntimeError("hyperliquid-python-sdk not installed")
        self.account_address = account_address
        base_url = constants.TESTNET_API_URL if testnet else constants.MAINNET_API_URL
        self.base_url = base_url
        account: LocalAccount = eth_account.Account.from_key(secret_key)
        self.info = Info(base_url, skip_ws=skip_ws)
        self.exchange = Exchange(account, base_url, account_address=account_address)
        self.meta = self.info.meta()

    # ------------------ helpers ------------------
    def _asset(self, symbol: str) -> Dict[str, Any]:
        for p in self.meta["universe"]:
            if p["name"] == symbol:
                return p
        raise RuntimeError(f"symbol {symbol} not found in meta")

    def _sz_decimals(self, symbol: str) -> int:
        return int(self._asset(symbol).get("szDecimals", 8))

    def _round_size(self, symbol: str, size: float) -> float:
        sz_dec = self._sz_decimals(symbol)
        q = Decimal(str(size)).quantize(Decimal(10) ** -sz_dec, rounding=ROUND_DOWN)
        return float(q)

    def _round_price(self, price: float) -> float:
        # HL accepts a finite number of significant figures; this is a safe default.
        if price > 5000:
            return round(price, 1)
        if price > 500:
            return round(price, 2)
        if price > 10:
            return round(price, 3)
        if price > 1:
            return round(price, 4)
        return round(price, 5)

    # ------------------ reads ------------------
    def fetch_mids(self) -> Dict[str, float]:
        return {k: float(v) for k, v in self.info.all_mids().items()}

    def fetch_user_state(self) -> Dict[str, Any]:
        return self.info.user_state(self.account_address)

    def fetch_positions(self) -> List[Dict[str, Any]]:
        state = self.fetch_user_state()
        mids = self.fetch_mids()
        out = []
        for ap in state.get("assetPositions", []) or []:
            pos = ap.get("position") if isinstance(ap, dict) else None
            if not pos:
                continue
            size = float(pos.get("szi", 0))
            if size == 0:
                continue
            coin = pos.get("coin")
            entry = float(pos.get("entryPx", 0))
            mark = float(mids.get(coin, entry))
            lev = pos.get("leverage", {}) or {}
            out.append({
                "symbol": coin,
                "side": "long" if size > 0 else "short",
                "size": abs(size),
                "entry_price": entry,
                "mark_price": mark,
                "pnl_usd": round((mark - entry) * size, 6),
                "leverage_value": float(lev.get("value", 0) or 0),
                "leverage_type": lev.get("type", "cross"),
            })
        return out

    def fetch_open_trigger_orders(self) -> List[Dict[str, Any]]:
        try:
            return self.info.open_orders(self.account_address)
        except Exception:
            return []

    def account_value(self) -> float:
        state = self.fetch_user_state()
        return float(state["marginSummary"]["accountValue"])

    # ------------------ mutations ------------------
    def set_leverage(self, symbol: str, leverage: int, is_cross: bool = True) -> Dict[str, Any]:
        return self.exchange.update_leverage(leverage=leverage, name=symbol, is_cross=is_cross)

    def market_in(self, intent: OrderIntent) -> OrderResult:
        sz = self._round_size(intent.symbol, intent.size)
        is_buy = intent.side == Side.LONG
        try:
            if intent.leverage:
                self.set_leverage(intent.symbol, intent.leverage, is_cross=True)
                time.sleep(0.3)
            raw = self.exchange.market_open(intent.symbol, is_buy, sz, None, 0.01)
            status = OrderStatus.FILLED if raw.get("status") == "ok" else OrderStatus.ERROR
            filled = 0.0
            avg = None
            exch_id = None
            try:
                data = raw["response"]["data"]["statuses"][0]
                if "filled" in data:
                    filled = float(data["filled"]["totalSz"])
                    avg = float(data["filled"]["avgPx"])
                    exch_id = data["filled"].get("oid")
            except Exception:
                pass
            return OrderResult(
                client_order_id=intent.client_order_id,
                status=status,
                exchange_order_id=str(exch_id) if exch_id else None,
                filled_size=filled,
                avg_price=avg,
                raw=raw,
            )
        except Exception as e:
            logger.warning("market_in_error symbol=%s err=%s", intent.symbol, e)
            return OrderResult(
                client_order_id=intent.client_order_id,
                status=OrderStatus.ERROR,
                exchange_order_id=None,
                filled_size=0.0,
                avg_price=None,
                raw={},
                error=str(e),
            )

    def market_out(self, intent: OrderIntent) -> OrderResult:
        try:
            raw = self.exchange.market_close(intent.symbol)
            status = OrderStatus.FILLED if raw.get("status") == "ok" else OrderStatus.ERROR
            return OrderResult(
                client_order_id=intent.client_order_id,
                status=status,
                exchange_order_id=None,
                filled_size=intent.size,
                avg_price=None,
                raw=raw,
            )
        except Exception as e:
            return OrderResult(
                client_order_id=intent.client_order_id,
                status=OrderStatus.ERROR,
                exchange_order_id=None,
                filled_size=0.0,
                avg_price=None,
                raw={},
                error=str(e),
            )

    def place_trigger(self, intent: OrderIntent, kind: str) -> OrderResult:
        """kind = 'sl' or 'tp'. reduce_only=True always."""
        if intent.price is None:
            raise ValueError("trigger price required")
        sz = self._round_size(intent.symbol, intent.size)
        trig_px = self._round_price(intent.price)
        is_buy = intent.side == Side.LONG  # intent.side is the *order* side
        order_type = {"trigger": {"triggerPx": float(trig_px), "isMarket": True, "tpsl": kind}}
        try:
            raw = self.exchange.order(
                name=intent.symbol,
                is_buy=is_buy,
                sz=sz,
                limit_px=float(trig_px),
                order_type=order_type,
                reduce_only=True,
            )
            ok = raw.get("status") == "ok"
            exch_id = None
            try:
                exch_id = raw["response"]["data"]["statuses"][0].get("resting", {}).get("oid")
            except Exception:
                pass
            return OrderResult(
                client_order_id=intent.client_order_id,
                status=OrderStatus.SUBMITTED if ok else OrderStatus.ERROR,
                exchange_order_id=str(exch_id) if exch_id else None,
                filled_size=0.0,
                avg_price=None,
                raw=raw,
            )
        except Exception as e:
            return OrderResult(
                client_order_id=intent.client_order_id,
                status=OrderStatus.ERROR,
                exchange_order_id=None,
                filled_size=0.0,
                avg_price=None,
                raw={},
                error=str(e),
            )

    def cancel(self, symbol: str, exchange_order_id: str) -> Dict[str, Any]:
        try:
            return self.exchange.cancel(name=symbol, oid=int(exchange_order_id))
        except Exception as e:
            return {"status": "error", "error": str(e)}
