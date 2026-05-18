"""
Pydantic response models for the FastAPI REST endpoints.
"""

from __future__ import annotations

from typing import Any, Optional
from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: str = "ok"
    version: str = "1.0.0"
    uptime_s: float


class ExchangeStatus(BaseModel):
    exchange: str
    running: bool
    dry_run: bool
    q_switch_triggered: bool
    circuit_breaker_tripped: bool
    circuit_breaker_reason: str
    open_orders: int
    global_mid: Optional[float]
    volatility: Optional[float]
    aggressiveness: Optional[float]
    zz_regime: Optional[str]
    balance_usd: float
    balance_token: float
    skew_factor: float
    token_drift_pct: float


class AllStatusResponse(BaseModel):
    exchanges: list[ExchangeStatus]
    global_mid: Optional[float]
    volatility: Optional[float]
    aggressiveness: Optional[float]
    zz_vol: Optional[float]
    zz_regime: Optional[str]
    contributing_exchanges: list[str]


class OrderResponse(BaseModel):
    id: str
    exchange: str
    symbol: str
    side: str
    price: float
    amount: float
    amount_usd: float
    status: str
    placed_at: float


class FillResponse(BaseModel):
    id: str
    order_id: str
    exchange: str
    symbol: str
    side: str
    filled_price: float
    filled_amount: float
    fee: float
    fee_currency: str
    filled_at: float
    pnl_usd: Optional[float]


class BalanceResponse(BaseModel):
    exchange: str
    usd: float
    token: float
    quote_currency: str


class MetricsResponse(BaseModel):
    global_mid: Optional[float]
    volatility: Optional[float]
    aggressiveness: Optional[float]
    zz_vol: Optional[float]
    zz_regime: Optional[str]
    fill_rate_1m: float
    pnl_1h: float
    total_open_orders: int


class ControlResponse(BaseModel):
    success: bool
    message: str


class ConfigResponse(BaseModel):
    config: dict[str, Any]
    message: str = "ok"
