/**
 * bindings.cpp — pybind11 module definition.
 *
 * Exposes all 4 C++ connector classes and their shared data structures to Python.
 * The Python wrapper (exchange/cpp_connector.py) imports this module and wraps
 * each class in an async BaseConnector-compatible interface.
 *
 * Data structures are exposed as simple Python objects with named attributes.
 * cpp_connector.py converts them into the exchange/base.py dataclasses that
 * the rest of the bot expects.
 */

#include <pybind11/pybind11.h>
#include <pybind11/stl.h>        // std::vector ↔ Python list automatic conversion

#include "connector.h"
#include "kucoin_connector.h"
#include "gate_connector.h"
#include "mexc_connector.h"
#include "kraken_connector.h"

namespace py = pybind11;

PYBIND11_MODULE(alkimi_cpp_connectors, m) {
    m.doc() = "Alkimi C++ exchange connectors — low-latency WebSocket market making";

    // -----------------------------------------------------------------------
    // Data structures
    // Exposed as Python classes with readable attributes.
    // cpp_connector.py converts these into exchange/base.py dataclasses.
    // -----------------------------------------------------------------------

    py::class_<Ticker>(m, "Ticker")
        .def(py::init<>())
        .def_readwrite("bid",       &Ticker::bid)
        .def_readwrite("ask",       &Ticker::ask)
        .def_readwrite("mid",       &Ticker::mid)
        .def_readwrite("last",      &Ticker::last)
        .def_readwrite("timestamp", &Ticker::timestamp)
        .def("__repr__", [](const Ticker& t) {
            return "<Ticker bid=" + std::to_string(t.bid) +
                   " ask="  + std::to_string(t.ask) +
                   " mid="  + std::to_string(t.mid) + ">";
        });

    py::class_<Candle>(m, "Candle")
        .def(py::init<>())
        .def_readwrite("timestamp", &Candle::timestamp)
        .def_readwrite("open",      &Candle::open)
        .def_readwrite("high",      &Candle::high)
        .def_readwrite("low",       &Candle::low)
        .def_readwrite("close",     &Candle::close)
        .def_readwrite("volume",    &Candle::volume);

    py::class_<Balance>(m, "Balance")
        .def(py::init<>())
        .def_readwrite("usd",            &Balance::usd)
        .def_readwrite("token",          &Balance::token)
        .def_readwrite("quote_currency", &Balance::quote_currency)
        .def("__repr__", [](const Balance& b) {
            return "<Balance usd=" + std::to_string(b.usd) +
                   " token=" + std::to_string(b.token) + ">";
        });

    py::class_<Order>(m, "Order")
        .def(py::init<>())
        .def_readwrite("id",            &Order::id)
        .def_readwrite("exchange",      &Order::exchange)
        .def_readwrite("symbol",        &Order::symbol)
        .def_readwrite("side",          &Order::side)
        .def_readwrite("price",         &Order::price)
        .def_readwrite("amount",        &Order::amount)
        .def_readwrite("amount_usd",    &Order::amount_usd)
        .def_readwrite("status",        &Order::status)
        .def_readwrite("timestamp",     &Order::timestamp)
        .def_readwrite("filled_amount", &Order::filled_amount)
        .def_readwrite("filled_price",  &Order::filled_price)
        .def_readwrite("fee",           &Order::fee)
        .def_readwrite("fee_currency",  &Order::fee_currency);

    py::class_<Fill>(m, "Fill")
        .def(py::init<>())
        .def_readwrite("id",            &Fill::id)
        .def_readwrite("order_id",      &Fill::order_id)
        .def_readwrite("exchange",      &Fill::exchange)
        .def_readwrite("symbol",        &Fill::symbol)
        .def_readwrite("side",          &Fill::side)
        .def_readwrite("filled_price",  &Fill::filled_price)
        .def_readwrite("filled_amount", &Fill::filled_amount)
        .def_readwrite("fee",           &Fill::fee)
        .def_readwrite("fee_currency",  &Fill::fee_currency)
        .def_readwrite("timestamp",     &Fill::timestamp)
        .def_readwrite("pnl_usd",       &Fill::pnl_usd);

    // -----------------------------------------------------------------------
    // KuCoin connector
    // -----------------------------------------------------------------------
    py::class_<KuCoinConnector>(m, "KuCoinConnector")
        .def(py::init<
                const std::string&,   // symbol
                const std::string&,   // api_key
                const std::string&,   // api_secret
                const std::string&,   // passphrase
                const std::string&>(), // quote_currency
             py::arg("symbol"),
             py::arg("api_key"),
             py::arg("api_secret"),
             py::arg("passphrase"),
             py::arg("quote_currency") = "USDT")
        .def("connect",            &KuCoinConnector::connect)
        .def("disconnect",         &KuCoinConnector::disconnect)
        .def("is_connected",       &KuCoinConnector::is_connected)
        .def("fetch_ticker",       &KuCoinConnector::fetch_ticker)
        .def("fetch_candles",      &KuCoinConnector::fetch_candles,
             py::arg("timeframe") = "1m", py::arg("limit") = 15)
        .def("fetch_balance",      &KuCoinConnector::fetch_balance)
        .def("create_limit_order", &KuCoinConnector::create_limit_order,
             py::arg("side"), py::arg("price"), py::arg("amount"))
        .def("cancel_order",       &KuCoinConnector::cancel_order,    py::arg("order_id"))
        .def("cancel_all_orders",  &KuCoinConnector::cancel_all_orders)
        .def("fetch_open_orders",  &KuCoinConnector::fetch_open_orders)
        .def("fetch_fills",        &KuCoinConnector::fetch_fills,
             py::arg("since_ts") = -1.0, py::arg("limit") = 100);

    // -----------------------------------------------------------------------
    // Gate.io connector
    // -----------------------------------------------------------------------
    py::class_<GateConnector>(m, "GateConnector")
        .def(py::init<
                const std::string&,
                const std::string&,
                const std::string&,
                const std::string&>(),
             py::arg("symbol"),
             py::arg("api_key"),
             py::arg("api_secret"),
             py::arg("quote_currency") = "USDT")
        .def("connect",            &GateConnector::connect)
        .def("disconnect",         &GateConnector::disconnect)
        .def("is_connected",       &GateConnector::is_connected)
        .def("fetch_ticker",       &GateConnector::fetch_ticker)
        .def("fetch_candles",      &GateConnector::fetch_candles,
             py::arg("timeframe") = "1m", py::arg("limit") = 15)
        .def("fetch_balance",      &GateConnector::fetch_balance)
        .def("create_limit_order", &GateConnector::create_limit_order,
             py::arg("side"), py::arg("price"), py::arg("amount"))
        .def("cancel_order",       &GateConnector::cancel_order,    py::arg("order_id"))
        .def("cancel_all_orders",  &GateConnector::cancel_all_orders)
        .def("fetch_open_orders",  &GateConnector::fetch_open_orders)
        .def("fetch_fills",        &GateConnector::fetch_fills,
             py::arg("since_ts") = -1.0, py::arg("limit") = 100);

    // -----------------------------------------------------------------------
    // MEXC connector
    // -----------------------------------------------------------------------
    py::class_<MexcConnector>(m, "MexcConnector")
        .def(py::init<
                const std::string&,
                const std::string&,
                const std::string&,
                const std::string&>(),
             py::arg("symbol"),
             py::arg("api_key"),
             py::arg("api_secret"),
             py::arg("quote_currency") = "USDT")
        .def("connect",            &MexcConnector::connect)
        .def("disconnect",         &MexcConnector::disconnect)
        .def("is_connected",       &MexcConnector::is_connected)
        .def("fetch_ticker",       &MexcConnector::fetch_ticker)
        .def("fetch_candles",      &MexcConnector::fetch_candles,
             py::arg("timeframe") = "1m", py::arg("limit") = 15)
        .def("fetch_balance",      &MexcConnector::fetch_balance)
        .def("create_limit_order", &MexcConnector::create_limit_order,
             py::arg("side"), py::arg("price"), py::arg("amount"))
        .def("cancel_order",       &MexcConnector::cancel_order,    py::arg("order_id"))
        .def("cancel_all_orders",  &MexcConnector::cancel_all_orders)
        .def("fetch_open_orders",  &MexcConnector::fetch_open_orders)
        .def("fetch_fills",        &MexcConnector::fetch_fills,
             py::arg("since_ts") = -1.0, py::arg("limit") = 100);

    // -----------------------------------------------------------------------
    // Kraken connector
    // -----------------------------------------------------------------------
    py::class_<KrakenConnector>(m, "KrakenConnector")
        .def(py::init<
                const std::string&,
                const std::string&,
                const std::string&,
                const std::string&>(),
             py::arg("symbol"),
             py::arg("api_key"),
             py::arg("api_secret"),
             py::arg("quote_currency") = "USD")
        .def("connect",            &KrakenConnector::connect)
        .def("disconnect",         &KrakenConnector::disconnect)
        .def("is_connected",       &KrakenConnector::is_connected)
        .def("fetch_ticker",       &KrakenConnector::fetch_ticker)
        .def("fetch_candles",      &KrakenConnector::fetch_candles,
             py::arg("timeframe") = "1m", py::arg("limit") = 15)
        .def("fetch_balance",      &KrakenConnector::fetch_balance)
        .def("create_limit_order", &KrakenConnector::create_limit_order,
             py::arg("side"), py::arg("price"), py::arg("amount"))
        .def("cancel_order",       &KrakenConnector::cancel_order,    py::arg("order_id"))
        .def("cancel_all_orders",  &KrakenConnector::cancel_all_orders)
        .def("fetch_open_orders",  &KrakenConnector::fetch_open_orders)
        .def("fetch_fills",        &KrakenConnector::fetch_fills,
             py::arg("since_ts") = -1.0, py::arg("limit") = 100);

    // Module-level version info
    m.attr("__version__") = "0.1.0-skeleton";
}
