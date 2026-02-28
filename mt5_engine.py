"""MetaTrader 5 engine module.

Provides a singleton MT5 engine that encapsulates terminal initialization,
market data retrieval, and market order placement with robust error handling.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any

from dotenv import load_dotenv
import os

try:
    import MetaTrader5 as mt5
except ImportError:  # pragma: no cover - runtime environment may not have MT5 package
    mt5 = None  # type: ignore[assignment]


load_dotenv()

logger = logging.getLogger(__name__)


class MT5EngineError(Exception):
    """Raised when MT5 terminal interactions fail."""


@dataclass(slots=True)
class TickPrice:
    symbol: str
    bid: float
    ask: float
    time: int


class MT5Engine:
    """Thread-safe singleton wrapper around MetaTrader5 package APIs."""

    _instance: "MT5Engine | None" = None
    _lock = threading.Lock()

    def __new__(cls) -> "MT5Engine":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        if self._initialized:
            return
        self.login = os.getenv("MT5_LOGIN", "").strip()
        self.password = os.getenv("MT5_PASSWORD", "").strip()
        self.server = os.getenv("MT5_SERVER", "").strip()

        if not self.login or not self.password or not self.server:
            raise MT5EngineError(
                "Missing MT5 credentials. Ensure MT5_LOGIN, MT5_PASSWORD, and MT5_SERVER are set."
            )
        self._initialized = True

    def connect(self) -> None:
        """Initialize and login to MT5 terminal."""
        if mt5 is None:
            raise MT5EngineError("MetaTrader5 package is not installed in this environment.")

        initialized = mt5.initialize(
            login=int(self.login),
            password=self.password,
            server=self.server,
        )
        if not initialized:
            code, message = mt5.last_error()
            raise MT5EngineError(f"MT5 initialize() failed: [{code}] {message}")

    def shutdown(self) -> None:
        """Shutdown MT5 terminal connection if module is available."""
        if mt5 is None:
            return
        mt5.shutdown()

    def get_price(self, symbol: str) -> TickPrice:
        """Fetch latest tick for a symbol."""
        self.connect()
        symbol = symbol.upper().strip()

        selected = mt5.symbol_select(symbol, True)
        if not selected:
            code, message = mt5.last_error()
            self.shutdown()
            raise MT5EngineError(f"Failed to select symbol {symbol}: [{code}] {message}")

        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            code, message = mt5.last_error()
            self.shutdown()
            raise MT5EngineError(f"Failed to fetch tick for {symbol}: [{code}] {message}")

        price = TickPrice(symbol=symbol, bid=tick.bid, ask=tick.ask, time=tick.time)
        self.shutdown()
        return price

    def send_market_order(
        self,
        symbol: str,
        volume: float,
        order_type: str,
        comment: str = "perfect-traders bridge",
    ) -> dict[str, Any]:
        """Send a market order to MT5 and return execution metadata."""
        self.connect()
        symbol = symbol.upper().strip()
        order_type = order_type.lower().strip()

        if order_type not in {"buy", "sell"}:
            self.shutdown()
            raise MT5EngineError("order_type must be 'buy' or 'sell'.")

        selected = mt5.symbol_select(symbol, True)
        if not selected:
            code, message = mt5.last_error()
            self.shutdown()
            raise MT5EngineError(f"Failed to select symbol {symbol}: [{code}] {message}")

        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            code, message = mt5.last_error()
            self.shutdown()
            raise MT5EngineError(f"Failed to fetch tick for {symbol}: [{code}] {message}")

        mt5_type = mt5.ORDER_TYPE_BUY if order_type == "buy" else mt5.ORDER_TYPE_SELL
        price = tick.ask if order_type == "buy" else tick.bid

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": float(volume),
            "type": mt5_type,
            "price": price,
            "deviation": 20,
            "magic": 202601,
            "comment": comment,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        result = mt5.order_send(request)
        if result is None:
            code, message = mt5.last_error()
            self.shutdown()
            raise MT5EngineError(f"order_send() returned None: [{code}] {message}")

        if result.retcode != mt5.TRADE_RETCODE_DONE:
            code, message = mt5.last_error()
            self.shutdown()
            raise MT5EngineError(
                "Order rejected. "
                f"retcode={result.retcode}, comment={result.comment}, mt5_last_error=[{code}] {message}"
            )

        response = {
            "ticket": int(result.order),
            "deal": int(result.deal),
            "retcode": int(result.retcode),
            "price": float(result.price),
            "volume": float(result.volume),
            "symbol": symbol,
            "type": order_type,
        }
        self.shutdown()
        logger.info("MT5 order executed successfully: %s", response)
        return response
