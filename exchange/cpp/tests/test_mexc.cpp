/**
 * test_mexc.cpp — Unit tests for MexcConnector.
 *
 * All tests are offline (no network calls).
 * Uses a test subclass to expose protected members and methods.
 */

#include "mexc_connector.h"
#include "auth_utils.h"

#include <cassert>
#include <cmath>
#include <functional>
#include <iostream>
#include <string>

// ---------------------------------------------------------------------------
// Test subclass — exposes protected state + methods via pub_* wrappers
// ---------------------------------------------------------------------------
class MexcConnectorTest : public MexcConnector {
public:
    MexcConnectorTest(const std::string& symbol,
                      const std::string& api_key,
                      const std::string& api_secret,
                      const std::string& quote_currency = "USDT")
        : MexcConnector(symbol, api_key, api_secret, quote_currency) {}

    // Field access
    const std::string& pub_mexc_symbol()   const { return mexc_symbol_; }
    const std::string& pub_base_token()    const { return base_token_; }
    const std::string& pub_api_key()       const { return api_key_; }
    bool               pub_ticker_ready()  const { return ticker_ready_; }
    bool               pub_balance_ready() const { return balance_ready_; }
    Ticker             pub_ticker()        const { return latest_ticker_; }
    Balance            pub_balance()       const { return latest_balance_; }
    std::map<std::string, Order> pub_open_orders() const { return open_orders_; }

    // Method wrappers
    std::string pub_make_auth_query(const std::string& base) const {
        return make_auth_query(base);
    }
    std::map<std::string, std::string> pub_make_api_key_header() const {
        return make_api_key_header();
    }
    Order pub_json_to_order(const nlohmann::json& j) const { return json_to_order(j); }
    Fill  pub_json_to_fill(const nlohmann::json& j)  const { return json_to_fill(j); }
    void  pub_on_ticker_msg(const nlohmann::json& d)       { on_ticker_msg(d); }
    void  pub_on_order_msg(const nlohmann::json& d)        { on_order_msg(d); }
    void  pub_on_balance_msg(const nlohmann::json& d)      { on_balance_msg(d); }
    static std::string pub_map_timeframe(const std::string& tf) { return map_timeframe(tf); }
};

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
static int g_pass = 0, g_fail = 0;

static void check(const std::string& name, bool cond) {
    std::cout << (cond ? "  PASS  " : "  FAIL  ") << name << "\n";
    cond ? ++g_pass : ++g_fail;
}

static bool throws(std::function<void()> fn) {
    try { fn(); return false; } catch (...) { return true; }
}

// ---------------------------------------------------------------------------
// Test groups
// ---------------------------------------------------------------------------

void test_construction() {
    std::cout << "\n[Construction + symbol normalisation]\n";

    MexcConnectorTest c1("ALKIMIUSDT", "KEY1", "SECRET1");
    check("exchange_name",                    c1.exchange_name()    == "mexc");
    check("symbol stored",                    c1.symbol()           == "ALKIMIUSDT");
    check("mexc_symbol (no-sep native)",      c1.pub_mexc_symbol()  == "ALKIMIUSDT");
    check("base_token",                       c1.pub_base_token()   == "ALKIMI");
    check("not connected",                    !c1.is_connected());

    MexcConnectorTest c2("ALKIMI/USDT", "K", "S");
    check("mexc_symbol (slash normalised)",   c2.pub_mexc_symbol() == "ALKIMIUSDT");

    MexcConnectorTest c3("ALKIMI-USDT", "K", "S");
    check("mexc_symbol (hyphen normalised)",  c3.pub_mexc_symbol() == "ALKIMIUSDT");

    MexcConnectorTest c4("ALKIMI_USDT", "K", "S");
    check("mexc_symbol (underscore normalised)", c4.pub_mexc_symbol() == "ALKIMIUSDT");

    MexcConnectorTest c5("alkimi/usdt", "K", "S");
    check("mexc_symbol (lowercase normalised)", c5.pub_mexc_symbol() == "ALKIMIUSDT");
}

void test_auth() {
    std::cout << "\n[REST auth — make_auth_query + make_api_key_header]\n";

    MexcConnectorTest c("ALKIMIUSDT", "MY_KEY", "MY_SECRET");

    // make_api_key_header
    auto hdrs = c.pub_make_api_key_header();
    check("X-MEXC-APIKEY present",    hdrs.count("X-MEXC-APIKEY") > 0);
    check("X-MEXC-APIKEY value",      hdrs.at("X-MEXC-APIKEY") == "MY_KEY");
    check("Content-Type present",     hdrs.count("Content-Type") > 0);

    // make_auth_query with base params
    std::string qs = c.pub_make_auth_query("symbol=ALKIMIUSDT");
    check("query has base params",    qs.find("symbol=ALKIMIUSDT") != std::string::npos);
    check("query has timestamp",      qs.find("timestamp=")        != std::string::npos);
    check("query has signature",      qs.find("signature=")        != std::string::npos);

    // Signature must be 64 hex chars
    auto sig_pos = qs.rfind("signature=");
    std::string sig_val = qs.substr(sig_pos + 10);
    check("signature is 64 hex chars", sig_val.size() == 64);

    // Recompute and verify signature
    auto ts_end  = qs.find("&signature=");
    std::string pre_sig = qs.substr(0, ts_end);
    check("signature correct", sig_val == mexc_sign("MY_SECRET", pre_sig));

    // make_auth_query with empty base
    std::string qs2 = c.pub_make_auth_query("");
    check("empty base: starts with timestamp=", qs2.substr(0, 10) == "timestamp=");
    check("empty base: has signature",          qs2.find("signature=") != std::string::npos);
}

void test_not_connected() {
    std::cout << "\n[Not-connected guard]\n";

    MexcConnectorTest c("ALKIMIUSDT", "K", "S");
    check("fetch_ticker throws",       throws([&]{ c.fetch_ticker(); }));
    check("fetch_balance throws",      throws([&]{ c.fetch_balance(); }));
    check("fetch_open_orders throws",  throws([&]{ c.fetch_open_orders(); }));
    check("create_limit_order throws", throws([&]{ c.create_limit_order("buy",1.0,100.0); }));
    check("cancel_order throws",       throws([&]{ c.cancel_order("123"); }));
    check("cancel_all_orders throws",  throws([&]{ c.cancel_all_orders(); }));
    check("fetch_fills throws",        throws([&]{ c.fetch_fills(); }));
}

void test_json_to_order() {
    std::cout << "\n[json_to_order — MEXC REST format]\n";
    using json = nlohmann::json;

    MexcConnectorTest c("ALKIMIUSDT", "K", "S");

    // NEW order (buy)
    json j = {
        {"orderId",             "MID001"},
        {"side",                "BUY"},
        {"price",               "0.00123000"},
        {"origQty",             "1000.00000000"},
        {"executedQty",         "0.00000000"},
        {"cummulativeQuoteQty", "0.00000000"},
        {"status",              "NEW"},
        {"time",                1714832400000LL},
    };

    Order o = c.pub_json_to_order(j);
    check("order.id",            o.id       == "MID001");
    check("order.exchange",      o.exchange == "mexc");
    check("order.symbol",        o.symbol   == "ALKIMIUSDT");
    check("order.side buy",      o.side     == "buy");
    check("order.price",         std::abs(o.price  - 0.00123) < 1e-9);
    check("order.amount",        std::abs(o.amount - 1000.0)  < 1e-6);
    check("order.amount_usd",    std::abs(o.amount_usd - 1.23) < 1e-6);
    check("order.status NEW→open",       o.status == "open");
    check("order.timestamp ms/1000",     std::abs(o.timestamp - 1714832400.0) < 1.0);
    check("order.filled_amount zero",    std::abs(o.filled_amount) < 1e-9);
    check("order.filled_price zero",     std::abs(o.filled_price)  < 1e-9);

    // PARTIALLY_FILLED
    json j2 = j;
    j2["status"]              = "PARTIALLY_FILLED";
    j2["executedQty"]         = "500.00000000";
    j2["cummulativeQuoteQty"] = "0.61500000";
    Order o2 = c.pub_json_to_order(j2);
    check("status PARTIALLY_FILLED→partial", o2.status == "partial");
    check("partial filled_amount",  std::abs(o2.filled_amount - 500.0) < 1e-6);
    check("partial filled_price",   std::abs(o2.filled_price  - 0.00123) < 1e-9);

    // FILLED (sell)
    json j3 = j;
    j3["status"]              = "FILLED";
    j3["side"]                = "SELL";
    j3["executedQty"]         = "1000.00000000";
    j3["cummulativeQuoteQty"] = "1.23000000";
    Order o3 = c.pub_json_to_order(j3);
    check("status FILLED→filled", o3.status == "filled");
    check("sell side",             o3.side   == "sell");
    check("full fill price",       std::abs(o3.filled_price - 0.00123) < 1e-9);

    // CANCELED
    json j4 = j;
    j4["status"] = "CANCELED";
    Order o4 = c.pub_json_to_order(j4);
    check("status CANCELED→canceled", o4.status == "canceled");
}

void test_json_to_fill() {
    std::cout << "\n[json_to_fill — MEXC REST format]\n";
    using json = nlohmann::json;

    MexcConnectorTest c("ALKIMIUSDT", "K", "S");

    json j = {
        {"id",              "FILL001"},
        {"orderId",         "MID001"},
        {"price",           "0.00123000"},
        {"qty",             "500.00000000"},
        {"quoteQty",        "0.61500000"},
        {"commission",      "0.50000000"},
        {"commissionAsset", "ALKIMI"},
        {"time",            1714832400000LL},
        {"isBuyer",         true},
        {"isMaker",         true},
    };

    Fill f = c.pub_json_to_fill(j);
    check("fill.id",            f.id           == "FILL001");
    check("fill.order_id",      f.order_id     == "MID001");
    check("fill.exchange",      f.exchange     == "mexc");
    check("fill.symbol",        f.symbol       == "ALKIMIUSDT");
    check("fill.side buy",      f.side         == "buy");
    check("fill.filled_price",  std::abs(f.filled_price  - 0.00123) < 1e-9);
    check("fill.filled_amount", std::abs(f.filled_amount - 500.0)   < 1e-6);
    check("fill.fee",           std::abs(f.fee           - 0.5)     < 1e-9);
    check("fill.fee_currency",  f.fee_currency == "ALKIMI");
    check("fill.timestamp ms/1000", std::abs(f.timestamp - 1714832400.0) < 1.0);

    // Sell fill (isBuyer = false)
    json j2 = j;
    j2["isBuyer"] = false;
    Fill f2 = c.pub_json_to_fill(j2);
    check("sell fill side",     f2.side == "sell");
}

void test_on_ticker_msg() {
    std::cout << "\n[on_ticker_msg — cache update]\n";
    using json = nlohmann::json;

    MexcConnectorTest c("ALKIMIUSDT", "K", "S");
    check("ticker_ready starts false", !c.pub_ticker_ready());

    // MEXC miniTicker WS data fields: b=bid, a=ask, p=last
    json d = {
        {"s", "ALKIMIUSDT"},
        {"b", "0.00120"},
        {"a", "0.00126"},
        {"p", "0.00123"},
    };
    c.pub_on_ticker_msg(d);

    check("ticker_ready after update",  c.pub_ticker_ready());
    auto t = c.pub_ticker();
    check("ticker.bid",       std::abs(t.bid  - 0.00120) < 1e-9);
    check("ticker.ask",       std::abs(t.ask  - 0.00126) < 1e-9);
    check("ticker.last",      std::abs(t.last - 0.00123) < 1e-9);
    check("ticker.mid",       std::abs(t.mid  - 0.00123) < 1e-9);  // (0.00120+0.00126)/2
    check("ticker.timestamp > 0", t.timestamp > 0.0);
}

void test_on_order_msg() {
    std::cout << "\n[on_order_msg — open_orders cache]\n";
    using json = nlohmann::json;

    MexcConnectorTest c("ALKIMIUSDT", "K", "S");
    check("orders empty initially", c.pub_open_orders().empty());

    // NEW buy order
    json d1 = {
        {"i",  "MID001"}, {"S",  "BUY"}, {"X",  "NEW"},
        {"p",  "0.00123"}, {"q",  "1000.0"}, {"z",  "0.0"},
        {"ap", "0.0"},    {"t",  1714832400000LL},
    };
    c.pub_on_order_msg(d1);
    {
        auto orders = c.pub_open_orders();
        check("1 order after NEW",  orders.size() == 1);
        check("order in cache",     orders.count("MID001") > 0);
        check("side is buy",        orders.count("MID001") && orders.at("MID001").side   == "buy");
        check("status is open",     orders.count("MID001") && orders.at("MID001").status == "open");
    }

    // Second order (NEW sell)
    json d2 = d1;
    d2["i"] = "MID002";
    d2["S"] = "SELL";
    c.pub_on_order_msg(d2);
    check("2 orders in cache", c.pub_open_orders().size() == 2);

    // PARTIALLY_FILLED — stays in cache, values updated
    json d3 = d1;
    d3["X"]  = "PARTIALLY_FILLED";
    d3["z"]  = "500.0";
    d3["ap"] = "0.00123";
    c.pub_on_order_msg(d3);
    {
        auto orders = c.pub_open_orders();
        check("still 2 orders (partial)",  orders.size() == 2);
        check("status updated to partial", orders.count("MID001") && orders.at("MID001").status == "partial");
        check("filled_amount updated",     orders.count("MID001") &&
              std::abs(orders.at("MID001").filled_amount - 500.0) < 1e-6);
    }

    // FILLED — removed from cache
    json d4 = d1;
    d4["X"] = "FILLED";
    c.pub_on_order_msg(d4);
    {
        auto orders = c.pub_open_orders();
        check("1 order after FILLED",   orders.size() == 1);
        check("remaining is MID002",    orders.count("MID002") > 0);
    }

    // CANCELED — also removed
    json d5 = d2;
    d5["X"] = "CANCELED";
    c.pub_on_order_msg(d5);
    check("0 orders after CANCELED", c.pub_open_orders().empty());
}

void test_on_balance_msg() {
    std::cout << "\n[on_balance_msg — balance cache]\n";
    using json = nlohmann::json;

    MexcConnectorTest c("ALKIMIUSDT", "K", "S");

    // USDT balance update (a=asset, f=free, l=locked)
    json d1 = {{"a", "USDT"}, {"f", "1500.00"}, {"l", "0.00"}};
    c.pub_on_balance_msg(d1);
    check("balance_ready after USDT update", c.pub_balance_ready());
    check("balance.usd",                     std::abs(c.pub_balance().usd   - 1500.0) < 1e-6);
    check("balance.token starts zero",        std::abs(c.pub_balance().token - 0.0)    < 1e-9);

    // ALKIMI (base token) balance update
    json d2 = {{"a", "ALKIMI"}, {"f", "50000.00"}, {"l", "1000.00"}};
    c.pub_on_balance_msg(d2);
    check("balance.token updated", std::abs(c.pub_balance().token - 50000.0) < 1e-6);
    check("balance.usd unchanged", std::abs(c.pub_balance().usd   - 1500.0)  < 1e-6);

    // Unrelated currency — should not change usd or token
    json d3 = {{"a", "BTC"}, {"f", "9999.0"}, {"l", "0.0"}};
    c.pub_on_balance_msg(d3);
    check("usd unchanged after BTC update",   std::abs(c.pub_balance().usd   - 1500.0)  < 1e-6);
    check("token unchanged after BTC update", std::abs(c.pub_balance().token - 50000.0) < 1e-6);
}

void test_map_timeframe() {
    std::cout << "\n[map_timeframe — MEXC format]\n";

    check("1m",       MexcConnectorTest::pub_map_timeframe("1m")  == "1m");
    check("5m",       MexcConnectorTest::pub_map_timeframe("5m")  == "5m");
    check("15m",      MexcConnectorTest::pub_map_timeframe("15m") == "15m");
    check("30m",      MexcConnectorTest::pub_map_timeframe("30m") == "30m");
    check("1h→60m",   MexcConnectorTest::pub_map_timeframe("1h")  == "60m");
    check("4h",       MexcConnectorTest::pub_map_timeframe("4h")  == "4h");
    check("1d",       MexcConnectorTest::pub_map_timeframe("1d")  == "1d");
    check("1w→1W",    MexcConnectorTest::pub_map_timeframe("1w")  == "1W");
    check("unknown→1m", MexcConnectorTest::pub_map_timeframe("xyz") == "1m");
}

void test_rapid_ticker_updates() {
    std::cout << "\n[Rapid ticker updates — latest wins]\n";
    using json = nlohmann::json;

    MexcConnectorTest c("ALKIMIUSDT", "K", "S");
    for (int i = 1; i <= 10; ++i) {
        double price = i * 0.001;
        json d = {
            {"b", std::to_string(price)},
            {"a", std::to_string(price + 0.0001)},
            {"p", std::to_string(price)},
        };
        c.pub_on_ticker_msg(d);
    }
    check("latest bid after 10 updates", std::abs(c.pub_ticker().bid - 0.010) < 1e-6);
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

int main() {
    std::cout << "=== mexc_connector test suite ===\n";

    test_construction();
    test_auth();
    test_not_connected();
    test_json_to_order();
    test_json_to_fill();
    test_on_ticker_msg();
    test_on_order_msg();
    test_on_balance_msg();
    test_map_timeframe();
    test_rapid_ticker_updates();

    std::cout << "\n=== Results: " << g_pass << " / " << (g_pass + g_fail) << " passed ===\n";
    return g_fail > 0 ? 1 : 0;
}
