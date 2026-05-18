"""
Tests for exchange/factory.py — C++ connector routing logic (Step 8).

Validates:
- Default behaviour: create_connector returns CCXTConnector (no env var set)
- USE_CPP_CONNECTOR=true → C++ connector returned for all 4 exchanges
- USE_CPP_CONNECTOR=false → CCXT connector returned even when cpp is available
- use_cpp=True kwarg overrides env var to force C++
- use_cpp=False kwarg overrides env var to force CCXT
- Per-exchange use_cpp_connector config field flows through correctly
- Fallback to CCXT when _CPP_AVAILABLE is False (even with USE_CPP_CONNECTOR=true)
- Fallback to CCXT for unsupported exchange (e.g. binance) even when use_cpp=True
- use_websocket=True (CCXT path) returns CCXTWSConnector
- create_ws_connector always returns CCXTWSConnector regardless of env var
- ValueError raised for unknown exchange name
- _want_cpp() helper logic
- ExchangeBotConfig schema: use_cpp_connector defaults to None, accepts True/False
- Schema validation: use_cpp_connector is optional — existing bot.json still loads
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from config.schema import ExchangeBotConfig, SpreadConfig, DepthConfig, SafetyConfig
from exchange.ccxt_connector import CCXTConnector
from exchange.ccxt_ws_connector import CCXTWSConnector
from exchange.cpp_connector import (
    CppGateConnector,
    CppKrakenConnector,
    CppKuCoinConnector,
    CppMexcConnector,
)
from exchange.factory import (
    _CPP_CONNECTOR_MAP,
    _want_cpp,
    create_connector,
    create_ws_connector,
)

# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------

_KUCOIN_CREDS  = {"api_key": "k", "api_secret": "s", "passphrase": "p"}
_GATE_CREDS    = {"api_key": "k", "api_secret": "s"}
_MEXC_CREDS    = {"api_key": "k", "api_secret": "s"}
_KRAKEN_CREDS  = {"api_key": "k", "api_secret": "s"}
_BINANCE_CREDS = {"api_key": "k", "api_secret": "s"}


def _make_exchange_cfg(exchange="kucoin", use_cpp_connector=None):
    return ExchangeBotConfig(
        exchange=exchange,
        symbol="ALKIMI/USDT",
        quote_currency="USDT",
        enabled=True,
        spread=SpreadConfig(),
        depth=DepthConfig(),
        safety=SafetyConfig(),
        use_cpp_connector=use_cpp_connector,
    )


# ---------------------------------------------------------------------------
# TestWantCppHelper
# ---------------------------------------------------------------------------

class TestWantCppHelper:
    def test_explicit_true_overrides_env(self, monkeypatch):
        monkeypatch.setenv("USE_CPP_CONNECTOR", "false")
        assert _want_cpp(True) is True

    def test_explicit_false_overrides_env(self, monkeypatch):
        monkeypatch.setenv("USE_CPP_CONNECTOR", "true")
        assert _want_cpp(False) is False

    def test_none_reads_env_true(self, monkeypatch):
        monkeypatch.setenv("USE_CPP_CONNECTOR", "true")
        assert _want_cpp(None) is True

    def test_none_reads_env_false(self, monkeypatch):
        monkeypatch.setenv("USE_CPP_CONNECTOR", "false")
        assert _want_cpp(None) is False

    def test_none_with_no_env_defaults_false(self, monkeypatch):
        monkeypatch.delenv("USE_CPP_CONNECTOR", raising=False)
        assert _want_cpp(None) is False

    def test_env_value_1_is_truthy(self, monkeypatch):
        monkeypatch.setenv("USE_CPP_CONNECTOR", "1")
        assert _want_cpp(None) is True

    def test_env_value_yes_is_truthy(self, monkeypatch):
        monkeypatch.setenv("USE_CPP_CONNECTOR", "yes")
        assert _want_cpp(None) is True

    def test_env_case_insensitive(self, monkeypatch):
        monkeypatch.setenv("USE_CPP_CONNECTOR", "TRUE")
        assert _want_cpp(None) is True

    def test_env_empty_string_is_false(self, monkeypatch):
        monkeypatch.setenv("USE_CPP_CONNECTOR", "")
        assert _want_cpp(None) is False


# ---------------------------------------------------------------------------
# TestCppConnectorMap
# ---------------------------------------------------------------------------

class TestCppConnectorMap:
    def test_all_four_exchanges_in_map(self):
        for ex in ("kucoin", "gate", "mexc", "kraken"):
            assert ex in _CPP_CONNECTOR_MAP, f"{ex} missing from _CPP_CONNECTOR_MAP"

    def test_kucoin_maps_to_correct_class(self):
        assert _CPP_CONNECTOR_MAP["kucoin"] is CppKuCoinConnector

    def test_gate_maps_to_correct_class(self):
        assert _CPP_CONNECTOR_MAP["gate"] is CppGateConnector

    def test_mexc_maps_to_correct_class(self):
        assert _CPP_CONNECTOR_MAP["mexc"] is CppMexcConnector

    def test_kraken_maps_to_correct_class(self):
        assert _CPP_CONNECTOR_MAP["kraken"] is CppKrakenConnector

    def test_binance_not_in_cpp_map(self):
        assert "binance" not in _CPP_CONNECTOR_MAP


# ---------------------------------------------------------------------------
# TestCreateConnectorDefaultCCXT
# ---------------------------------------------------------------------------

class TestCreateConnectorDefaultCCXT:
    """No env var set → always CCXT."""

    def test_kucoin_default_returns_ccxt(self, monkeypatch):
        monkeypatch.delenv("USE_CPP_CONNECTOR", raising=False)
        conn = create_connector("kucoin", "ALKIMI/USDT", _KUCOIN_CREDS)
        assert isinstance(conn, CCXTConnector)

    def test_gate_default_returns_ccxt(self, monkeypatch):
        monkeypatch.delenv("USE_CPP_CONNECTOR", raising=False)
        conn = create_connector("gate", "ALKIMI/USDT", _GATE_CREDS)
        assert isinstance(conn, CCXTConnector)

    def test_mexc_default_returns_ccxt(self, monkeypatch):
        monkeypatch.delenv("USE_CPP_CONNECTOR", raising=False)
        conn = create_connector("mexc", "ALKIMI/USDT", _MEXC_CREDS)
        assert isinstance(conn, CCXTConnector)

    def test_kraken_default_returns_ccxt(self, monkeypatch):
        monkeypatch.delenv("USE_CPP_CONNECTOR", raising=False)
        conn = create_connector("kraken", "ALKIMI/USD", _KRAKEN_CREDS,
                                quote_currency="USD")
        assert isinstance(conn, CCXTConnector)

    def test_use_websocket_returns_ccxt_ws(self, monkeypatch):
        monkeypatch.delenv("USE_CPP_CONNECTOR", raising=False)
        conn = create_connector("kucoin", "ALKIMI/USDT", _KUCOIN_CREDS,
                                use_websocket=True)
        assert isinstance(conn, CCXTWSConnector)

    def test_env_false_returns_ccxt(self, monkeypatch):
        monkeypatch.setenv("USE_CPP_CONNECTOR", "false")
        conn = create_connector("kucoin", "ALKIMI/USDT", _KUCOIN_CREDS)
        assert isinstance(conn, CCXTConnector)


# ---------------------------------------------------------------------------
# TestCreateConnectorCppEnvVar
# ---------------------------------------------------------------------------

class TestCreateConnectorCppEnvVar:
    """USE_CPP_CONNECTOR=true → C++ connector for all 4 exchanges."""

    def test_kucoin_env_true_returns_cpp(self, monkeypatch):
        monkeypatch.setenv("USE_CPP_CONNECTOR", "true")
        conn = create_connector("kucoin", "ALKIMI/USDT", _KUCOIN_CREDS)
        assert isinstance(conn, CppKuCoinConnector)

    def test_gate_env_true_returns_cpp(self, monkeypatch):
        monkeypatch.setenv("USE_CPP_CONNECTOR", "true")
        conn = create_connector("gate", "ALKIMI/USDT", _GATE_CREDS)
        assert isinstance(conn, CppGateConnector)

    def test_mexc_env_true_returns_cpp(self, monkeypatch):
        monkeypatch.setenv("USE_CPP_CONNECTOR", "true")
        conn = create_connector("mexc", "ALKIMI/USDT", _MEXC_CREDS)
        assert isinstance(conn, CppMexcConnector)

    def test_kraken_env_true_returns_cpp(self, monkeypatch):
        monkeypatch.setenv("USE_CPP_CONNECTOR", "true")
        conn = create_connector("kraken", "ALKIMI/USD", _KRAKEN_CREDS,
                                quote_currency="USD")
        assert isinstance(conn, CppKrakenConnector)

    def test_cpp_connector_has_correct_exchange_name(self, monkeypatch):
        monkeypatch.setenv("USE_CPP_CONNECTOR", "true")
        conn = create_connector("kucoin", "ALKIMI/USDT", _KUCOIN_CREDS)
        assert conn.exchange_name == "kucoin"

    def test_cpp_connector_has_correct_symbol(self, monkeypatch):
        monkeypatch.setenv("USE_CPP_CONNECTOR", "true")
        conn = create_connector("gate", "ALKIMI/USDT", _GATE_CREDS)
        assert conn.symbol == "ALKIMI/USDT"

    def test_cpp_connector_not_connected_at_creation(self, monkeypatch):
        monkeypatch.setenv("USE_CPP_CONNECTOR", "true")
        conn = create_connector("mexc", "ALKIMI/USDT", _MEXC_CREDS)
        assert conn.is_connected is False

    def test_env_value_1_activates_cpp(self, monkeypatch):
        monkeypatch.setenv("USE_CPP_CONNECTOR", "1")
        conn = create_connector("kucoin", "ALKIMI/USDT", _KUCOIN_CREDS)
        assert isinstance(conn, CppKuCoinConnector)


# ---------------------------------------------------------------------------
# TestCreateConnectorKwargOverride
# ---------------------------------------------------------------------------

class TestCreateConnectorKwargOverride:
    """use_cpp kwarg takes priority over the env var."""

    def test_use_cpp_true_overrides_env_false(self, monkeypatch):
        monkeypatch.setenv("USE_CPP_CONNECTOR", "false")
        conn = create_connector("kucoin", "ALKIMI/USDT", _KUCOIN_CREDS, use_cpp=True)
        assert isinstance(conn, CppKuCoinConnector)

    def test_use_cpp_false_overrides_env_true(self, monkeypatch):
        monkeypatch.setenv("USE_CPP_CONNECTOR", "true")
        conn = create_connector("kucoin", "ALKIMI/USDT", _KUCOIN_CREDS, use_cpp=False)
        assert isinstance(conn, CCXTConnector)

    def test_use_cpp_none_falls_through_to_env(self, monkeypatch):
        monkeypatch.setenv("USE_CPP_CONNECTOR", "true")
        conn = create_connector("gate", "ALKIMI/USDT", _GATE_CREDS, use_cpp=None)
        assert isinstance(conn, CppGateConnector)

    def test_use_cpp_true_gate_returns_cpp_gate(self, monkeypatch):
        monkeypatch.delenv("USE_CPP_CONNECTOR", raising=False)
        conn = create_connector("gate", "ALKIMI/USDT", _GATE_CREDS, use_cpp=True)
        assert isinstance(conn, CppGateConnector)

    def test_use_cpp_false_with_no_env_returns_ccxt(self, monkeypatch):
        monkeypatch.delenv("USE_CPP_CONNECTOR", raising=False)
        conn = create_connector("mexc", "ALKIMI/USDT", _MEXC_CREDS, use_cpp=False)
        assert isinstance(conn, CCXTConnector)


# ---------------------------------------------------------------------------
# TestCreateConnectorFallbacks
# ---------------------------------------------------------------------------

class TestCreateConnectorFallbacks:
    """Verify graceful fallback when C++ is requested but unavailable."""

    def test_fallback_to_ccxt_when_cpp_so_not_built(self, monkeypatch):
        monkeypatch.setenv("USE_CPP_CONNECTOR", "true")
        with patch("exchange.factory._CPP_AVAILABLE", False):
            conn = create_connector("kucoin", "ALKIMI/USDT", _KUCOIN_CREDS)
        assert isinstance(conn, CCXTConnector)

    def test_fallback_to_ccxt_for_unsupported_exchange(self, monkeypatch):
        # binance has no C++ connector
        monkeypatch.setenv("USE_CPP_CONNECTOR", "true")
        conn = create_connector("binance", "BTC/USDT", _BINANCE_CREDS)
        assert isinstance(conn, CCXTConnector)

    def test_fallback_to_ccxt_unsupported_even_with_kwarg(self, monkeypatch):
        monkeypatch.delenv("USE_CPP_CONNECTOR", raising=False)
        conn = create_connector("binance", "BTC/USDT", _BINANCE_CREDS, use_cpp=True)
        assert isinstance(conn, CCXTConnector)

    def test_fallback_when_cpp_available_false_gate(self, monkeypatch):
        monkeypatch.setenv("USE_CPP_CONNECTOR", "true")
        with patch("exchange.factory._CPP_AVAILABLE", False):
            conn = create_connector("gate", "ALKIMI/USDT", _GATE_CREDS)
        assert isinstance(conn, CCXTConnector)

    def test_fallback_when_cpp_available_false_with_kwarg(self, monkeypatch):
        monkeypatch.delenv("USE_CPP_CONNECTOR", raising=False)
        with patch("exchange.factory._CPP_AVAILABLE", False):
            conn = create_connector("mexc", "ALKIMI/USDT", _MEXC_CREDS, use_cpp=True)
        assert isinstance(conn, CCXTConnector)


# ---------------------------------------------------------------------------
# TestCreateConnectorErrors
# ---------------------------------------------------------------------------

class TestCreateConnectorErrors:
    def test_unknown_exchange_raises_value_error(self, monkeypatch):
        monkeypatch.delenv("USE_CPP_CONNECTOR", raising=False)
        with pytest.raises(ValueError, match="Unsupported exchange"):
            create_connector("bybit", "BTC/USDT", {"api_key": "k", "api_secret": "s"})

    def test_unknown_exchange_with_cpp_true_raises(self, monkeypatch):
        # Even with use_cpp=True, unsupported exchange falls to CCXT which raises
        with pytest.raises(ValueError, match="Unsupported exchange"):
            create_connector("bybit", "BTC/USDT", {"api_key": "k", "api_secret": "s"},
                             use_cpp=False)


# ---------------------------------------------------------------------------
# TestCreateWsConnector
# ---------------------------------------------------------------------------

class TestCreateWsConnector:
    """create_ws_connector always returns CCXTWSConnector — never C++."""

    def test_returns_ccxt_ws_by_default(self, monkeypatch):
        monkeypatch.delenv("USE_CPP_CONNECTOR", raising=False)
        conn = create_ws_connector("kucoin", "ALKIMI/USDT", _KUCOIN_CREDS)
        assert isinstance(conn, CCXTWSConnector)

    def test_returns_ccxt_ws_even_with_env_true(self, monkeypatch):
        monkeypatch.setenv("USE_CPP_CONNECTOR", "true")
        conn = create_ws_connector("gate", "ALKIMI/USDT", _GATE_CREDS)
        assert isinstance(conn, CCXTWSConnector)

    def test_returns_ccxt_ws_for_mexc(self, monkeypatch):
        monkeypatch.delenv("USE_CPP_CONNECTOR", raising=False)
        conn = create_ws_connector("mexc", "ALKIMI/USDT", _MEXC_CREDS)
        assert isinstance(conn, CCXTWSConnector)

    def test_returns_ccxt_ws_for_kraken(self, monkeypatch):
        monkeypatch.delenv("USE_CPP_CONNECTOR", raising=False)
        conn = create_ws_connector("kraken", "ALKIMI/USD", _KRAKEN_CREDS,
                                   quote_currency="USD")
        assert isinstance(conn, CCXTWSConnector)

    def test_unknown_exchange_raises(self):
        with pytest.raises(ValueError, match="Unsupported exchange"):
            create_ws_connector("bybit", "BTC/USDT", {"api_key": "k", "api_secret": "s"})


# ---------------------------------------------------------------------------
# TestExchangeBotConfigSchema
# ---------------------------------------------------------------------------

class TestExchangeBotConfigSchema:
    """Verify that use_cpp_connector is correctly modelled in the config schema."""

    def test_default_is_none(self):
        cfg = _make_exchange_cfg()
        assert cfg.use_cpp_connector is None

    def test_explicit_true(self):
        cfg = _make_exchange_cfg(use_cpp_connector=True)
        assert cfg.use_cpp_connector is True

    def test_explicit_false(self):
        cfg = _make_exchange_cfg(use_cpp_connector=False)
        assert cfg.use_cpp_connector is False

    def test_existing_bot_json_without_field_still_parses(self):
        """bot.json does not have use_cpp_connector — must default to None."""
        data = {
            "exchange": "kucoin",
            "symbol": "ALKIMI/USDT",
            "quote_currency": "USDT",
            "enabled": True,
            "spread": {"buy_min_pct": -5.0, "buy_max_pct": -0.1,
                       "sell_min_pct": 0.3, "sell_max_pct": 7.0,
                       "curve_strength": 4.0},
            "depth": {"levels": 15, "total_budget_usd": 1000.0,
                      "curve_strength": 4.0, "min_order_usd": 5.0},
            "safety": {"min_balance_usd": 0.0, "min_balance_token": 0.0,
                       "max_requests_per_second": 15,
                       "heartbeat_interval_s": 5.0, "max_missed_heartbeats": 6,
                       "max_daily_loss_pct": 10.0, "max_drawdown_pct": 15.0},
        }
        cfg = ExchangeBotConfig(**data)
        assert cfg.use_cpp_connector is None

    def test_per_exchange_override_flows_to_factory_cpp(self, monkeypatch):
        """use_cpp_connector=True in config selects C++ via create_connector use_cpp kwarg."""
        monkeypatch.delenv("USE_CPP_CONNECTOR", raising=False)
        cfg = _make_exchange_cfg("kucoin", use_cpp_connector=True)
        conn = create_connector(
            exchange_name=cfg.exchange,
            symbol=cfg.symbol,
            credentials=_KUCOIN_CREDS,
            quote_currency=cfg.quote_currency,
            use_cpp=cfg.use_cpp_connector,
        )
        assert isinstance(conn, CppKuCoinConnector)

    def test_per_exchange_override_false_forces_ccxt(self, monkeypatch):
        """use_cpp_connector=False in config forces CCXT even when env var is true."""
        monkeypatch.setenv("USE_CPP_CONNECTOR", "true")
        cfg = _make_exchange_cfg("gate", use_cpp_connector=False)
        conn = create_connector(
            exchange_name=cfg.exchange,
            symbol=cfg.symbol,
            credentials=_GATE_CREDS,
            quote_currency=cfg.quote_currency,
            use_cpp=cfg.use_cpp_connector,
        )
        assert isinstance(conn, CCXTConnector)

    def test_per_exchange_none_defers_to_env_true(self, monkeypatch):
        """use_cpp_connector=None (default) reads env var."""
        monkeypatch.setenv("USE_CPP_CONNECTOR", "true")
        cfg = _make_exchange_cfg("mexc", use_cpp_connector=None)
        conn = create_connector(
            exchange_name=cfg.exchange,
            symbol=cfg.symbol,
            credentials=_MEXC_CREDS,
            quote_currency=cfg.quote_currency,
            use_cpp=cfg.use_cpp_connector,
        )
        assert isinstance(conn, CppMexcConnector)

    def test_kraken_config_uses_usd(self, monkeypatch):
        monkeypatch.setenv("USE_CPP_CONNECTOR", "true")
        cfg = ExchangeBotConfig(
            exchange="kraken",
            symbol="ALKIMI/USD",
            quote_currency="USD",
            enabled=True,
            spread=SpreadConfig(),
            depth=DepthConfig(),
            safety=SafetyConfig(),
        )
        conn = create_connector(
            exchange_name=cfg.exchange,
            symbol=cfg.symbol,
            credentials=_KRAKEN_CREDS,
            quote_currency=cfg.quote_currency,
            use_cpp=cfg.use_cpp_connector,
        )
        assert isinstance(conn, CppKrakenConnector)
        assert conn.symbol == "ALKIMI/USD"
