"""
Runtime settings loader.
- Secrets (API keys) come from environment variables / Railway env vars.
- Bot config (spread, depth, vol params) comes from bot.json.
- Both are validated via Pydantic.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from config.schema import BotConfig


# ---------------------------------------------------------------------------
# Exchange credentials (from environment — never in bot.json)
# ---------------------------------------------------------------------------

class ExchangeSecrets(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # KuCoin
    kucoin_api_key: str = Field(default="")
    kucoin_api_secret: str = Field(default="")
    kucoin_passphrase: str = Field(default="")

    # Gate.io
    gate_api_key: str = Field(default="")
    gate_api_secret: str = Field(default="")

    # MEXC
    mexc_api_key: str = Field(default="")
    mexc_api_secret: str = Field(default="")

    # Kraken
    kraken_api_key: str = Field(default="")
    kraken_api_secret: str = Field(default="")

    def credentials_for(self, exchange: str) -> dict:
        """Return a dict with api_key, api_secret, and optionally passphrase."""
        if exchange == "kucoin":
            return {
                "api_key": self.kucoin_api_key,
                "api_secret": self.kucoin_api_secret,
                "passphrase": self.kucoin_passphrase,
            }
        if exchange == "gate":
            return {"api_key": self.gate_api_key, "api_secret": self.gate_api_secret}
        if exchange == "mexc":
            return {"api_key": self.mexc_api_key, "api_secret": self.mexc_api_secret}
        if exchange == "kraken":
            return {"api_key": self.kraken_api_key, "api_secret": self.kraken_api_secret}
        raise ValueError(f"Unknown exchange: {exchange}")


# ---------------------------------------------------------------------------
# Runtime settings
# ---------------------------------------------------------------------------

class RuntimeSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    live_mode: bool = Field(default=False, description="Set LIVE_MODE=true to place real orders")
    port: int = Field(default=8000)
    db_path: str = Field(default="data/mm_bot.db")
    bot_config_path: str = Field(default="bot.json")
    log_level: str = Field(default="INFO")
    alert_webhook_url: str | None = Field(default=None)
    api_key: str | None = Field(default=None, description="X-API-Key required for control/config endpoints. Unset = no auth.")


# ---------------------------------------------------------------------------
# Bot config loader (bot.json)
# ---------------------------------------------------------------------------

def load_bot_config(path: str | None = None) -> BotConfig:
    """Load and validate bot.json. Returns a BotConfig instance."""
    config_path = Path(path or os.environ.get("BOT_CONFIG_PATH", "bot.json"))
    if not config_path.exists():
        raise FileNotFoundError(f"Bot config not found: {config_path.resolve()}")
    with open(config_path) as f:
        data = json.load(f)
    return BotConfig.model_validate(data)


def save_bot_config(config: BotConfig, path: str | None = None) -> None:
    """Persist a BotConfig back to bot.json (used by the hot-reload API endpoint)."""
    config_path = Path(path or os.environ.get("BOT_CONFIG_PATH", "bot.json"))
    with open(config_path, "w") as f:
        json.dump(config.model_dump(), f, indent=2)


@lru_cache(maxsize=1)
def get_runtime_settings() -> RuntimeSettings:
    return RuntimeSettings()


@lru_cache(maxsize=1)
def get_secrets() -> ExchangeSecrets:
    return ExchangeSecrets()
