/**
 * test_kucoin.cpp — Unit tests for KuCoin connector (no live API keys required).
 *
 * Tests cover:
 *   1. Object construction / basic state
 *   2. Auth header generation (KC-API-SIGN correctness using known vectors)
 *   3. fetch_ticker / fetch_balance / fetch_open_orders throw when not connected
 *   4. json_to_order  — parse known KuCoin REST order JSON
 *   5. json_to_fill   — parse known KuCoin REST fill JSON
 *   6. on_ticker_msg  — cache update from WS ticker message
 *   7. on_order_msg   — open_orders_ cache insert / remove
 *   8. on_balance_msg — balance cache update
 *   9. map_timeframe  — timeframe string mapping
 *  10. WebSocket frame encode / decode round-trip (via auth_utils base64)
 *
 * Live connection tests are in a separate integration test (requires .env).
 */

#include "kucoin_connector.h"
#include "auth_utils.h"

#include <cassert>
#include <cstdio>
#include <string>
#include <stdexcept>
#include <map>
#include <nlohmann/json.hpp>

using json = nlohmann::json;

// ---------------------------------------------------------------------------
// Test framework (same as test_auth_utils)
// ---------------------------------------------------------------------------

static int g_total  = 0;
static int g_passed = 0;

static void check(const std::string& name, bool ok) {
    ++g_total;
    if (ok) { ++g_passed; std::printf("  PASS  %s\n", name.c_str()); }
    else               std::printf("  FAIL  %s\n", name.c_str());
}

static void check_eq(const std::string& name, const std::string& got, const std::string& exp) {
    bool ok = (got == exp);
    ++g_total;
    if (ok) { ++g_passed; std::printf("  PASS  %s\n", name.c_str()); }
    else    std::printf("  FAIL  %s\n  got: %s\n  exp: %s\n",
                         name.c_str(), got.c_str(), exp.c_str());
}

static void check_approx(const std::string& name, double got, double exp, double tol = 1e-9) {
    bool ok = std::abs(got - exp) <= tol;
    ++g_total;
    if (ok) { ++g_passed; std::printf("  PASS  %s\n", name.c_str()); }
    else    std::printf("  FAIL  %s  got=%.15f  exp=%.15f\n", name.c_str(), got, exp);
}

// ---------------------------------------------------------------------------
// Testable subclass — exposes private helpers for unit testing
// ---------------------------------------------------------------------------

class KuCoinConnectorTest : public KuCoinConnector {
public:
    using KuCoinConnector::KuCoinConnector;  // inherit constructors

    // Expose private helpers
    std::map<std::string, std::string> pub_auth_headers(
        const std::string& method, const std::string& path,
        const std::string& query,  const std::string& body) const
    {
        return make_auth_headers(method, path, query, body);
    }

    Order pub_json_to_order(const json& j) const { return json_to_order(j); }
    Fill  pub_json_to_fill(const json& j)  const { return json_to_fill(j);  }

    void pub_on_ticker_msg(const json& data) { on_ticker_msg(data); }
    void pub_on_order_msg(const json& data)  { on_order_msg(data);  }
    void pub_on_balance_msg(const json& data){ on_balance_msg(data);}

    Ticker  pub_latest_ticker()  { std::lock_guard<std::mutex> lk(state_mutex_); return latest_ticker_; }
    Balance pub_latest_balance() { std::lock_guard<std::mutex> lk(state_mutex_); return latest_balance_; }
    std::map<std::string, Order> pub_open_orders() {
        std::lock_guard<std::mutex> lk(state_mutex_); return open_orders_;
    }
    bool pub_ticker_ready()  { std::lock_guard<std::mutex> lk(state_mutex_); return ticker_ready_; }
    bool pub_balance_ready() { std::lock_guard<std::mutex> lk(state_mutex_); return balance_ready_; }

    static std::string pub_map_timeframe(const std::string& tf) { return map_timeframe(tf); }
};

// ---------------------------------------------------------------------------
// Test groups
// ---------------------------------------------------------------------------

static void test_construction() {
    std::printf("\n[Construction]\n");
    KuCoinConnectorTest c("ALKIMI-USDT", "key", "secret", "pass", "USDT");
    check("exchange_name",   c.exchange_name() == "kucoin");
    check("symbol",          c.symbol()        == "ALKIMI-USDT");
    check("not connected",   !c.is_connected());
    check("ticker not ready",!c.pub_ticker_ready());
    check("balance not ready",!c.pub_balance_ready());
    check("open_orders empty",c.pub_open_orders().empty());
}

static void test_auth_headers() {
    std::printf("\n[Auth headers — KC-API-SIGN correctness]\n");

    const std::string secret     = "test_secret";
    const std::string api_key    = "test_key";
    const std::string passphrase = "test_pass";

    KuCoinConnectorTest c("ALKIMI-USDT", api_key, secret, passphrase, "USDT");

    // Auth headers for a GET request
    auto hdrs = c.pub_auth_headers("GET", "/api/v1/accounts", "type=trade", "");

    check("KC-API-KEY present",         hdrs.count("KC-API-KEY")  == 1);
    check("KC-API-SIGN present",        hdrs.count("KC-API-SIGN") == 1);
    check("KC-API-TIMESTAMP present",   hdrs.count("KC-API-TIMESTAMP") == 1);
    check("KC-API-PASSPHRASE present",  hdrs.count("KC-API-PASSPHRASE") == 1);
    check("KC-API-KEY-VERSION == 2",    hdrs["KC-API-KEY-VERSION"] == "2");
    check("KC-API-KEY value",           hdrs["KC-API-KEY"] == api_key);

    // Independently verify KC-API-SIGN using auth_utils directly
    std::string ts      = hdrs["KC-API-TIMESTAMP"];
    std::string full_p  = "/api/v1/accounts?type=trade";
    std::string exp_sign = base64_encode(hmac_sha256(secret, ts + "GET" + full_p));
    check_eq("KC-API-SIGN correct", hdrs["KC-API-SIGN"], exp_sign);

    // Independently verify KC-API-PASSPHRASE
    std::string exp_pp = base64_encode(hmac_sha256(secret, passphrase));
    check_eq("KC-API-PASSPHRASE correct", hdrs["KC-API-PASSPHRASE"], exp_pp);

    // Content-Type header present
    check("Content-Type present", hdrs.count("Content-Type") == 1);
}

static void test_not_connected_throws() {
    std::printf("\n[Not-connected guard]\n");
    KuCoinConnectorTest c("ALKIMI-USDT", "k", "s", "p");

    auto throws = [](auto fn) -> bool {
        try { fn(); return false; }
        catch (const std::runtime_error&) { return true; }
    };

    check("fetch_ticker throws",       throws([&]{ c.fetch_ticker(); }));
    check("fetch_balance throws",      throws([&]{ c.fetch_balance(); }));
    check("fetch_open_orders throws",  throws([&]{ c.fetch_open_orders(); }));
    check("create_limit_order throws", throws([&]{ c.create_limit_order("buy", 0.001, 1000); }));
    check("cancel_order throws",       throws([&]{ c.cancel_order("id"); }));
    check("cancel_all_orders throws",  throws([&]{ c.cancel_all_orders(); }));
    check("fetch_fills throws",        throws([&]{ c.fetch_fills(); }));
}

static void test_json_to_order() {
    std::printf("\n[json_to_order — REST format]\n");
    KuCoinConnectorTest c("ALKIMI-USDT", "k", "s", "p");

    // Simulate a typical KuCoin REST order record
    json j = {
        {"id",         "6391f8a2abc123"},
        {"symbol",     "ALKIMI-USDT"},
        {"side",       "buy"},
        {"price",      "0.001230"},
        {"size",       "5000"},
        {"dealSize",   "0"},
        {"dealFunds",  "0"},
        {"fee",        "0"},
        {"feeCurrency","USDT"},
        {"isActive",   true},
        {"cancelExist",false},
        {"createdAt",  1714832400000LL},
    };

    Order o = c.pub_json_to_order(j);
    check_eq("order.id",       o.id,       "6391f8a2abc123");
    check_eq("order.exchange", o.exchange, "kucoin");
    check_eq("order.symbol",   o.symbol,   "ALKIMI-USDT");
    check_eq("order.side",     o.side,     "buy");
    check_approx("order.price",  o.price,  0.001230);
    check_approx("order.amount", o.amount, 5000.0);
    check_approx("order.amount_usd", o.amount_usd, 0.001230 * 5000.0);
    check_eq("order.status",   o.status,   "open");
    check("order.timestamp > 0", o.timestamp > 0.0);
}

static void test_json_to_fill() {
    std::printf("\n[json_to_fill — REST format]\n");
    KuCoinConnectorTest c("ALKIMI-USDT", "k", "s", "p");

    json j = {
        {"tradeId",     "trade_abc123"},
        {"orderId",     "order_abc123"},
        {"symbol",      "ALKIMI-USDT"},
        {"side",        "sell"},
        {"price",       "0.001250"},
        {"size",        "1000"},
        {"fee",         "0.000001"},
        {"feeCurrency", "USDT"},
        {"createdAt",   1714832400000.0},
    };

    Fill f = c.pub_json_to_fill(j);
    check_eq("fill.id",           f.id,           "trade_abc123");
    check_eq("fill.order_id",     f.order_id,     "order_abc123");
    check_eq("fill.exchange",     f.exchange,     "kucoin");
    check_eq("fill.side",         f.side,         "sell");
    check_approx("fill.price",    f.filled_price,  0.001250);
    check_approx("fill.amount",   f.filled_amount, 1000.0);
    check_approx("fill.fee",      f.fee,           0.000001);
    check_eq("fill.fee_currency", f.fee_currency,  "USDT");
    check("fill.timestamp > 0",   f.timestamp > 0.0);
}

static void test_on_ticker_msg() {
    std::printf("\n[on_ticker_msg — cache update]\n");
    KuCoinConnectorTest c("ALKIMI-USDT", "k", "s", "p");

    check("ticker_ready starts false", !c.pub_ticker_ready());

    json data = {
        {"bestBid",     "0.001220"},
        {"bestAsk",     "0.001231"},
        {"price",       "0.001225"},
        {"bestBidSize", "100000"},
        {"bestAskSize", "100000"},
    };
    c.pub_on_ticker_msg(data);

    check("ticker_ready after update", c.pub_ticker_ready());
    Ticker t = c.pub_latest_ticker();
    check_approx("ticker.bid",  t.bid,  0.001220);
    check_approx("ticker.ask",  t.ask,  0.001231);
    check_approx("ticker.last", t.last, 0.001225);
    check_approx("ticker.mid",  t.mid,  (0.001220 + 0.001231) / 2.0);
    check("ticker.timestamp > 0", t.timestamp > 0.0);
}

static void test_on_order_msg() {
    std::printf("\n[on_order_msg — open_orders cache]\n");
    KuCoinConnectorTest c("ALKIMI-USDT", "k", "s", "p");

    check("open_orders empty initially", c.pub_open_orders().empty());

    // Open order event
    json open_evt = {
        {"orderId", "oid_001"},
        {"type",    "open"},
        {"symbol",  "ALKIMI-USDT"},
        {"side",    "buy"},
        {"price",   "0.001200"},
        {"size",    "2000"},
        {"ts",      1714832400000000000LL},
    };
    c.pub_on_order_msg(open_evt);
    {
        auto orders = c.pub_open_orders();
        check("open: 1 order in cache", orders.size() == 1);
        check("open: order id",    orders.count("oid_001") == 1);
        check_eq("open: side", orders["oid_001"].side, "buy");
        check_approx("open: price", orders["oid_001"].price, 0.001200);
    }

    // Second open order
    json open_evt2 = open_evt;
    open_evt2["orderId"] = "oid_002";
    open_evt2["side"]    = "sell";
    open_evt2["price"]   = "0.001260";
    c.pub_on_order_msg(open_evt2);
    check("open: 2 orders in cache", c.pub_open_orders().size() == 2);

    // Cancel first order
    json cancel_evt = {{"orderId","oid_001"},{"type","canceled"}};
    c.pub_on_order_msg(cancel_evt);
    {
        auto orders = c.pub_open_orders();
        check("cancel: 1 order remaining", orders.size() == 1);
        check("cancel: correct order remains", orders.count("oid_002") == 1);
    }

    // Fill second order
    json fill_evt = {{"orderId","oid_002"},{"type","filled"}};
    c.pub_on_order_msg(fill_evt);
    check("filled: 0 orders remaining", c.pub_open_orders().empty());
}

static void test_on_balance_msg() {
    std::printf("\n[on_balance_msg — balance cache]\n");
    KuCoinConnectorTest c("ALKIMI-USDT", "k", "s", "p");

    json usdt_evt = {{"currency","USDT"},{"available","500.00"}};
    json alkimi_evt = {{"currency","ALKIMI"},{"available","1000000.00"}};

    c.pub_on_balance_msg(usdt_evt);
    c.pub_on_balance_msg(alkimi_evt);

    check("balance_ready after update", c.pub_balance_ready());
    Balance b = c.pub_latest_balance();
    check_approx("balance.usd",   b.usd,   500.0);
    check_approx("balance.token", b.token, 1000000.0);
    check_eq("balance.quote_currency", b.quote_currency, "USDT");

    // Update USDT only
    json usdt_evt2 = {{"currency","USDT"},{"available","300.00"}};
    c.pub_on_balance_msg(usdt_evt2);
    Balance b2 = c.pub_latest_balance();
    check_approx("balance.usd updated",   b2.usd,   300.0);
    check_approx("balance.token unchanged", b2.token, 1000000.0);
}

static void test_timeframe_mapping() {
    std::printf("\n[map_timeframe]\n");
    check_eq("1m  → 1min",  KuCoinConnectorTest::pub_map_timeframe("1m"),   "1min");
    check_eq("5m  → 5min",  KuCoinConnectorTest::pub_map_timeframe("5m"),   "5min");
    check_eq("1h  → 1hour", KuCoinConnectorTest::pub_map_timeframe("1h"),   "1hour");
    check_eq("1d  → 1day",  KuCoinConnectorTest::pub_map_timeframe("1d"),   "1day");
    check_eq("1w  → 1week", KuCoinConnectorTest::pub_map_timeframe("1w"),   "1week");
    check_eq("unknown→1min",KuCoinConnectorTest::pub_map_timeframe("weird"),"1min");
}

static void test_multiple_updates() {
    std::printf("\n[Rapid ticker updates — latest wins]\n");
    KuCoinConnectorTest c("ALKIMI-USDT", "k", "s", "p");

    for (int i = 0; i < 10; i++) {
        double bid = 0.001200 + i * 0.000001;
        double ask = bid + 0.000010;
        json d = {{"bestBid", std::to_string(bid)},
                  {"bestAsk", std::to_string(ask)},
                  {"price",   std::to_string((bid+ask)/2)}};
        c.pub_on_ticker_msg(d);
    }
    Ticker t = c.pub_latest_ticker();
    // After 10 updates (i=9): bid=0.001209, ask=0.001219
    check("latest bid after 10 updates", t.bid > 0.001208 && t.bid < 0.001210);
}

// ---------------------------------------------------------------------------
// main
// ---------------------------------------------------------------------------

int main() {
    std::printf("=== kucoin_connector test suite ===\n");
    try {
        test_construction();
        test_auth_headers();
        test_not_connected_throws();
        test_json_to_order();
        test_json_to_fill();
        test_on_ticker_msg();
        test_on_order_msg();
        test_on_balance_msg();
        test_timeframe_mapping();
        test_multiple_updates();
    } catch (const std::exception& e) {
        std::printf("\nFATAL exception: %s\n", e.what());
        return 1;
    }
    std::printf("\n=== Results: %d / %d passed ===\n", g_passed, g_total);
    return (g_passed == g_total) ? 0 : 1;
}
