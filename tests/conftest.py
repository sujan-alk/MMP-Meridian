"""
Shared fixtures for the Alkimi MM Platform test suite.

Provides:
- Mock exchange data (tickers, balances, candles, orders)
- Sample configs (SpreadConfig, DepthConfig, VolatilityConfig, SafetyConfig, etc.)
- Database fixtures (in-memory SQLite)
- Mock connectors and rate limiters
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from config.schema import (
    BotConfig,
    DepthConfig,
    ExchangeBotConfig,
    GlobalMidWeights,
    SafetyConfig,
    SpreadConfig,
    VolatilityConfig,
)
from db.database import Database
from exchange.base import Balance, Candle, Order, Ticker


# ---------------------------------------------------------------------------
# Sample data factories
# ---------------------------------------------------------------------------

def make_ticker(bid=0.10, ask=0.11, last=0.105) -> Ticker:
    """Create a sample Ticker."""
    return Ticker(bid=bid, ask=ask, mid=(bid + ask) / 2, last=last, timestamp=1700000000.0)


def make_balance(usd=1000.0, token=5000.0) -> Balance:
    """Create a sample Balance."""
    return Balance(usd=usd, token=token, quote_currency="USDT")


def make_order(
    id="order-001",
    exchange="kucoin",
    side="buy",
    price=0.10,
    amount=100.0,
    status="open",
) -> Order:
    """Create a sample Order."""
    return Order(
        id=id,
        exchange=exchange,
        symbol="ALKIMI/USDT",
        side=side,
        price=price,
        amount=amount,
        amount_usd=price * amount,
        status=status,
        timestamp=1700000000.0,
    )


def make_candle(open_=0.10, high=0.12, low=0.09, close=0.11, volume=50000.0) -> Candle:
    """Create a sample Candle."""
    return Candle(
        timestamp=1700000000.0,
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=volume,
    )


def make_candles(n: int = 10, base_price: float = 0.10) -> list[Candle]:
    """Generate n candles with slight variations around a base price."""
    candles = []
    for i in range(n):
        o = base_price + (i % 3) * 0.001
        h = o + 0.005
        l = o - 0.003
        c = o + 0.002
        candles.append(Candle(
            timestamp=1700000000.0 + i * 60,
            open=o, high=h, low=l, close=c, volume=50000.0 + i * 1000,
        ))
    return candles


# ---------------------------------------------------------------------------
# Config fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def spread_config() -> SpreadConfig:
    return SpreadConfig(
        buy_min_pct=-5.0,
        buy_max_pct=-0.1,
        sell_min_pct=0.3,
        sell_max_pct=7.0,
        curve_strength=4.0,
    )


@pytest.fixture
def depth_config() -> DepthConfig:
    return DepthConfig(
        levels=15,
        total_budget_usd=1000.0,
        curve_strength=4.0,
        min_order_usd=5.0,
    )


@pytest.fixture
def volatility_config() -> VolatilityConfig:
    return VolatilityConfig(
        window_minutes=10,
        low_threshold=0.001,
        high_threshold=0.003,
        power=2.0,
    )


@pytest.fixture
def safety_config() -> SafetyConfig:
    return SafetyConfig(
        min_balance_usd=50.0,
        min_balance_token=100.0,
        max_requests_per_second=8,
        heartbeat_interval_s=5.0,
        max_missed_heartbeats=3,
        max_daily_loss_pct=10.0,
        max_drawdown_pct=15.0,
    )


@pytest.fixture
def exchange_bot_config(spread_config, depth_config, safety_config) -> ExchangeBotConfig:
    return ExchangeBotConfig(
        exchange="kucoin",
        symbol="ALKIMI/USDT",
        quote_currency="USDT",
        enabled=True,
        spread=spread_config,
        depth=depth_config,
        safety=safety_config,
    )


@pytest.fixture
def bot_config(volatility_config, exchange_bot_config) -> BotConfig:
    return BotConfig(
        dry_run=True,
        global_mid_weights=GlobalMidWeights(kucoin=0.45, gate=0.45, mexc=0.05, kraken=0.05),
        volatility=volatility_config,
        exchanges=[exchange_bot_config],
    )


# ---------------------------------------------------------------------------
# Database fixture (in-memory)
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def db():
    """Provide an in-memory async SQLite database with schema applied."""
    database = Database(":memory:")
    await database.connect()
    yield database
    await database.disconnect()


# ---------------------------------------------------------------------------
# Mock connector
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_connector():
    """A fully-mocked BaseConnector for testing without exchange API calls."""
    connector = AsyncMock()
    connector.exchange_name = "kucoin"
    connector.symbol = "ALKIMI/USDT"
    connector.is_connected = True
    connector.fetch_ticker.return_value = make_ticker()
    connector.fetch_balance.return_value = make_balance()
    connector.fetch_open_orders.return_value = []
    connector.fetch_candles.return_value = make_candles()
    connector.create_limit_order.side_effect = lambda side, price, amount: make_order(
        id=f"LIVE-{side}-{price:.4f}",
        side=side,
        price=price,
        amount=amount,
    )
    connector.cancel_order.return_value = None
    connector.cancel_all_orders.return_value = None
    connector.fetch_fills.return_value = []
    connector.connect.return_value = None
    connector.disconnect.return_value = None
    return connector


# ---------------------------------------------------------------------------
# Mock rate limiter (no-op)
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_rate_limiter():
    """A rate limiter that never blocks."""
    limiter = AsyncMock()
    limiter.acquire.return_value = None
    return limiter
