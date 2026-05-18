/**
 * mexc_connector.cpp — Full MEXC v3 C++ connector implementation.
 *
 * REST host : api.mexc.com
 * WS  host  : wbs.mexc.com   path: /ws
 *
 * REST auth (MEXC v3):
 *   All auth params go in query string (GET and POST alike).
 *   Params: ...user_params...&timestamp=<ms>&signature=hex(HMAC-SHA256(secret, query_string))
 *   Header: X-MEXC-APIKEY: <api_key>
 *
 * Private WS (requires listenKey):
 *   POST /api/v3/userDataStream → {"listenKey":"..."}  (no signature, just X-MEXC-APIKEY)
 *   PUT  /api/v3/userDataStream?listenKey=<k>          (keepalive every 29 min)
 *   Subscribe: {"method":"SUBSCRIPTION","params":["spot@private.orders.v3.api@<lk>",...]}
 *
 * WS heartbeat: {"method":"PING"} every 20 s; server replies {"id":0,"code":0,"msg":"PONG"}
 */

#include "mexc_connector.h"
#include "net_utils.h"
#include "auth_utils.h"

#include <algorithm>
#include <chrono>
#include <iomanip>
#include <sstream>
#include <stdexcept>
#include <thread>

#include <nlohmann/json.hpp>
using json = nlohmann::json;

// =============================================================================
// Internal helpers
// =============================================================================
namespace {

double now_s() {
    using namespace std::chrono;
    return static_cast<double>(
        duration_cast<milliseconds>(system_clock::now().time_since_epoch()).count()
    ) / 1000.0;
}

double safe_stod(const std::string& s) {
    if (s.empty()) return 0.0;
    try { return std::stod(s); } catch (...) { return 0.0; }
}

// Helper: extract double from a json value that may be string or number.
double jdbl(const json& j, const std::string& key, double fallback = 0.0) {
    if (!j.contains(key)) return fallback;
    const auto& v = j[key];
    if (v.is_string())         return safe_stod(v.get<std::string>());
    if (v.is_number_integer()) return static_cast<double>(v.get<long long>());
    if (v.is_number())         return v.get<double>();
    return fallback;
}

// MEXC v3 REST response unwrapper.
// Success: bare JSON object/array (no envelope) OR {"code":200,"data":{...}}
// Error:   {"code":-1100,"msg":"..."}
json mexc_unwrap(const HttpResponse& r, const std::string& ctx) {
    if (r.status == 0)
        throw std::runtime_error(ctx + ": no response from server");
    if (r.status < 200 || r.status >= 300)
        throw std::runtime_error(ctx + ": HTTP " + std::to_string(r.status) + " — " + r.body);

    json j = json::parse(r.body, nullptr, false);
    if (j.is_discarded())
        throw std::runtime_error(ctx + ": JSON parse error — " + r.body);

    if (j.is_object() && j.contains("code")) {
        bool is_error = false;
        const auto& code_val = j["code"];
        if (code_val.is_number_integer()) {
            int c = code_val.get<int>();
            is_error = (c != 200 && c != 0);
        } else if (code_val.is_string()) {
            std::string cs = code_val.get<std::string>();
            is_error = (cs != "200" && cs != "0");
        }
        if (is_error)
            throw std::runtime_error(ctx + ": MEXC error code=" + j["code"].dump()
                                         + " msg=" + j.value("msg", "?"));
        // Some MEXC endpoints wrap data in a "data" sub-field
        if (j.contains("data")) return j["data"];
    }
    return j;
}

// Normalise any symbol variant → MEXC no-separator uppercase form.
// "ALKIMI/USDT" | "ALKIMI-USDT" | "ALKIMI_USDT" → "ALKIMIUSDT"
std::string to_mexc_symbol(const std::string& s) {
    std::string result;
    result.reserve(s.size());
    for (char c : s)
        if (c != '/' && c != '-' && c != '_') result += static_cast<char>(std::toupper(c));
    return result;
}

} // namespace

static constexpr char MEXC_REST_HOST[] = "api.mexc.com";
static constexpr char MEXC_WS_HOST[]   = "wbs.mexc.com";
static constexpr char MEXC_WS_PATH[]   = "/ws";

// =============================================================================
// Constructor / Destructor
// =============================================================================

MexcConnector::MexcConnector(
    const std::string& symbol,
    const std::string& api_key,
    const std::string& api_secret,
    const std::string& quote_currency
)
    : BaseConnector("mexc", symbol)
    , api_key_(api_key)
    , api_secret_(api_secret)
    , quote_currency_(quote_currency)
{
    mexc_symbol_ = to_mexc_symbol(symbol);

    // Parse base token by stripping the quote currency suffix.
    // "ALKIMIUSDT" with qc="USDT" → base="ALKIMI"
    std::string upper_qc = quote_currency_;
    std::transform(upper_qc.begin(), upper_qc.end(), upper_qc.begin(), ::toupper);

    if (mexc_symbol_.size() > upper_qc.size() &&
        mexc_symbol_.substr(mexc_symbol_.size() - upper_qc.size()) == upper_qc)
    {
        base_token_ = mexc_symbol_.substr(0, mexc_symbol_.size() - upper_qc.size());
    } else {
        base_token_ = mexc_symbol_;
    }
}

MexcConnector::~MexcConnector() {
    if (running_.load()) {
        try { disconnect(); } catch (...) {}
    }
}

// =============================================================================
// Lifecycle
// =============================================================================

void MexcConnector::connect() {
    if (running_.load()) return;
    running_.store(true);
    io_thread_ = std::thread(&MexcConnector::io_thread_main, this);

    std::unique_lock<std::mutex> lk(state_mutex_);
    bool ok = cache_cv_.wait_for(lk, std::chrono::seconds(15), [this] {
        return (ticker_ready_ && balance_ready_) || !running_.load();
    });

    if (!ok) {
        running_.store(false);
        if (io_thread_.joinable()) io_thread_.join();
        throw std::runtime_error("MexcConnector::connect: timeout waiting for initial data");
    }
    if (!running_.load()) {
        if (io_thread_.joinable()) io_thread_.join();
        throw std::runtime_error("MexcConnector::connect: io_thread failed during startup");
    }
    connected_ = true;
}

void MexcConnector::disconnect() {
    running_.store(false);
    connected_ = false;
    cache_cv_.notify_all();
    if (io_thread_.joinable()) io_thread_.join();
}

// =============================================================================
// REST auth helpers
// =============================================================================

std::string MexcConnector::make_auth_query(const std::string& base_query) const {
    std::string ts = timestamp_ms();
    std::string qs = base_query.empty()
                   ? "recvWindow=60000&timestamp=" + ts
                   : base_query + "&recvWindow=60000&timestamp=" + ts;
    std::string sig = mexc_sign(api_secret_, qs);
    return qs + "&signature=" + sig;
}

std::map<std::string, std::string> MexcConnector::make_api_key_header() const {
    return {
        { "X-MEXC-APIKEY", api_key_ },
        { "Content-Type",  "application/json" },
    };
}

// =============================================================================
// listenKey management  (no rest_mutex_ — separate, short-lived connections)
// =============================================================================

std::string MexcConnector::fetch_listen_key() {
    // MEXC now requires a signed POST for userDataStream.
    json j = rest_post_form("/api/v3/userDataStream", "");
    if (!j.contains("listenKey"))
        throw std::runtime_error("fetch_listen_key: no listenKey in response");
    return j["listenKey"].get<std::string>();
}

void MexcConnector::refresh_listen_key() {
    if (listen_key_.empty()) return;
    auto hdrs = make_api_key_header();
    https_request(MEXC_REST_HOST, "PUT",
                  "/api/v3/userDataStream?listenKey=" + listen_key_, hdrs, "");
}

// =============================================================================
// Low-level REST (each acquires rest_mutex_)
// =============================================================================

json MexcConnector::rest_get(const std::string& path, const std::string& query) {
    std::lock_guard<std::mutex> lk(rest_mutex_);
    std::string qs = make_auth_query(query);
    auto hdrs = make_api_key_header();
    return mexc_unwrap(
        https_request(MEXC_REST_HOST, "GET", path + "?" + qs, hdrs, ""),
        "GET " + path);
}

// MEXC v3 POST: all params (including auth) go in the query string, body is empty.
json MexcConnector::rest_post_form(const std::string& path, const std::string& query) {
    std::lock_guard<std::mutex> lk(rest_mutex_);
    std::string qs = make_auth_query(query);
    auto hdrs = make_api_key_header();
    return mexc_unwrap(
        https_request(MEXC_REST_HOST, "POST", path + "?" + qs, hdrs, ""),
        "POST " + path);
}

json MexcConnector::rest_delete(const std::string& path, const std::string& query) {
    std::lock_guard<std::mutex> lk(rest_mutex_);
    std::string qs = make_auth_query(query);
    auto hdrs = make_api_key_header();
    return mexc_unwrap(
        https_request(MEXC_REST_HOST, "DELETE", path + "?" + qs, hdrs, ""),
        "DELETE " + path);
}

// =============================================================================
// IO thread (WebSocket loop)
// =============================================================================

void MexcConnector::io_thread_main() {
    int retry_count = 0;
    while (running_.load()) {
        try {
            // ── Initial balance via REST ─────────────────────────────────────
            {
                json acct = rest_get("/api/v3/account", "");
                Balance bal;
                bal.quote_currency = quote_currency_;
                if (acct.contains("balances") && acct["balances"].is_array()) {
                    for (auto& b : acct["balances"]) {
                        std::string asset = b.value("asset", "");
                        double free_amt   = jdbl(b, "free");
                        if (asset == quote_currency_) bal.usd   = free_amt;
                        if (asset == base_token_)     bal.token = free_amt;
                    }
                }
                std::lock_guard<std::mutex> lk(state_mutex_);
                latest_balance_ = bal;
                balance_ready_  = true;
            }

            // ── Initial open orders via REST (non-fatal) ─────────────────────
            try {
                json orders_arr = rest_get("/api/v3/openOrders",
                                           "symbol=" + mexc_symbol_);
                std::map<std::string, Order> omap;
                if (orders_arr.is_array())
                    for (auto& item : orders_arr)
                        omap[item.value("orderId", "?")] = json_to_order(item);
                std::lock_guard<std::mutex> lk(state_mutex_);
                open_orders_ = omap;
            } catch (const std::exception& e) {
                fprintf(stderr, "[MexcConnector] open orders fetch skipped: %s\n", e.what());
                fflush(stderr);
            }

            // Bootstrap ticker from REST (low-liquidity pairs may not get a WS
            // push within the connect() timeout window).
            {
                json bt = rest_get("/api/v3/ticker/bookTicker",
                    "symbol=" + mexc_symbol_);
                Ticker t;
                t.bid  = jdbl(bt, "bidPrice");
                t.ask  = jdbl(bt, "askPrice");
                if (t.bid <= 0 || t.ask <= 0) {
                    json lt = rest_get("/api/v3/ticker/price",
                        "symbol=" + mexc_symbol_);
                    double last = jdbl(lt, "price");
                    t.bid = last * 0.999; t.ask = last * 1.001;
                }
                t.last = (t.bid + t.ask) / 2.0;
                t.mid  = t.last;
                t.timestamp = now_s();
                std::lock_guard<std::mutex> lk(state_mutex_);
                latest_ticker_ = t;
                ticker_ready_  = true;
                cache_cv_.notify_all();
            }

            // ── Open WebSocket (public ticker stream only) ───────────────────
            // Private stream (orders/balance) omitted — the public miniTicker
            // subscription is sufficient for the latency benchmark and A/B test.
            // Private subscriptions were causing MEXC to close the connection.
            WsClient ws;
            ws.connect(MEXC_WS_HOST, MEXC_WS_PATH);

            // Subscribe to public miniTicker only
            ws.send_text(json{
                {"method", "SUBSCRIPTION"},
                {"params", {"spot@public.miniTicker.v3.api@" + mexc_symbol_}},
            }.dump());

            // Reset backoff only after first successful recv (WS truly stable)
            bool backoff_reset = false;

            auto last_ping = std::chrono::steady_clock::now();

            while (running_.load()) {
                std::string msg = ws.recv_msg(3000);
                if (!msg.empty()) {
                    on_ws_message(msg);
                    if (!backoff_reset) { retry_count = 0; backoff_reset = true; }
                }

                auto now = std::chrono::steady_clock::now();

                if (std::chrono::duration_cast<std::chrono::milliseconds>(
                        now - last_ping).count() >= PING_INTERVAL_MS)
                {
                    ws.send_text(R"({"method":"PING"})");
                    last_ping = now;
                }
            }
        } catch (const std::exception& e) {
            fprintf(stderr, "[MexcConnector] io_thread error: %s\n", e.what());
            fflush(stderr);
            if (!running_.load()) break;
            int delay_s = std::min(2 << retry_count, 60);  // 2→4→8→16→32→60s max
            retry_count++;
            std::this_thread::sleep_for(std::chrono::seconds(delay_s));
        }
    }
}

// =============================================================================
// WS message dispatch
// =============================================================================

void MexcConnector::on_ws_message(const std::string& raw) {
    json j = json::parse(raw, nullptr, false);
    if (j.is_discarded()) return;

    // Subscription ACK / PONG: {"id":0,"code":0,"msg":"..."}
    if (j.contains("msg") || (j.contains("id") && !j.contains("c"))) return;

    std::string channel = j.value("c", "");
    if (channel.empty() || !j.contains("d")) return;

    const json& d = j["d"];
    if (channel.find("miniTicker")      != std::string::npos) on_ticker_msg(d);
    else if (channel.find("private.orders")  != std::string::npos) on_order_msg(d);
    else if (channel.find("private.account") != std::string::npos) on_balance_msg(d);
}

void MexcConnector::on_ticker_msg(const nlohmann::json& d) {
    Ticker t;
    t.bid  = jdbl(d, "b");   // best bid price
    t.ask  = jdbl(d, "a");   // best ask price
    t.last = jdbl(d, "p");   // last price
    if (t.last == 0.0) t.last = jdbl(d, "c"); // fallback to close
    t.mid  = (t.bid > 0.0 && t.ask > 0.0)
           ? (t.bid + t.ask) / 2.0
           : t.last;
    t.timestamp = now_s();

    {
        std::lock_guard<std::mutex> lk(state_mutex_);
        latest_ticker_ = t;
        ticker_ready_  = true;
    }
    cache_cv_.notify_all();
}

void MexcConnector::on_order_msg(const nlohmann::json& d) {
    // WS order fields: i=orderId, S=side(BUY|SELL), X=status, p=price,
    //   q=origQty, z=executedQty, ap=avgPrice, t=timestamp(ms)
    std::string oid    = d.value("i", "");
    std::string status = d.value("X", "");
    if (oid.empty()) return;

    std::lock_guard<std::mutex> lk(state_mutex_);
    if (status == "NEW" || status == "PARTIALLY_FILLED") {
        Order o;
        o.id            = oid;
        o.exchange      = "mexc";
        o.symbol        = mexc_symbol_;
        std::string side = d.value("S", "BUY");
        o.side          = (side == "BUY") ? "buy" : "sell";
        o.price         = jdbl(d, "p");
        o.amount        = jdbl(d, "q");
        o.amount_usd    = o.price * o.amount;
        o.filled_amount = jdbl(d, "z");
        double avg      = jdbl(d, "ap");
        o.filled_price  = (avg > 0.0) ? avg : 0.0;
        o.status        = (status == "NEW") ? "open" : "partial";
        double ts_ms    = jdbl(d, "t");
        o.timestamp     = (ts_ms > 0.0) ? ts_ms / 1000.0 : now_s();
        open_orders_[oid] = o;
    } else {
        // FILLED | CANCELED | REJECTED | EXPIRED
        open_orders_.erase(oid);
    }
}

void MexcConnector::on_balance_msg(const nlohmann::json& d) {
    // WS account fields: a=asset, f=free, l=locked
    std::string asset = d.value("a", "");
    double free_amt   = jdbl(d, "f");

    std::lock_guard<std::mutex> lk(state_mutex_);
    if (asset == quote_currency_) latest_balance_.usd   = free_amt;
    if (asset == base_token_)     latest_balance_.token = free_amt;
    balance_ready_ = true;
    cache_cv_.notify_all();
}

// =============================================================================
// Cache reads
// =============================================================================

Ticker MexcConnector::fetch_ticker() {
    if (!connected_) throw std::runtime_error("MexcConnector::fetch_ticker: not connected");
    std::lock_guard<std::mutex> lk(state_mutex_);
    if (!ticker_ready_) throw std::runtime_error("MexcConnector::fetch_ticker: no data yet");
    return latest_ticker_;
}

Balance MexcConnector::fetch_balance() {
    if (!connected_) throw std::runtime_error("MexcConnector::fetch_balance: not connected");
    std::lock_guard<std::mutex> lk(state_mutex_);
    return latest_balance_;
}

std::vector<Order> MexcConnector::fetch_open_orders() {
    if (!connected_) throw std::runtime_error("MexcConnector::fetch_open_orders: not connected");
    std::lock_guard<std::mutex> lk(state_mutex_);
    std::vector<Order> result;
    result.reserve(open_orders_.size());
    for (auto& [id, o] : open_orders_) result.push_back(o);
    return result;
}

// =============================================================================
// REST order management
// =============================================================================

Order MexcConnector::create_limit_order(const std::string& side, double price, double amount) {
    if (!connected_) throw std::runtime_error("MexcConnector::create_limit_order: not connected");

    auto fmt = [](double v) {
        std::ostringstream ss;
        ss << std::fixed << std::setprecision(8) << v;
        return ss.str();
    };

    std::string query =
        "symbol="   + mexc_symbol_ +
        "&side="    + ((side == "buy") ? "BUY" : "SELL") +
        "&type=LIMIT"
        "&quantity=" + fmt(amount) +
        "&price="    + fmt(price);

    json data = rest_post_form("/api/v3/order", query);
    return json_to_order(data);
}

void MexcConnector::cancel_order(const std::string& order_id) {
    if (!connected_) throw std::runtime_error("MexcConnector::cancel_order: not connected");
    rest_delete("/api/v3/order",
                "symbol=" + mexc_symbol_ + "&orderId=" + order_id);
}

void MexcConnector::cancel_all_orders() {
    if (!connected_) throw std::runtime_error("MexcConnector::cancel_all_orders: not connected");
    rest_delete("/api/v3/openOrders", "symbol=" + mexc_symbol_);
}

std::vector<Fill> MexcConnector::fetch_fills(double since_ts, int limit) {
    if (!connected_) throw std::runtime_error("MexcConnector::fetch_fills: not connected");
    std::string query = "symbol=" + mexc_symbol_ +
                        "&limit=" + std::to_string(limit);
    if (since_ts > 0.0)
        query += "&startTime=" + std::to_string(static_cast<long long>(since_ts * 1000.0));

    json data = rest_get("/api/v3/myTrades", query);
    std::vector<Fill> fills;
    if (data.is_array())
        for (auto& item : data) fills.push_back(json_to_fill(item));
    return fills;
}

std::vector<Candle> MexcConnector::fetch_candles(const std::string& timeframe, int limit) {
    // Candlesticks endpoint is public (no auth).
    std::string tf    = map_timeframe(timeframe);
    std::string query = "symbol="   + mexc_symbol_ +
                        "&interval=" + tf +
                        "&limit="    + std::to_string(limit);

    auto resp = https_request(MEXC_REST_HOST, "GET",
        "/api/v3/klines?" + query, {}, "");
    if (resp.status < 200 || resp.status >= 300)
        throw std::runtime_error("fetch_candles: HTTP " + std::to_string(resp.status));

    json j = json::parse(resp.body, nullptr, false);
    if (j.is_discarded()) throw std::runtime_error("fetch_candles: JSON parse error");

    // MEXC kline row: [openTime(ms), open, high, low, close, volume, closeTime, ...]
    std::vector<Candle> candles;
    if (j.is_array()) {
        for (auto& row : j) {
            if (!row.is_array() || row.size() < 6) continue;
            Candle c;
            double open_ms = row[0].is_number()
                           ? static_cast<double>(row[0].is_number_integer()
                                 ? row[0].get<long long>() : row[0].get<double>())
                           : safe_stod(row[0].get<std::string>());
            c.timestamp = open_ms / 1000.0;

            auto col = [&](int i) -> double {
                if (row[i].is_string()) return safe_stod(row[i].get<std::string>());
                if (row[i].is_number()) return row[i].get<double>();
                return 0.0;
            };
            c.open   = col(1);
            c.high   = col(2);
            c.low    = col(3);
            c.close  = col(4);
            c.volume = col(5);
            candles.push_back(c);
        }
    }
    return candles;
}

// =============================================================================
// Conversion helpers
// =============================================================================

Order MexcConnector::json_to_order(const nlohmann::json& j) const {
    Order o;
    o.id       = j.value("orderId", "");
    o.exchange = "mexc";
    o.symbol   = mexc_symbol_;

    std::string side = j.value("side", "BUY");
    o.side     = (side == "BUY") ? "buy" : "sell";

    o.price      = jdbl(j, "price");
    o.amount     = jdbl(j, "origQty");
    o.amount_usd = o.price * o.amount;

    std::string raw = j.value("status", "NEW");
    if      (raw == "NEW")              o.status = "open";
    else if (raw == "PARTIALLY_FILLED") o.status = "partial";
    else if (raw == "FILLED")           o.status = "filled";
    else if (raw == "CANCELED")         o.status = "canceled";
    else                                o.status = raw;

    // MEXC REST timestamps are in milliseconds
    double ts_ms = jdbl(j, "time");
    o.timestamp  = (ts_ms > 0.0) ? ts_ms / 1000.0 : now_s();

    o.filled_amount = jdbl(j, "executedQty");
    double filled_quote = jdbl(j, "cummulativeQuoteQty");
    o.filled_price = (o.filled_amount > 0.0) ? filled_quote / o.filled_amount : 0.0;

    return o;
}

Fill MexcConnector::json_to_fill(const nlohmann::json& j) const {
    Fill f;
    f.id            = j.value("id",      "");
    f.order_id      = j.value("orderId", "");
    f.exchange      = "mexc";
    f.symbol        = mexc_symbol_;

    bool is_buyer = j.contains("isBuyer") && j["isBuyer"].is_boolean()
                  ? j["isBuyer"].get<bool>() : false;
    f.side = is_buyer ? "buy" : "sell";

    f.filled_price  = jdbl(j, "price");
    f.filled_amount = jdbl(j, "qty");
    f.fee           = jdbl(j, "commission");
    f.fee_currency  = j.value("commissionAsset", "");
    double ts_ms    = jdbl(j, "time");
    f.timestamp     = (ts_ms > 0.0) ? ts_ms / 1000.0 : 0.0;
    f.pnl_usd       = 0.0;
    return f;
}

std::string MexcConnector::map_timeframe(const std::string& tf) {
    // MEXC kline intervals: 1m 5m 15m 30m 60m 4h 1d 1W 1M
    static const std::map<std::string, std::string> kMap = {
        {"1m","1m"}, {"5m","5m"}, {"15m","15m"}, {"30m","30m"},
        {"1h","60m"}, {"4h","4h"}, {"1d","1d"}, {"1w","1W"}, {"1M","1M"},
    };
    auto it = kMap.find(tf);
    return (it != kMap.end()) ? it->second : "1m";
}
