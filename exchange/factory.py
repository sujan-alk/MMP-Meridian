"""
Exchange connector factory.
THIS IS THE ONLY FILE that imports concrete connector implementations.
Callers always receive a BaseConnector — swapping to C++ only changes this file.

Connector selection (create_connector only):
  1. Per-call override  (use_cpp=True/False)  — highest priority
  2. Per-exchange field (ExchangeBotConfig.use_cpp_connector)  — set in bot.json
  3. Global env var     USE_CPP_CONNECTOR=true  — lowest priority

If C++ is requested but the .so is not built, or the exchange has no C++ connector
(e.g. binance), factory logs a warning and falls back to CCXT automatically.

create_ws_connector always returns a CCXTWSConnector — it powers paper-trader order
book streaming via watch_order_book(), which is a CCXT-specific feature.
C++ connectors handle WebSocket internally; use create_connector() for them.
"""

from __future__ import annotations

import os

from exchange.base import BaseConnector
from exchange.ccxt_connector import CCXTConnector
from exchange.ccxt_ws_connector import CCXTWSConnector
from utils.logging import get_logger

log = get_logger("factory")

# ---------------------------------------------------------------------------
# C++ connector import (graceful fallback if .so not built)
# ---------------------------------------------------------------------------
try:
    from exchange.cpp_connector import (
        _CPP_AVAILABLE,
        CppKuCoinConnector,
        CppGateConnector,
        CppMexcConnector,
        CppKrakenConnector,
    )
    _CPP_CONNECTOR_MAP: dict[str, type] = {
        "kucoin": CppKuCoinConnector,
        "gate":   CppGateConnector,
        "mexc":   CppMexcConnector,
        "kraken": CppKrakenConnector,
    }
except ImportError:
    _CPP_AVAILABLE = False
    _CPP_CONNECTOR_MAP = {}

# Map our exchange name → CCXT exchange id
_CCXT_NAME_MAP: dict[str, str] = {
    "kucoin":  "kucoin",
    "gate":    "gate",
    "mexc":    "mexc",
    "kraken":  "kraken",
    "binance": "binance",
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _want_cpp(per_call_override: bool | None) -> bool:
    """
    Decide whether C++ connector is requested for this call.
    Priority: per-call override > USE_CPP_CONNECTOR env var.
    """
    if per_call_override is not None:
        return per_call_override
    return os.environ.get("USE_CPP_CONNECTOR", "").lower() in ("true", "1", "yes")


def _make_ccxt_connector(
    exchange_name: str,
    symbol: str,
    credentials: dict,
    quote_currency: str,
    ccxt_options: dict,
    use_websocket: bool,
) -> BaseConnector:
    ccxt_id = _CCXT_NAME_MAP.get(exchange_name)
    if ccxt_id is None:
        raise ValueError(
            f"Unsupported exchange: {exchange_name!r}. "
            f"Must be one of {list(_CCXT_NAME_MAP)}"
        )
    if use_websocket:
        return CCXTWSConnector(
            exchange_name=ccxt_id,
            symbol=symbol,
            credentials=credentials,
            quote_currency=quote_currency,
            ccxt_options=ccxt_options,
        )
    return CCXTConnector(
        exchange_name=ccxt_id,
        symbol=symbol,
        credentials=credentials,
        quote_currency=quote_currency,
        ccxt_options=ccxt_options,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def create_ws_connector(
    exchange_name: str,
    symbol: str,
    credentials: dict,
    quote_currency: str = "USDT",
    ccxt_options: dict | None = None,
) -> CCXTWSConnector:
    """
    Create a CCXTWSConnector for WebSocket order book streaming + paper trading.

    Always returns a CCXTWSConnector regardless of USE_CPP_CONNECTOR — this
    connector powers PaperTrader.watch_order_book() which is CCXT-specific.
    For live trading via C++, use create_connector() instead.

    Args:
        exchange_name: One of "kucoin", "gate", "mexc", "kraken"
        symbol: CCXT unified symbol, e.g. "ALKIMI/USDT"
        credentials: dict with api_key, api_secret, and optionally passphrase
        quote_currency: "USDT" or "USD"
        ccxt_options: Optional dict of CCXT options to pass through

    Returns:
        CCXTWSConnector instance
    """
    ccxt_id = _CCXT_NAME_MAP.get(exchange_name)
    if ccxt_id is None:
        raise ValueError(
            f"Unsupported exchange: {exchange_name!r}. "
            f"Must be one of {list(_CCXT_NAME_MAP)}"
        )
    return CCXTWSConnector(
        exchange_name=ccxt_id,
        symbol=symbol,
        credentials=credentials,
        quote_currency=quote_currency,
        ccxt_options=ccxt_options or {},
    )


def create_connector(
    exchange_name: str,
    symbol: str,
    credentials: dict,
    quote_currency: str = "USDT",
    ccxt_options: dict | None = None,
    use_websocket: bool = False,
    use_cpp: bool | None = None,
) -> BaseConnector:
    """
    Create and return a BaseConnector for the given exchange.

    Connector type selection:
      - If use_cpp=True  (or USE_CPP_CONNECTOR=true and use_cpp is None):
          Returns the C++ connector for the exchange if built and supported.
          Falls back to CCXT with a warning if unavailable.
      - If use_cpp=False:
          Always returns a CCXT connector (REST or WS based on use_websocket).
      - If use_cpp=None (default):
          Reads USE_CPP_CONNECTOR environment variable.

    When C++ is active, use_websocket is ignored — C++ connectors always
    maintain a live WebSocket stream internally.

    Args:
        exchange_name: One of "kucoin", "gate", "mexc", "kraken"
        symbol: CCXT unified symbol, e.g. "ALKIMI/USDT"
        credentials: dict with api_key, api_secret, and optionally passphrase
        quote_currency: "USDT" or "USD" (Kraken uses "USD")
        ccxt_options: Optional dict of CCXT options (CCXT path only)
        use_websocket: If True (CCXT path only), return CCXTWSConnector instead of CCXTConnector
        use_cpp: Override for C++ routing. None = use env var.

    Returns:
        BaseConnector instance (C++ or CCXT depending on config).
    """
    if _want_cpp(use_cpp):
        cpp_cls = _CPP_CONNECTOR_MAP.get(exchange_name)
        if cpp_cls is not None and _CPP_AVAILABLE:
            log.info("factory_using_cpp_connector", exchange=exchange_name, symbol=symbol)
            return cpp_cls(
                symbol=symbol,
                credentials=credentials,
                quote_currency=quote_currency,
            )
        elif not _CPP_AVAILABLE:
            log.warning(
                "factory_cpp_unavailable_fallback",
                exchange=exchange_name,
                msg="C++ .so not built — run: cd exchange/cpp/build && cmake .. && make. "
                    "Falling back to CCXT.",
            )
        else:
            log.warning(
                "factory_cpp_not_supported_fallback",
                exchange=exchange_name,
                msg=f"No C++ connector for {exchange_name!r}. Falling back to CCXT.",
            )

    # CCXT path
    log.info("factory_using_ccxt_connector",
             exchange=exchange_name, symbol=symbol, websocket=use_websocket)
    return _make_ccxt_connector(
        exchange_name=exchange_name,
        symbol=symbol,
        credentials=credentials,
        quote_currency=quote_currency,
        ccxt_options=ccxt_options or {},
        use_websocket=use_websocket,
    )
