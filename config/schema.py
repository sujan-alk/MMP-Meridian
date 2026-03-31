"""
Pydantic config models for the ALKIMI MM Bot.
bot.json holds non-secret config; secrets come from environment variables.
"""

from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, Field, field_validator


# ---------------------------------------------------------------------------
# Spread / Depth / Safety sub-models
# ---------------------------------------------------------------------------

class SpreadConfig(BaseModel):
    """Controls how bid/ask levels are distributed using the power-curve model."""
    buy_min_pct: float = Field(default=-5.0, description="Furthest buy spread (most passive)")
    buy_max_pct: float = Field(default=-0.1, description="Tightest buy spread (most aggressive)")
    sell_min_pct: float = Field(default=0.3, description="Tightest sell spread (most aggressive)")
    sell_max_pct: float = Field(default=7.0, description="Furthest sell spread (most passive)")
    curve_strength: float = Field(default=4.0, ge=0.5, le=10.0)

    @field_validator("buy_min_pct")
    @classmethod
    def buy_min_must_be_negative(cls, v: float) -> float:
        if v >= 0:
            raise ValueError("buy_min_pct must be negative")
        return v

    @field_validator("buy_max_pct")
    @classmethod
    def buy_max_must_be_negative(cls, v: float) -> float:
        if v >= 0:
            raise ValueError("buy_max_pct must be negative")
        return v


class DepthConfig(BaseModel):
    """Controls how total budget is distributed across order levels."""
    levels: int = Field(default=15, ge=3, le=30, description="Number of orders per side")
    total_budget_usd: float = Field(default=1000.0, gt=0, description="Total USD allocated (split buy/sell)")
    curve_strength: float = Field(default=4.0, ge=0.5, le=10.0, description="Amount distribution curve strength")
    min_order_usd: float = Field(default=5.0, gt=0, description="Minimum single order size in USD")


class VolatilityConfig(BaseModel):
    """Controls the rolling volatility window and aggressiveness mapping."""
    window_minutes: int = Field(default=10, ge=1, le=60)
    low_threshold: float = Field(
        default=0.001, gt=0,
        description="vol <= this → aggressiveness = 1.0 (fully aggressive)"
    )
    high_threshold: float = Field(
        default=0.003, gt=0,
        description="vol >= this → aggressiveness = 0.0 (fully passive)"
    )
    power: float = Field(
        default=2.0, ge=1.0, le=5.0,
        description="Curve power: 1=linear, 2=quadratic, 3=cubic"
    )
    trending_threshold: float = Field(
        default=0.0015, gt=0,
        description="Mean candle direction above this → trending regime"
    )
    choppy_threshold: float = Field(
        default=0.0005, ge=0,
        description="Mean candle direction below this → choppy regime"
    )

    @field_validator("high_threshold")
    @classmethod
    def high_must_exceed_low(cls, v: float, info) -> float:
        low = info.data.get("low_threshold")
        if low is not None and v <= low:
            raise ValueError("high_threshold must be greater than low_threshold")
        return v


class SafetyConfig(BaseModel):
    """Per-exchange safety thresholds."""
    min_balance_usd: float = Field(default=50.0, ge=0)
    min_balance_token: float = Field(default=100.0, ge=0)
    max_requests_per_second: int = Field(default=8, ge=1, le=30)
    heartbeat_interval_s: float = Field(default=5.0, ge=1.0)
    max_missed_heartbeats: int = Field(default=3, ge=1)
    max_daily_loss_pct: float = Field(default=10.0, gt=0, le=100)
    max_drawdown_pct: float = Field(default=15.0, gt=0, le=100)
    alert_webhook_url: str | None = Field(default=None)


# ---------------------------------------------------------------------------
# Exchange-level config
# ---------------------------------------------------------------------------

ExchangeName = Literal["kucoin", "gate", "mexc", "kraken"]
QuoteCurrency = Literal["USDT", "USD"]


class ExchangeBotConfig(BaseModel):
    """Config for one exchange instance."""
    exchange: ExchangeName
    symbol: str = Field(description="CCXT unified symbol, e.g. ALKIMI/USDT")
    quote_currency: QuoteCurrency = Field(default="USDT")
    enabled: bool = True
    spread: SpreadConfig = Field(default_factory=SpreadConfig)
    depth: DepthConfig = Field(default_factory=DepthConfig)
    safety: SafetyConfig = Field(default_factory=SafetyConfig)
    # Per-exchange CCXT options override (e.g. {"defaultType": "spot"})
    ccxt_options: dict = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Global mid-price weights
# ---------------------------------------------------------------------------

class GlobalMidWeights(BaseModel):
    """
    Weighted average: global_mid = sum(weight * exchange_mid).
    Weights must sum to 1.0.
    """
    kucoin: float = Field(default=0.45, ge=0.0, le=1.0)
    gate: float = Field(default=0.45, ge=0.0, le=1.0)
    mexc: float = Field(default=0.05, ge=0.0, le=1.0)
    kraken: float = Field(default=0.05, ge=0.0, le=1.0)

    @field_validator("kraken")
    @classmethod
    def weights_must_sum_to_one(cls, v: float, info) -> float:
        data = info.data
        total = data.get("kucoin", 0) + data.get("gate", 0) + data.get("mexc", 0) + v
        if abs(total - 1.0) > 0.001:
            raise ValueError(f"GlobalMidWeights must sum to 1.0 (got {total:.4f})")
        return v

    def as_dict(self) -> dict[str, float]:
        return {"kucoin": self.kucoin, "gate": self.gate, "mexc": self.mexc, "kraken": self.kraken}


# ---------------------------------------------------------------------------
# Initial balances
# ---------------------------------------------------------------------------

class ExchangeInitialBalance(BaseModel):
    usd: float = Field(default=0.0, ge=0)
    token: float = Field(default=0.0, ge=0)


# ---------------------------------------------------------------------------
# Top-level bot config (bot.json)
# ---------------------------------------------------------------------------

class BotConfig(BaseModel):
    """
    Complete bot configuration. Loaded from bot.json.
    Secrets (API keys) are loaded separately via pydantic-settings.
    This model is also used for the hot-reload PUT /api/config endpoint.
    """
    dry_run: bool = Field(default=True, description="If true, calculate orders but do NOT place them")
    global_mid_weights: GlobalMidWeights = Field(default_factory=GlobalMidWeights)
    volatility: VolatilityConfig = Field(default_factory=VolatilityConfig)
    exchanges: list[ExchangeBotConfig] = Field(default_factory=list, min_length=1)
    initial_balances: dict[ExchangeName, ExchangeInitialBalance] = Field(default_factory=dict)

    def get_exchange_config(self, name: ExchangeName) -> ExchangeBotConfig | None:
        for ex in self.exchanges:
            if ex.exchange == name:
                return ex
        return None

    def enabled_exchanges(self) -> list[ExchangeBotConfig]:
        return [ex for ex in self.exchanges if ex.enabled]
