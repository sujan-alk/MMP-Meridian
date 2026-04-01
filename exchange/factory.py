"""
Exchange connector factory.
THIS IS THE ONLY FILE that imports concrete connector implementations.
Callers always receive a BaseConnector — swapping to C++ only changes this file.
"""

from __future__ import annotations

from exchange.base import BaseConnector
from exchange.ccxt_connector import CCXTConnector
from exchange.ccxt_ws_connector import CCXTWSConnector

# Map our exchange name → CCXT exchange id
_CCXT_NAME_MAP: dict[str, str] = {
    "kucoin": "kucoin",
    "gate": "gate",
    "mexc": "mexc",
    "kraken": "kraken",
}


def create_ws_connector(
    exchange_name: str,
    symbol: str,
    credentials: dict,
    quote_currency: str = "USDT",
    ccxt_options: dict | None = None,
) -> CCXTWSConnector:
    """
    Create a CCXTWSConnector for WebSocket order book streaming + paper trading.

    Use create_connector() for REST-only mode. Use this when use_websocket=True
    is set in the exchange config.

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
        raise ValueError(f"Unsupported exchange: {exchange_name}. Must be one of {list(_CCXT_NAME_MAP)}")

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
) -> BaseConnector:
    """
    Create and return a BaseConnector for the given exchange.

    Args:
        exchange_name: One of "kucoin", "gate", "mexc", "kraken"
        symbol: CCXT unified symbol, e.g. "ALKIMI/USDT"
        credentials: dict with api_key, api_secret, and optionally passphrase
        quote_currency: "USDT" or "USD" (Kraken uses "USD")
        ccxt_options: Optional dict of CCXT options to pass through

    Returns:
        BaseConnector instance. Returns CCXTWSConnector if use_websocket=True,
        otherwise CCXTConnector (REST-only).
    """
    ccxt_id = _CCXT_NAME_MAP.get(exchange_name)
    if ccxt_id is None:
        raise ValueError(f"Unsupported exchange: {exchange_name}. Must be one of {list(_CCXT_NAME_MAP)}")

    if use_websocket:
        return CCXTWSConnector(
            exchange_name=ccxt_id,
            symbol=symbol,
            credentials=credentials,
            quote_currency=quote_currency,
            ccxt_options=ccxt_options or {},
        )

    return CCXTConnector(
        exchange_name=ccxt_id,
        symbol=symbol,
        credentials=credentials,
        quote_currency=quote_currency,
        ccxt_options=ccxt_options or {},
    )
