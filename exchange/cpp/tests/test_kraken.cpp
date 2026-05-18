/**
 * test_kraken.cpp — Unit tests for KrakenConnector.
 *
 * All tests are offline (no network calls).
 * Uses a test subclass to expose protected members and methods.
 */

#include "kraken_connector.h"
#include "auth_utils.h"

#include <cmath>
#include <functional>
#include <iostream>
#include <string>

// ---------------------------------------------------------------------------
// Test subclass — exposes protected state + methods via pub_* wrappers
// ---------------------------------------------------------------------------
class KrakenConnectorTest : public KrakenConnector {
public:
    KrakenConnectorTest(const std::string& symbol,
                        const std::string& api_key,
                        const std::string& api_secret,
                        const std::string& quote_currency = "USD")
        : KrakenConnector(symbol, api_key, api_secret, quote_currency) {}

    // Field access
    const std::string& pub_kraken_pair()      const { return kraken_pair_; }
    const std::string& pub_kraken_rest_pair() const { return kraken_rest_pair_; }
    const std::string& pub_base_token()       const { return base_token_; }
    const std::string& pub_api_key()          const { return api_key_; }
    bool               pub_ticker_ready()     const { return ticker_ready_; }
    bool               pub_balance_ready()    const { return balance_ready_; }
    Ticker             pub_ticker()           const { return latest_ticker_; }
    Balance            pub_balance()          const { return latest_balance_; }
    std::map<std::string, Order> pub_open_orders() const { return open_orders_; }

    // Method wrappers
    std::map<std::string, std::string> pub_make_auth_headers(
        const std::string& path,
        const std::string& nonce,
        const std::string& body) const
    { return make_auth_headers(path, nonce, body); }

    Order pub_json_to_order_rest(const std::string& txid,
                                  const nlohmann::json& j) const
    { return json_to_order_rest(txid, j); }

    Fill pub_json_to_fill_rest(const std::string& trade_id,
                                const nlohmann::json& j) const
    { return json_to_fill_rest(trade_id, j); }

    void pub_on_ticker_msg(const nlohmann::json& d)    { on_ticker_msg(d); }
    void pub_on_execution_msg(const nlohmann::json& d) { on_execution_msg(d); }
    void pub_on_balance_msg(const nlohmann::json& d)   { on_balance_msg(d); }

    static std::string pub_map_timeframe(const std::string& tf)
    { return map_timeframe(tf); }
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
// Tests
// ---------------------------------------------------------------------------

void test_construction() {
    std::cout << "\n[Construction + symbol normalisation]\n";

    KrakenConnectorTest c1("ALKIMI/USD", "KEY1", "SECRET1");
    check("exchange_name",          c1.exchange_name()        == "kraken");
    check("symbol stored",          c1.symbol()               == "ALKIMI/USD");
    check("kraken_pair (WS)",       c1.pub_kraken_pair()      == "ALKIMI/USD");
    check("kraken_rest_pair",       c1.pub_kraken_rest_pair() == "ALKIMIUSD");
    check("base_token",             c1.pub_base_token()       == "ALKIMI");
    check("not connected",          !c1.is_connected());

    KrakenConnectorTest c2("ALKIMI-USD", "K", "S");
    check("hyphen normalised pair",     c2.pub_kraken_pair()      == "ALKIMI/USD");
    check("hyphen normalised restpair", c2.pub_kraken_rest_pair() == "ALKIMIUSD");

    KrakenConnectorTest c3("ALKIMIUSD", "K", "S");
    check("no-sep normalised pair",     c3.pub_kraken_pair()      == "ALKIMI/USD");
    check("no-sep normalised restpair", c3.pub_kraken_rest_pair() == "ALKIMIUSD");

    KrakenConnectorTest c4("alkimi/usd", "K", "S");
    check("lowercase normalised pair",  c4.pub_kraken_pair()      == "ALKIMI/USD");
    check("lowercase restpair",         c4.pub_kraken_rest_pair() == "ALKIMIUSD");
}

void test_auth() {
    std::cout << "\n[REST auth — make_auth_headers]\n";

    // Kraken api_secret must be base64-encoded. Use base64("testsecretkey000") as test value.
    // base64("testsecretkey000") = "dGVzdHNlY3JldGtleTAwMA=="
    const std::string secret_b64 = "dGVzdHNlY3JldGtleTAwMA==";
    KrakenConnectorTest c("ALKIMI/USD", "MY_KEY", secret_b64);

    std::string nonce = "1714832400000";
    std::string body  = "nonce=" + nonce;

    auto hdrs = c.pub_make_auth_headers("/0/private/Balance", nonce, body);

    check("API-Key present",          hdrs.count("API-Key")      > 0);
    check("API-Key value",            hdrs.at("API-Key")         == "MY_KEY");
    check("API-Sign present",         hdrs.count("API-Sign")     > 0);
    check("API-Sign non-empty",       !hdrs.at("API-Sign").empty());
    check("Content-Type present",     hdrs.count("Content-Type") > 0);
    check("Content-Type form-enc",    hdrs.at("Content-Type")    == "application/x-www-form-urlencoded");

    // Verify signature equals what kraken_sign() produces directly
    std::string expected_sign = kraken_sign(secret_b64, "/0/private/Balance", nonce, body);
    check("API-Sign matches kraken_sign()", hdrs.at("API-Sign") == expected_sign);

    // Different nonce → different signature (HMAC is deterministic but nonce changes)
    auto hdrs2 = c.pub_make_auth_headers("/0/private/Balance", "9999999999999", "nonce=9999999999999");
    check("different nonce → different sign", hdrs.at("API-Sign") != hdrs2.at("API-Sign"));
}

void test_not_connected() {
    std::cout << "\n[Not-connected guard]\n";

    KrakenConnectorTest c("ALKIMI/USD", "K", "S");
    check("fetch_ticker throws",       throws([&]{ c.fetch_ticker(); }));
    check("fetch_balance throws",      throws([&]{ c.fetch_balance(); }));
    check("fetch_open_orders throws",  throws([&]{ c.fetch_open_orders(); }));
    check("create_limit_order throws", throws([&]{ c.create_limit_order("buy",0.001,1000.0); }));
    check("cancel_order throws",       throws([&]{ c.cancel_order("ID123"); }));
    check("cancel_all_orders throws",  throws([&]{ c.cancel_all_orders(); }));
    check("fetch_fills throws",        throws([&]{ c.fetch_fills(); }));
}

void test_json_to_order_rest() {
    std::cout << "\n[json_to_order_rest — Kraken OpenOrders format]\n";
    using json = nlohmann::json;

    KrakenConnectorTest c("ALKIMI/USD", "K", "S");

    // Open buy limit order
    json j = {
        {"refid",    nullptr},
        {"userref",  0},
        {"status",   "open"},
        {"opentm",   1714832400.123},
        {"starttm",  0},
        {"expiretm", 0},
        {"descr", {
            {"pair",      "ALKIMIUSD"},
            {"type",      "buy"},
            {"ordertype", "limit"},
            {"price",     "0.00100000"},
        }},
        {"vol",      "1000.00000000"},
        {"vol_exec", "0.00000000"},
        {"cost",     "0.00000000"},
        {"fee",      "0.00000000"},
        {"price",    "0.00000000"},
    };

    Order o = c.pub_json_to_order_rest("ABCDE-FGHIJ-11111", j);
    check("order.id",          o.id       == "ABCDE-FGHIJ-11111");
    check("order.exchange",    o.exchange == "kraken");
    check("order.symbol",      o.symbol   == "ALKIMI/USD");
    check("order.side buy",    o.side     == "buy");
    check("order.price",       std::abs(o.price  - 0.001)   < 1e-9);
    check("order.amount",      std::abs(o.amount - 1000.0)  < 1e-6);
    check("order.amount_usd",  std::abs(o.amount_usd - 1.0) < 1e-6);
    check("order.status open", o.status   == "open");
    check("order.timestamp",   std::abs(o.timestamp - 1714832400.123) < 0.001);
    check("order.filled_amount zero", std::abs(o.filled_amount) < 1e-9);
    check("order.filled_price zero",  std::abs(o.filled_price)  < 1e-9);

    // Partially filled sell order
    json j2 = j;
    j2["descr"]["type"] = "sell";
    j2["vol_exec"]      = "500.00000000";
    j2["price"]         = "0.00100000";  // avg fill price
    Order o2 = c.pub_json_to_order_rest("ABCDE-FGHIJ-22222", j2);
    check("sell side",          o2.side          == "sell");
    check("partial filled_amt", std::abs(o2.filled_amount - 500.0) < 1e-6);
    check("partial fill price", std::abs(o2.filled_price - 0.001)  < 1e-9);

    // status: "closed" → "filled"
    json j3 = j;
    j3["status"] = "closed";
    check("closed→filled", c.pub_json_to_order_rest("X", j3).status == "filled");

    // status: "canceled" → "canceled"
    json j4 = j;
    j4["status"] = "canceled";
    check("canceled→canceled", c.pub_json_to_order_rest("X", j4).status == "canceled");

    // status: "expired" → "canceled"
    json j5 = j;
    j5["status"] = "expired";
    check("expired→canceled", c.pub_json_to_order_rest("X", j5).status == "canceled");
}

void test_json_to_fill_rest() {
    std::cout << "\n[json_to_fill_rest — Kraken TradesHistory format]\n";
    using json = nlohmann::json;

    KrakenConnectorTest c("ALKIMI/USD", "K", "S");

    json j = {
        {"ordertxid", "ABCDE-FGHIJ-11111"},
        {"pair",      "ALKIMIUSD"},
        {"time",      1714832400.456},
        {"type",      "buy"},
        {"ordertype", "limit"},
        {"price",     "0.00100000"},
        {"cost",      "1.00000000"},
        {"fee",       "0.00200000"},
        {"vol",       "1000.00000000"},
        {"margin",    "0.00000000"},
        {"misc",      ""},
    };

    Fill f = c.pub_json_to_fill_rest("TRADE-001", j);
    check("fill.id",             f.id           == "TRADE-001");
    check("fill.order_id",       f.order_id     == "ABCDE-FGHIJ-11111");
    check("fill.exchange",       f.exchange     == "kraken");
    check("fill.symbol",         f.symbol       == "ALKIMI/USD");
    check("fill.side buy",       f.side         == "buy");
    check("fill.filled_price",   std::abs(f.filled_price  - 0.001)  < 1e-9);
    check("fill.filled_amount",  std::abs(f.filled_amount - 1000.0) < 1e-6);
    check("fill.fee",            std::abs(f.fee - 0.002) < 1e-9);
    check("fill.fee_currency",   f.fee_currency == "USD");
    check("fill.timestamp",      std::abs(f.timestamp - 1714832400.456) < 0.001);

    // Sell fill
    json j2 = j;
    j2["type"] = "sell";
    check("sell fill side", c.pub_json_to_fill_rest("T2", j2).side == "sell");
}

void test_on_ticker_msg() {
    std::cout << "\n[on_ticker_msg — cache update]\n";
    using json = nlohmann::json;

    KrakenConnectorTest c("ALKIMI/USD", "K", "S");
    check("ticker_ready starts false", !c.pub_ticker_ready());

    // Kraken WS v2 ticker data item fields: bid, ask, last
    json d = {
        {"symbol",   "ALKIMI/USD"},
        {"bid",      0.001},
        {"bid_qty",  10000.0},
        {"ask",      0.0011},
        {"ask_qty",  5000.0},
        {"last",     0.00105},
        {"volume",   100000.0},
    };
    c.pub_on_ticker_msg(d);

    check("ticker_ready after update",  c.pub_ticker_ready());
    auto t = c.pub_ticker();
    check("ticker.bid",       std::abs(t.bid  - 0.001)   < 1e-9);
    check("ticker.ask",       std::abs(t.ask  - 0.0011)  < 1e-9);
    check("ticker.last",      std::abs(t.last - 0.00105) < 1e-9);
    check("ticker.mid",       std::abs(t.mid  - 0.00105) < 1e-9); // (0.001+0.0011)/2
    check("ticker.timestamp > 0", t.timestamp > 0.0);
}

void test_on_execution_msg() {
    std::cout << "\n[on_execution_msg — open_orders cache]\n";
    using json = nlohmann::json;

    KrakenConnectorTest c("ALKIMI/USD", "K", "S");
    check("orders empty initially", c.pub_open_orders().empty());

    // new buy order
    json d1 = {
        {"order_id",     "KID001"},
        {"order_status", "new"},
        {"exec_type",    "new"},
        {"side",         "buy"},
        {"order_qty",    1000.0},
        {"limit_price",  0.001},
        {"cum_qty",      0.0},
        {"avg_price",    0.0},
    };
    c.pub_on_execution_msg(d1);
    {
        auto orders = c.pub_open_orders();
        check("1 order after new",  orders.size() == 1);
        check("order in cache",     orders.count("KID001") > 0);
        check("side is buy",        orders.count("KID001") && orders.at("KID001").side   == "buy");
        check("status is open",     orders.count("KID001") && orders.at("KID001").status == "open");
        check("price correct",      orders.count("KID001") && std::abs(orders.at("KID001").price - 0.001) < 1e-9);
    }

    // Second order (sell)
    json d2 = d1;
    d2["order_id"]     = "KID002";
    d2["side"]         = "sell";
    c.pub_on_execution_msg(d2);
    check("2 orders in cache", c.pub_open_orders().size() == 2);

    // partially_filled — stays in cache, values updated
    json d3 = d1;
    d3["order_status"] = "partially_filled";
    d3["cum_qty"]      = 500.0;
    d3["avg_price"]    = 0.001;
    c.pub_on_execution_msg(d3);
    {
        auto orders = c.pub_open_orders();
        check("still 2 orders (partial)",  orders.size() == 2);
        check("status updated to partial", orders.count("KID001") && orders.at("KID001").status == "partial");
        check("filled_amount updated",     orders.count("KID001") &&
              std::abs(orders.at("KID001").filled_amount - 500.0) < 1e-6);
    }

    // filled — removed from cache
    json d4 = d1;
    d4["order_status"] = "filled";
    c.pub_on_execution_msg(d4);
    {
        auto orders = c.pub_open_orders();
        check("1 order after filled",   orders.size() == 1);
        check("remaining is KID002",    orders.count("KID002") > 0);
    }

    // canceled — removed
    json d5 = d2;
    d5["order_status"] = "canceled";
    c.pub_on_execution_msg(d5);
    check("0 orders after canceled", c.pub_open_orders().empty());

    // expired → also removed
    json d6 = d1;
    d6["order_id"]     = "KID003";
    d6["order_status"] = "new";
    c.pub_on_execution_msg(d6);
    json d7 = d6;
    d7["order_status"] = "expired";
    c.pub_on_execution_msg(d7);
    check("expired order removed", c.pub_open_orders().empty());
}

void test_on_balance_msg() {
    std::cout << "\n[on_balance_msg — balance cache]\n";
    using json = nlohmann::json;

    KrakenConnectorTest c("ALKIMI/USD", "K", "S");

    // USD balance update (WS v2 uses "USD" not "ZUSD")
    json d1 = {
        {"asset",   "USD"},
        {"balance", 1200.0},
        {"wallets", {{{"type","spot"},{"id","main"},{"balance",1200.0},{"available",1100.0}}}},
    };
    c.pub_on_balance_msg(d1);
    check("balance_ready after USD update", c.pub_balance_ready());
    check("balance.usd from available",     std::abs(c.pub_balance().usd - 1100.0) < 1e-6);
    check("balance.token starts zero",       std::abs(c.pub_balance().token) < 1e-9);

    // ALKIMI balance update
    json d2 = {
        {"asset",   "ALKIMI"},
        {"balance", 50000.0},
        {"wallets", {{{"type","spot"},{"id","main"},{"balance",50000.0},{"available",50000.0}}}},
    };
    c.pub_on_balance_msg(d2);
    check("balance.token updated", std::abs(c.pub_balance().token - 50000.0) < 1e-6);
    check("balance.usd unchanged", std::abs(c.pub_balance().usd   - 1100.0)  < 1e-6);

    // Old-style "ZUSD" key (REST balance format)
    json d3 = {{"asset","ZUSD"},{"balance",9999.0},{"wallets",json::array()}};
    c.pub_on_balance_msg(d3);
    check("ZUSD key maps to usd", std::abs(c.pub_balance().usd - 9999.0) < 1e-6);

    // Unrelated asset → no change
    json d4 = {{"asset","BTC"},{"balance",1.0},{"wallets",json::array()}};
    c.pub_on_balance_msg(d4);
    check("usd unchanged after BTC",   std::abs(c.pub_balance().usd   - 9999.0)  < 1e-6);
    check("token unchanged after BTC", std::abs(c.pub_balance().token - 50000.0) < 1e-6);

    // Balance without wallets field → falls back to top-level balance
    json d5 = {{"asset","USD"},{"balance",500.0}};
    c.pub_on_balance_msg(d5);
    check("no wallets → uses top-level balance", std::abs(c.pub_balance().usd - 500.0) < 1e-6);
}

void test_map_timeframe() {
    std::cout << "\n[map_timeframe — Kraken OHLC interval (minutes)]\n";

    check("1m → 1",      KrakenConnectorTest::pub_map_timeframe("1m")  == "1");
    check("5m → 5",      KrakenConnectorTest::pub_map_timeframe("5m")  == "5");
    check("15m → 15",    KrakenConnectorTest::pub_map_timeframe("15m") == "15");
    check("30m → 30",    KrakenConnectorTest::pub_map_timeframe("30m") == "30");
    check("1h → 60",     KrakenConnectorTest::pub_map_timeframe("1h")  == "60");
    check("4h → 240",    KrakenConnectorTest::pub_map_timeframe("4h")  == "240");
    check("1d → 1440",   KrakenConnectorTest::pub_map_timeframe("1d")  == "1440");
    check("1w → 10080",  KrakenConnectorTest::pub_map_timeframe("1w")  == "10080");
    check("unknown → 1", KrakenConnectorTest::pub_map_timeframe("xyz") == "1");
}

void test_rapid_ticker_updates() {
    std::cout << "\n[Rapid ticker updates — latest wins]\n";
    using json = nlohmann::json;

    KrakenConnectorTest c("ALKIMI/USD", "K", "S");
    for (int i = 1; i <= 10; ++i) {
        double price = i * 0.001;
        json d = {{"bid", price}, {"ask", price + 0.0001}, {"last", price}};
        c.pub_on_ticker_msg(d);
    }
    check("latest bid after 10 updates", std::abs(c.pub_ticker().bid - 0.010) < 1e-6);
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

int main() {
    std::cout << "=== kraken_connector test suite ===\n";

    test_construction();
    test_auth();
    test_not_connected();
    test_json_to_order_rest();
    test_json_to_fill_rest();
    test_on_ticker_msg();
    test_on_execution_msg();
    test_on_balance_msg();
    test_map_timeframe();
    test_rapid_ticker_updates();

    std::cout << "\n=== Results: " << g_pass << " / " << (g_pass + g_fail) << " passed ===\n";
    return g_fail > 0 ? 1 : 0;
}
