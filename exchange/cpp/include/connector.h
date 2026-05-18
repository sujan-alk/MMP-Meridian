#pragma once

/**
 * connector.h — C++ abstract connector interface and shared data structures.
 *
 * Mirrors the Python dataclasses and BaseConnector ABC defined in exchange/base.py.
 * Every C++ exchange connector must inherit from BaseConnector and implement all
 * pure virtual methods.
 *
 * Data flow:
 *   C++ connector → bindings.cpp (pybind11) → cpp_connector.py → exchange/base.py types
 */

#include <string>
#include <vector>
#include <stdexcept>

// ---------------------------------------------------------------------------
// Data structures (mirror exchange/base.py dataclasses)
// ---------------------------------------------------------------------------

struct Ticker {
    double bid       = 0.0;
    double ask       = 0.0;
    double mid       = 0.0;
    double last      = 0.0;
    double timestamp = 0.0;  // Unix seconds
};

struct Candle {
    double timestamp = 0.0;  // Unix seconds (candle open time)
    double open      = 0.0;
    double high      = 0.0;
    double low       = 0.0;
    double close     = 0.0;
    double volume    = 0.0;
};

struct Balance {
    double      usd            = 0.0;
    double      token          = 0.0;
    std::string quote_currency = "USDT";
};

struct Order {
    std::string id;
    std::string exchange;
    std::string symbol;
    std::string side;           // "buy" | "sell"
    double      price          = 0.0;
    double      amount         = 0.0;  // token amount
    double      amount_usd     = 0.0;
    std::string status         = "open";  // "open"|"filled"|"canceled"|"partial"
    double      timestamp      = 0.0;
    double      filled_amount  = 0.0;
    double      filled_price   = 0.0;
    double      fee            = 0.0;
    std::string fee_currency;
};

struct Fill {
    std::string id;
    std::string order_id;
    std::string exchange;
    std::string symbol;
    std::string side;
    double      filled_price  = 0.0;
    double      filled_amount = 0.0;
    double      fee           = 0.0;
    std::string fee_currency;
    double      timestamp     = 0.0;
    double      pnl_usd       = 0.0;
};

// ---------------------------------------------------------------------------
// Abstract base connector
// ---------------------------------------------------------------------------

class BaseConnector {
public:
    BaseConnector(std::string exchange_name, std::string symbol)
        : exchange_name_(std::move(exchange_name))
        , symbol_(std::move(symbol))
        , connected_(false)
    {}

    virtual ~BaseConnector() = default;

    // Lifecycle
    virtual void connect()    = 0;
    virtual void disconnect() = 0;

    // Market data
    virtual Ticker              fetch_ticker()                                              = 0;
    virtual std::vector<Candle> fetch_candles(const std::string& timeframe = "1m",
                                              int limit = 15)                              = 0;

    // Account
    virtual Balance fetch_balance() = 0;

    // Order management
    virtual Order               create_limit_order(const std::string& side,
                                                   double price,
                                                   double amount)                          = 0;
    virtual void                cancel_order(const std::string& order_id)                 = 0;
    virtual void                cancel_all_orders()                                        = 0;
    virtual std::vector<Order>  fetch_open_orders()                                        = 0;
    virtual std::vector<Fill>   fetch_fills(double since_ts = -1.0, int limit = 100)      = 0;

    // State
    bool        is_connected()   const { return connected_; }
    std::string exchange_name()  const { return exchange_name_; }
    std::string symbol()         const { return symbol_; }

protected:
    std::string exchange_name_;
    std::string symbol_;
    bool        connected_;
};
