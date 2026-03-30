"""
Exchange connector factory.
THIS IS THE ONLY FILE that imports concrete connector implementations.
Callers always receive a BaseConnector — swapping to C++ only changes this file.
"""

from __future__ import annotations

from exchange.base import BaseConnector
from exchange.ccxt_connector import CCXTConnector

# Map our exchange name → CCXT exchange id
_CCXT_NAME_MAP: dict[str, str] = {
    "kucoin": "kucoin",
    "gate": "gate",
    "mexc": "mexc",
    "kraken": "kraken",
}


def create_connector(
    exchange_name: str,
    symbol: str,
    credentials: dict,
    quote_currency: str = "USDT",
    ccxt_options: dict | None = None,
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
        BaseConnector instance (currently CCXTConnector)
    """
    ccxt_id = _CCXT_NAME_MAP.get(exchange_name)
    if ccxt_id is None:
        raise ValueError(f"Unsupported exchange: {exchange_name}. Must be one of {list(_CCXT_NAME_MAP)}")

    return CCXTConnector(
        exchange_name=ccxt_id,
        symbol=symbol,
        credentials=credentials,
        quote_currency=quote_currency,
        ccxt_options=ccxt_options or {},
    )
