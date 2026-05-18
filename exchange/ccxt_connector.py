"""
CCXT async_support connector — primary implementation of BaseConnector.
Handles all 4 exchanges (KuCoin, Gate, MEXC, Kraken) via the unified CCXT API.
Exchange-specific quirks are handled here; native fallbacks live in kucoin.py etc.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import ccxt.async_support as ccxt

from exchange.base import BaseConnector, Balance, Candle, Fill, Order, Ticker
from utils.logging import get_logger

log = get_logger("ccxt_connector")

# Map CCXT-specific error types to friendly messages
_RETRYABLE_ERRORS = (
    ccxt.NetworkError,
    ccxt.RequestTimeout,
    ccxt.DDoSProtection,
    ccxt.RateLimitExceeded,
)

_MAX_RETRIES = 3
_BASE_DELAY_S = 1.0


async def _retry(coro_fn, *args, max_retries: int = _MAX_RETRIES, **kwargs) -> Any:
    """Execute an async ccxt call with exponential back-off on retryable errors."""
    for attempt in range(max_retries + 1):
        try:
            return await coro_fn(*args, **kwargs)
        except _RETRYABLE_ERRORS as exc:
            if attempt == max_retries:
                raise
            delay = _BASE_DELAY_S * (2 ** attempt)
            log.warning("ccxt_retry", attempt=attempt + 1, delay=delay, error=str(exc))
            await asyncio.sleep(delay)
        except ccxt.AuthenticationError:
            raise  # Never retry auth errors
        except ccxt.ExchangeError as exc:
            log.error("ccxt_exchange_error", error=str(exc))
            raise


class CCXTConnector(BaseConnector):
    """
    CCXT-based connector. One instance per exchange.
    Callers should use exchange/factory.py to obtain instances.
    """

    def __init__(
        self,
        exchange_name: str,
        symbol: str,
        credentials: dict,
        quote_currency: str = "USDT",
        ccxt_options: dict | None = None,
    ):
        super().__init__(exchange_name, symbol)
        self.quote_currency = quote_currency
        self._token_currency = symbol.split("/")[0]  # e.g. "ALKIMI"

        exchange_class = getattr(ccxt, exchange_name, None)
        if exchange_class is None:
            raise ValueError(f"CCXT does not support exchange: {exchange_name}")

        init_params: dict = {
            "apiKey": credentials.get("api_key", ""),
            "secret": credentials.get("api_secret", ""),
            "enableRateLimit": True,
            "options": {"defaultType": "spot"},
        }
        if credentials.get("passphrase"):
            init_params["password"] = credentials["passphrase"]
        if ccxt_options:
            init_params["options"].update(ccxt_options)

        self._exchange: ccxt.Exchange = exchange_class(init_params)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        log.info("connector_connecting", exchange=self.exchange_name, symbol=self.symbol)
        await _retry(self._exchange.load_markets)
        self._connected = True
        log.info("connector_connected", exchange=self.exchange_name)

    async def disconnect(self) -> None:
        log.info("connector_disconnecting", exchange=self.exchange_name)
        try:
            await self._exchange.close()
        except Exception as exc:
            log.warning("connector_close_error", error=str(exc))
        finally:
            self._connected = False

    # ------------------------------------------------------------------
    # Market data
    # ------------------------------------------------------------------

    async def fetch_ticker(self) -> Ticker:
        raw = await _retry(self._exchange.fetch_ticker, self.symbol)
        bid = float(raw["bid"] or raw["last"])
        ask = float(raw["ask"] or raw["last"])
        return Ticker(
            bid=bid,
            ask=ask,
            mid=(bid + ask) / 2.0,
            last=float(raw["last"]),
            timestamp=raw["timestamp"] / 1000.0 if raw["timestamp"] else time.time(),
        )

    async def fetch_candles(self, timeframe: str = "1m", limit: int = 15) -> list[Candle]:
        raw = await _retry(self._exchange.fetch_ohlcv, self.symbol, timeframe, limit=limit)
        candles = []
        for row in raw:
            ts, o, h, l, c, v = row
            candles.append(Candle(
                timestamp=ts / 1000.0,
                open=float(o),
                high=float(h),
                low=float(l),
                close=float(c),
                volume=float(v),
            ))
        return candles

    # ------------------------------------------------------------------
    # Account
    # ------------------------------------------------------------------

    async def fetch_balance(self) -> Balance:
        raw = await _retry(self._exchange.fetch_balance)
        token_bal = float(raw.get("free", {}).get(self._token_currency, 0) or 0)
        usd_bal = float(raw.get("free", {}).get(self.quote_currency, 0) or 0)
        return Balance(usd=usd_bal, token=token_bal, quote_currency=self.quote_currency)

    # ------------------------------------------------------------------
    # Order management
    # ------------------------------------------------------------------

    async def create_limit_order(self, side: str, price: float, amount: float) -> Order:
        raw = await _retry(
            self._exchange.create_limit_order,
            self.symbol,
            side,
            amount,
            price,
        )
        return self._parse_order(raw)

    async def cancel_order(self, order_id: str) -> None:
        try:
            await _retry(self._exchange.cancel_order, order_id, self.symbol)
        except ccxt.OrderNotFound:
            log.debug("cancel_order_not_found", order_id=order_id)

    async def cancel_all_orders(self) -> None:
        try:
            await _retry(self._exchange.cancel_all_orders, self.symbol)
        except ccxt.NotSupported:
            # Fall back to cancelling individually
            open_orders = await self.fetch_open_orders()
            for order in open_orders:
                await self.cancel_order(order.id)

    async def fetch_open_orders(self) -> list[Order]:
        raw_orders = await _retry(self._exchange.fetch_open_orders, self.symbol)
        return [self._parse_order(o) for o in raw_orders]

    async def fetch_fills(self, since_ts: float | None = None, limit: int = 100) -> list[Fill]:
        since_ms = int(since_ts * 1000) if since_ts is not None else None
        try:
            raw = await _retry(self._exchange.fetch_my_trades, self.symbol, since_ms, limit=limit)
        except ccxt.NotSupported:
            log.debug("fetch_fills_not_supported", exchange=self.exchange_name)
            return []
        return [self._parse_fill(t) for t in raw]

    async def reconnect(self) -> None:
        """Re-establish the exchange connection after a drop."""
        log.warning("connector_reconnecting", exchange=self.exchange_name)
        self._connected = False
        try:
            await self._exchange.close()
        except Exception:
            pass
        await asyncio.sleep(2.0)
        await self.connect()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _parse_order(self, raw: dict) -> Order:
        price = float(raw.get("price") or raw.get("average") or 0)
        amount = float(raw.get("amount") or 0)
        return Order(
            id=str(raw["id"]),
            exchange=self.exchange_name,
            symbol=self.symbol,
            side=raw["side"],
            price=price,
            amount=amount,
            amount_usd=price * amount,
            status=raw.get("status", "open"),
            timestamp=raw["timestamp"] / 1000.0 if raw.get("timestamp") else time.time(),
            filled_amount=float(raw.get("filled") or 0),
            filled_price=float(raw.get("average") or 0),
            fee=float((raw.get("fee") or {}).get("cost") or 0),
            fee_currency=str((raw.get("fee") or {}).get("currency") or ""),
        )

    def _parse_fill(self, raw: dict) -> Fill:
        return Fill(
            id=str(raw["id"]),
            order_id=str(raw.get("order") or ""),
            exchange=self.exchange_name,
            symbol=self.symbol,
            side=raw["side"],
            filled_price=float(raw.get("price") or 0),
            filled_amount=float(raw.get("amount") or 0),
            fee=float((raw.get("fee") or {}).get("cost") or 0),
            fee_currency=str((raw.get("fee") or {}).get("currency") or ""),
            timestamp=raw["timestamp"] / 1000.0 if raw.get("timestamp") else time.time(),
        )
