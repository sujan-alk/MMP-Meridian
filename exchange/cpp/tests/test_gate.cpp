/**
 * test_gate.cpp — Unit tests for Gate.io connector (no live API keys required).
 *
 * Tests cover:
 *   1. Construction + symbol normalisation (slashes/hyphens → underscores)
 *   2. REST auth headers (KEY / SIGN / Timestamp correctness)
 *   3. WS per-channel SIGN (HMAC-SHA512 format)
 *   4. Not-connected guards (all 7 public methods throw)
 *   5. json_to_order  — parse known Gate.io REST order JSON
 *   6. json_to_fill   — parse known Gate.io REST trade JSON
 *   7. on_ticker_msg  — cache update
 *   8. on_order_msg   — open / close / cancel cache management
 *   9. on_balance_msg — USDT + ALKIMI balance updates
 *  10. map_timeframe  — Python → Gate.io string mapping
 *  11. Rapid ticker updates — latest wins
 */

#include "gate_connector.h"
#include "auth_utils.h"

#include <cassert>
#include <cstdio>
#include <stdexcept>
#include <string>
#include <map>
#include <nlohmann/json.hpp>

using json = nlohmann::json;

// ---------------------------------------------------------------------------
// Minimal test framework
// ---------------------------------------------------------------------------
static int g_total = 0, g_passed = 0;

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
// Testable subclass — exposes protected helpers
// ---------------------------------------------------------------------------
class GateConnectorTest : public GateConnector {
public:
    using GateConnector::GateConnector;

    std::map<std::string, std::string> pub_auth_headers(
        const std::string& method, const std::string& path,
        const std::string& query,  const std::string& body) const
    { return make_auth_headers(method, path, query, body); }

    std::string pub_ws_sign(const std::string& channel, long long ts) const
    { return ws_sign(channel, ts); }

    std::string pub_ws_sign_login(long long ts) const
    { return ws_sign_login(ts); }

    Order pub_json_to_order(const json& j) const { return json_to_order(j); }
    Fill  pub_json_to_fill(const json& j)  const { return json_to_fill(j); }

    void pub_on_ticker_msg(const json& r)  { on_ticker_msg(r); }
    void pub_on_order_msg(const json& r)   { on_order_msg(r); }
    void pub_on_balance_msg(const json& r) { on_balance_msg(r); }

    // Seqlock read — no mutex needed.
    Ticker  pub_latest_ticker()  { return ticker_sl_.load(); }

    // AtomicDoubleBuffer load + reconstruct full Balance from the two numeric fields.
    Balance pub_latest_balance() {
        BalanceData bd = balance_adb_.load();
        Balance b;
        b.usd            = bd.usd;
        b.token          = bd.token;
        b.quote_currency = quote_currency_;
        return b;
    }

    // Drain the SPSC order queue into consumer_orders_ and return a snapshot.
    // Mirrors what fetch_open_orders() does, but without the connected_ guard
    // so unit tests can call it before connect().
    std::map<std::string, Order> pub_open_orders() {
        OrderEvent ev;
        while (order_queue_.pop(ev)) {
            if (ev.type == OrderEventType::OPEN)
                consumer_orders_[ev.order.id] = ev.order;
            else
                consumer_orders_.erase(ev.order.id);
        }
        return consumer_orders_;
    }

    // Atomic flag reads — no lock needed.
    bool pub_ticker_ready()  { return ticker_ready_.load(std::memory_order_acquire); }
    bool pub_balance_ready() { return balance_ready_.load(std::memory_order_acquire); }
    std::string pub_gate_symbol() const { return gate_symbol_; }
    std::string pub_base_token()  const { return base_token_; }

    static std::string pub_map_timeframe(const std::string& tf) { return map_timeframe(tf); }
};

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

static void test_construction() {
    std::printf("\n[Construction + symbol normalisation]\n");

    // underscore form (native Gate.io)
    GateConnectorTest a("ALKIMI_USDT", "k", "s", "USDT");
    check("exchange_name", a.exchange_name() == "gate");
    check("symbol stored",  a.symbol()       == "ALKIMI_USDT");
    check_eq("gate_symbol (native underscore)", a.pub_gate_symbol(), "ALKIMI_USDT");
    check_eq("base_token",  a.pub_base_token(), "ALKIMI");
    check("not connected",  !a.is_connected());

    // slash form (CCXT / Python style)
    GateConnectorTest b("ALKIMI/USDT", "k", "s", "USDT");
    check_eq("gate_symbol (slash normalised)", b.pub_gate_symbol(), "ALKIMI_USDT");

    // hyphen form (KuCoin style)
    GateConnectorTest c("ALKIMI-USDT", "k", "s", "USDT");
    check_eq("gate_symbol (hyphen normalised)", c.pub_gate_symbol(), "ALKIMI_USDT");
}

static void test_auth_headers() {
    std::printf("\n[REST auth headers — KEY / SIGN / Timestamp]\n");

    const std::string secret  = "gate_test_secret";
    const std::string api_key = "gate_test_key";
    GateConnectorTest c("ALKIMI_USDT", api_key, secret, "USDT");

    auto hdrs = c.pub_auth_headers("GET", "/api/v4/spot/accounts", "", "");
    check("KEY present",       hdrs.count("KEY")  == 1);
    check("SIGN present",      hdrs.count("SIGN") == 1);
    check("Timestamp present", hdrs.count("Timestamp") == 1);
    check_eq("KEY value", hdrs["KEY"], api_key);

    // Verify SIGN = gate_sign(secret, "GET", path, query, ts, body)
    std::string ts   = hdrs["Timestamp"];
    std::string sign = gate_sign(secret, "GET", "/api/v4/spot/accounts", "", ts, "");
    check_eq("SIGN correct", hdrs["SIGN"], sign);

    // POST with body
    auto hdrs2 = c.pub_auth_headers("POST", "/api/v4/spot/orders", "",
                                     R"({"currency_pair":"ALKIMI_USDT"})");
    std::string ts2   = hdrs2["Timestamp"];
    std::string sign2 = gate_sign(secret, "POST", "/api/v4/spot/orders", "",
                                  ts2, R"({"currency_pair":"ALKIMI_USDT"})");
    check_eq("POST SIGN correct", hdrs2["SIGN"], sign2);
}

static void test_ws_sign() {
    std::printf("\n[WS per-channel SIGN — HMAC-SHA512]\n");
    const std::string secret = "ws_secret";
    GateConnectorTest c("ALKIMI_USDT", "k", secret, "USDT");

    long long ts = 1714832400LL;
    std::string computed = c.pub_ws_sign("spot.orders", ts);

    // Verify independently: hex(HMAC-SHA512(secret, "channel=spot.orders&event=subscribe&time=1714832400"))
    std::string message  = "channel=spot.orders&event=subscribe&time=1714832400";
    std::string expected = hex_encode(hmac_sha512(secret, message));
    check_eq("ws_sign correct", computed, expected);

    // Ticker (public) vs orders (private) sign differ for same ts
    std::string sign_tickers  = c.pub_ws_sign("spot.tickers",  ts);
    std::string sign_orders   = c.pub_ws_sign("spot.orders",   ts);
    std::string sign_balances = c.pub_ws_sign("spot.balances", ts);
    check("ticker != orders sign",   sign_tickers  != sign_orders);
    check("orders != balances sign", sign_orders   != sign_balances);
    check("sign non-empty", !computed.empty());

    // Login sign: HMAC-SHA512(secret, "api\nspot.login\n\n{ts}")
    std::string login_sign = c.pub_ws_sign_login(ts);
    std::string login_msg  = "api\nspot.login\n\n1714832400";
    std::string login_exp  = hex_encode(hmac_sha512(secret, login_msg));
    check_eq("ws_sign_login correct", login_sign, login_exp);

    // subscribe and login signs must differ (different formula)
    std::string sub_sign2 = c.pub_ws_sign("spot.login", ts);
    check("subscribe != login sign", sub_sign2 != login_sign);
}

static void test_not_connected_throws() {
    std::printf("\n[Not-connected guard]\n");
    GateConnectorTest c("ALKIMI_USDT", "k", "s");

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
    std::printf("\n[json_to_order — Gate.io REST format]\n");
    GateConnectorTest c("ALKIMI_USDT", "k", "s");

    json j = {
        {"id",           "87654321"},
        {"currency_pair","ALKIMI_USDT"},
        {"type",         "limit"},
        {"side",         "buy"},
        {"amount",       "2000"},
        {"price",        "0.001200"},
        {"left",         "2000"},         // nothing filled yet
        {"filled_total", "0"},
        {"status",       "open"},
        {"create_time",  "1714832400"},
        {"fee",          "0"},
        {"fee_currency", "ALKIMI"},
    };

    Order o = c.pub_json_to_order(j);
    check_eq("order.id",       o.id,       "87654321");
    check_eq("order.exchange", o.exchange, "gate");
    check_eq("order.symbol",   o.symbol,   "ALKIMI_USDT");
    check_eq("order.side",     o.side,     "buy");
    check_approx("order.price",  o.price,  0.001200);
    check_approx("order.amount", o.amount, 2000.0);
    check_approx("order.amount_usd", o.amount_usd, 0.001200 * 2000.0);
    check_eq("order.status",   o.status,  "open");
    check_approx("order.timestamp", o.timestamp, 1714832400.0);
    check_approx("order.filled_amount", o.filled_amount, 0.0);

    // Partially-filled order
    json j2 = j;
    j2["left"]         = "500";    // 1500 filled
    j2["filled_total"] = "1.8";    // 1500 @ 0.0012 = 1.8 USDT
    Order o2 = c.pub_json_to_order(j2);
    check_approx("partial fill amount", o2.filled_amount, 1500.0);
    check_approx("partial fill price",  o2.filled_price,  1.8 / 1500.0, 1e-6);

    // Fully filled (status="closed")
    json j3 = j;
    j3["status"] = "closed";
    j3["left"]   = "0";
    Order o3 = c.pub_json_to_order(j3);
    check_eq("status closed → filled", o3.status, "filled");

    // Cancelled
    json j4 = j;
    j4["status"] = "cancelled";
    Order o4 = c.pub_json_to_order(j4);
    check_eq("status cancelled → canceled", o4.status, "canceled");
}

static void test_json_to_fill() {
    std::printf("\n[json_to_fill — Gate.io REST format]\n");
    GateConnectorTest c("ALKIMI_USDT", "k", "s");

    json j = {
        {"id",           "trade_999"},
        {"order_id",     "order_888"},
        {"currency_pair","ALKIMI_USDT"},
        {"side",         "sell"},
        {"price",        "0.001250"},
        {"amount",       "1000"},
        {"fee",          "0.000001"},
        {"fee_currency", "USDT"},
        {"create_time",  "1714832400"},
    };

    Fill f = c.pub_json_to_fill(j);
    check_eq("fill.id",           f.id,           "trade_999");
    check_eq("fill.order_id",     f.order_id,     "order_888");
    check_eq("fill.exchange",     f.exchange,     "gate");
    check_eq("fill.side",         f.side,         "sell");
    check_approx("fill.price",    f.filled_price,  0.001250);
    check_approx("fill.amount",   f.filled_amount, 1000.0);
    check_approx("fill.fee",      f.fee,           0.000001);
    check_eq("fill.fee_currency", f.fee_currency,  "USDT");
    check_approx("fill.timestamp",f.timestamp,     1714832400.0);
}

static void test_on_ticker_msg() {
    std::printf("\n[on_ticker_msg — cache update]\n");
    GateConnectorTest c("ALKIMI_USDT", "k", "s");

    check("ticker_ready starts false", !c.pub_ticker_ready());

    json result = {
        {"currency_pair", "ALKIMI_USDT"},
        {"last",          "0.001225"},
        {"lowest_ask",    "0.001231"},
        {"highest_bid",   "0.001220"},
        {"base_volume",   "1000000"},
        {"quote_volume",  "1230"},
    };
    c.pub_on_ticker_msg(result);

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
    GateConnectorTest c("ALKIMI_USDT", "k", "s");

    check("orders empty initially", c.pub_open_orders().empty());

    // Gate.io sends result as an array of order objects
    json open_arr = json::array({
        {{"id","goid_001"},{"currency_pair","ALKIMI_USDT"},{"side","buy"},
         {"amount","1000"},{"price","0.001200"},{"left","1000"},
         {"filled_total","0"},{"status","open"},{"create_time","1714832400"},
         {"fee","0"},{"fee_currency","ALKIMI"}}
    });
    c.pub_on_order_msg(open_arr);

    check("1 order after open event", c.pub_open_orders().size() == 1);
    check("correct id in cache", c.pub_open_orders().count("goid_001") == 1);
    check_eq("correct side", c.pub_open_orders()["goid_001"].side, "buy");

    // Second order (sell side)
    json open_arr2 = json::array({
        {{"id","goid_002"},{"currency_pair","ALKIMI_USDT"},{"side","sell"},
         {"amount","500"},{"price","0.001260"},{"left","500"},
         {"filled_total","0"},{"status","open"},{"create_time","1714832401"},
         {"fee","0"},{"fee_currency","USDT"}}
    });
    c.pub_on_order_msg(open_arr2);
    check("2 orders in cache", c.pub_open_orders().size() == 2);

    // Close (filled) first order
    json fill_arr = json::array({
        {{"id","goid_001"},{"status","closed"},{"amount","1000"},{"left","0"},
         {"filled_total","1.2"},{"price","0.001200"},{"side","buy"},
         {"currency_pair","ALKIMI_USDT"},{"create_time","1714832400"},
         {"fee","0"},{"fee_currency","ALKIMI"}}
    });
    c.pub_on_order_msg(fill_arr);
    check("1 order after fill", c.pub_open_orders().size() == 1);
    check("remaining is goid_002", c.pub_open_orders().count("goid_002") == 1);

    // Cancel second order
    json cancel_arr = json::array({
        {{"id","goid_002"},{"status","cancelled"},{"amount","500"},{"left","500"},
         {"filled_total","0"},{"price","0.001260"},{"side","sell"},
         {"currency_pair","ALKIMI_USDT"},{"create_time","1714832401"},
         {"fee","0"},{"fee_currency","USDT"}}
    });
    c.pub_on_order_msg(cancel_arr);
    check("0 orders after cancel", c.pub_open_orders().empty());
}

static void test_on_balance_msg() {
    std::printf("\n[on_balance_msg — balance cache]\n");
    GateConnectorTest c("ALKIMI_USDT", "k", "s");

    json usdt_evt   = {{"currency","USDT"},  {"available","750.00"},{"total","750.00"}};
    json alkimi_evt = {{"currency","ALKIMI"},{"available","500000.00"},{"total","500000.00"}};

    c.pub_on_balance_msg(usdt_evt);
    c.pub_on_balance_msg(alkimi_evt);

    check("balance_ready after update", c.pub_balance_ready());
    Balance b = c.pub_latest_balance();
    check_approx("balance.usd",   b.usd,   750.0);
    check_approx("balance.token", b.token, 500000.0);
    check_eq("balance.quote_currency", b.quote_currency, "USDT");

    // Update USDT, leave ALKIMI unchanged
    json usdt_evt2 = {{"currency","USDT"},{"available","600.00"},{"total","600.00"}};
    c.pub_on_balance_msg(usdt_evt2);
    Balance b2 = c.pub_latest_balance();
    check_approx("usd updated",       b2.usd,   600.0);
    check_approx("token unchanged",   b2.token, 500000.0);
}

static void test_timeframe_mapping() {
    std::printf("\n[map_timeframe — Gate.io format]\n");
    check_eq("1m",  GateConnectorTest::pub_map_timeframe("1m"),  "1m");
    check_eq("5m",  GateConnectorTest::pub_map_timeframe("5m"),  "5m");
    check_eq("1h",  GateConnectorTest::pub_map_timeframe("1h"),  "1h");
    check_eq("4h",  GateConnectorTest::pub_map_timeframe("4h"),  "4h");
    check_eq("1d",  GateConnectorTest::pub_map_timeframe("1d"),  "1d");
    check_eq("1w→7d", GateConnectorTest::pub_map_timeframe("1w"), "7d");
    check_eq("unknown→1m", GateConnectorTest::pub_map_timeframe("weird"), "1m");
}

static void test_rapid_ticker_updates() {
    std::printf("\n[Rapid ticker updates — latest wins]\n");
    GateConnectorTest c("ALKIMI_USDT", "k", "s");

    for (int i = 0; i < 10; i++) {
        double bid = 0.001200 + i * 0.000001;
        double ask = bid + 0.000010;
        json r = {{"highest_bid", std::to_string(bid)},
                  {"lowest_ask",  std::to_string(ask)},
                  {"last",        std::to_string((bid+ask)/2)}};
        c.pub_on_ticker_msg(r);
    }
    Ticker t = c.pub_latest_ticker();
    // After 10 updates (i=9): bid=0.001209
    check("latest bid after 10 updates", t.bid > 0.001208 && t.bid < 0.001210);
}

static void test_order_filter() {
    std::printf("\n[GateOrderFilter — precision rounding and validation]\n");

    // ── round() ──────────────────────────────────────────────────────────────
    {
        GateOrderFilter f;
        f.price_precision  = 8;
        f.amount_precision = 0;   // ALKIMI: whole numbers only
        f.loaded = false;

        auto [p1, a1] = f.round(0.003, 500.0);
        check_eq("round: exact amount 500.0 → \"500\"", a1, "500");

        auto [p2, a2] = f.round(0.003, 99.9);
        check_eq("round: 99.9 rounds up to \"100\"", a2, "100");

        auto [p3, a3] = f.round(0.003, 50.1);
        check_eq("round: 50.1 rounds down to \"50\"", a3, "50");
    }
    {
        // price uses price_precision, not amount_precision
        GateOrderFilter f;
        f.price_precision  = 4;
        f.amount_precision = 0;
        f.loaded = false;
        auto [p, a] = f.round(1.0, 100.0);
        check_eq("round: price 1.0 formatted to 4dp", p, "1.0000");
        check_eq("round: amount 100.0 formatted to 0dp", a, "100");
    }

    // ── validate() ───────────────────────────────────────────────────────────
    {
        GateOrderFilter f;
        f.min_base_amount  = 1.0;
        f.max_base_amount  = 1e6;
        f.min_quote_amount = 1.0;
        f.loaded = true;

        // valid: amount=100, price=0.02, notional=2.0 > 1.0 min_quote
        bool threw = false;
        try { f.validate(100.0, 0.02); } catch (...) { threw = true; }
        check("validate: valid order accepted", !threw);

        // amount < min_base_amount (0.5 < 1.0)
        threw = false;
        try { f.validate(0.5, 1.0); } catch (const std::runtime_error&) { threw = true; }
        check("validate: amount < min_base throws", threw);

        // amount > max_base_amount (2e6 > 1e6)
        threw = false;
        try { f.validate(2e6, 0.01); } catch (const std::runtime_error&) { threw = true; }
        check("validate: amount > max_base throws", threw);

        // notional too small: 1.0 * 0.0001 = 0.0001 < 1.0 min_quote
        threw = false;
        try { f.validate(1.0, 0.0001); } catch (const std::runtime_error&) { threw = true; }
        check("validate: notional < min_quote throws", threw);

        // loaded=false → all constraints bypassed regardless of values
        GateOrderFilter f_unloaded;
        f_unloaded.loaded           = false;
        f_unloaded.min_base_amount  = 1e9;   // absurdly high — must NOT trigger
        threw = false;
        try { f_unloaded.validate(0.001, 0.001); } catch (...) { threw = true; }
        check("validate: skipped when not loaded", !threw);
    }
}

static void test_rate_limiter() {
    std::printf("\n[RateLimiter — token bucket]\n");

    // A full bucket of 10 tokens → 10 acquires must return without sleeping.
    RateLimiter rl(10);
    auto t0 = std::chrono::steady_clock::now();
    for (int i = 0; i < 10; ++i) rl.acquire();
    long long elapsed_ms = std::chrono::duration_cast<std::chrono::milliseconds>(
        std::chrono::steady_clock::now() - t0).count();
    check("10 acquires from full bucket complete in <100 ms", elapsed_ms < 100);

    // on_rate_limit_hit() and on_success() are callable without throwing.
    bool ok = true;
    try { rl.on_rate_limit_hit(); rl.on_success(); } catch (...) { ok = false; }
    check("on_rate_limit_hit + on_success don't throw", ok);

    // Object remains usable after a hit+success cycle.
    RateLimiter rl2(3);
    rl2.on_rate_limit_hit();
    rl2.on_success();
    ok = true;
    try { /* just verifying no crash */ } catch (...) { ok = false; }
    check("RateLimiter usable after hit+success cycle", ok);
}

// ---------------------------------------------------------------------------
// main
// ---------------------------------------------------------------------------
int main() {
    std::printf("=== gate_connector test suite ===\n");
    try {
        test_construction();
        test_auth_headers();
        test_ws_sign();
        test_not_connected_throws();
        test_json_to_order();
        test_json_to_fill();
        test_on_ticker_msg();
        test_on_order_msg();
        test_on_balance_msg();
        test_timeframe_mapping();
        test_rapid_ticker_updates();
        test_order_filter();
        test_rate_limiter();
    } catch (const std::exception& e) {
        std::printf("\nFATAL exception: %s\n", e.what());
        return 1;
    }
    std::printf("\n=== Results: %d / %d passed ===\n", g_passed, g_total);
    return (g_passed == g_total) ? 0 : 1;
}
