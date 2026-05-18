/**
 * kraken_connector.cpp — Full Kraken v2 WebSocket C++ connector.
 *
 * REST host      : api.kraken.com
 * Public  WS     : ws.kraken.com         path /v2
 * Private WS     : ws-auth.kraken.com    path /v2
 *
 * REST auth (Kraken):
 *   nonce          = timestamp_ms()
 *   body           = "nonce=" + nonce [ + "&" + extra_params ]
 *   sha256_input   = nonce + body                       (nonce prepended again)
 *   sha256_result  = SHA256(sha256_input)               raw 32 bytes
 *   message        = path + sha256_result               binary concat
 *   decoded_key    = base64_decode(api_secret)
 *   API-Sign       = base64(HMAC-SHA512(decoded_key, message))
 *
 * WS v2 channels (public):
 *   ticker         → {"method":"subscribe","params":{"channel":"ticker","symbol":["ALKIMI/USD"]}}
 *   update msg:    → {"channel":"ticker","type":"update","data":[{"bid":...,"ask":...,"last":...}]}
 *
 * WS v2 channels (private, via ws-auth.kraken.com):
 *   executions     → open/filled/canceled order events (snapshot + updates)
 *   balances       → per-asset balance changes (snapshot + updates)
 *   Both need {"token":"<ws_token>"} in subscribe params
 *
 * Heartbeat: {"method":"ping"} sent to both WS every 30 s.
 */

#include "kraken_connector.h"
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

// Extract double from a JSON value that may be string, integer, or float.
double jdbl(const json& j, const std::string& key, double fallback = 0.0) {
    if (!j.contains(key)) return fallback;
    const auto& v = j[key];
    if (v.is_string())         return safe_stod(v.get<std::string>());
    if (v.is_number_integer()) return static_cast<double>(v.get<long long>());
    if (v.is_number())         return v.get<double>();
    return fallback;
}

// Kraken REST response unwrapper.
// Response: {"error":[],"result":{...}}
// Throws if error array is non-empty or result key is missing.
json kraken_unwrap(const HttpResponse& r, const std::string& ctx) {
    if (r.status == 0)
        throw std::runtime_error(ctx + ": no response from server");
    if (r.status < 200 || r.status >= 300)
        throw std::runtime_error(ctx + ": HTTP " + std::to_string(r.status) + " — " + r.body);

    json j = json::parse(r.body, nullptr, false);
    if (j.is_discarded())
        throw std::runtime_error(ctx + ": JSON parse error — " + r.body);

    if (j.contains("error") && j["error"].is_array() && !j["error"].empty())
        throw std::runtime_error(ctx + ": Kraken error: " + j["error"][0].dump());

    if (!j.contains("result"))
        throw std::runtime_error(ctx + ": missing 'result' field — " + r.body);

    return j["result"];
}

// Derive WS pair ("ALKIMI/USD") and REST pair ("ALKIMIUSD") from user-supplied symbol.
// Handles: "ALKIMI/USD", "ALKIMI-USD", "ALKIMI_USD", "ALKIMIUSD", "alkimi/usd"
std::pair<std::string, std::string> parse_kraken_symbol(
    const std::string& symbol, const std::string& quote_currency)
{
    // Step 1: uppercase and strip separators → rest_pair e.g. "ALKIMIUSD"
    std::string rest_pair;
    for (char c : symbol)
        if (c != '/' && c != '-' && c != '_')
            rest_pair += static_cast<char>(std::toupper(c));

    // Step 2: ensure quote_currency is uppercase
    std::string upper_qc = quote_currency;
    std::transform(upper_qc.begin(), upper_qc.end(), upper_qc.begin(), ::toupper);

    // Step 3: ws_pair = base + "/" + quote_currency
    std::string base;
    if (rest_pair.size() > upper_qc.size() &&
        rest_pair.substr(rest_pair.size() - upper_qc.size()) == upper_qc)
    {
        base = rest_pair.substr(0, rest_pair.size() - upper_qc.size());
    } else {
        base = rest_pair; // can't determine, use whole string as base
    }
    std::string ws_pair = base + "/" + upper_qc;

    return {ws_pair, rest_pair};
}

} // namespace

static constexpr char KRAKEN_REST_HOST[]    = "api.kraken.com";
static constexpr char KRAKEN_PUB_WS_HOST[]  = "ws.kraken.com";
static constexpr char KRAKEN_PRIV_WS_HOST[] = "ws-auth.kraken.com";
static constexpr char KRAKEN_WS_PATH[]      = "/v2";

// =============================================================================
// Constructor / Destructor
// =============================================================================

KrakenConnector::KrakenConnector(
    const std::string& symbol,
    const std::string& api_key,
    const std::string& api_secret,
    const std::string& quote_currency
)
    : BaseConnector("kraken", symbol)
    , api_key_(api_key)
    , api_secret_(api_secret)
    , quote_currency_(quote_currency)
{
    auto [ws_pair, rest_pair] = parse_kraken_symbol(symbol, quote_currency);
    kraken_pair_      = ws_pair;   // "ALKIMI/USD"
    kraken_rest_pair_ = rest_pair; // "ALKIMIUSD"

    // Derive base token from WS pair
    auto slash = kraken_pair_.find('/');
    base_token_ = (slash != std::string::npos)
                ? kraken_pair_.substr(0, slash)
                : kraken_pair_;
}

KrakenConnector::~KrakenConnector() {
    if (running_.load()) {
        try { disconnect(); } catch (...) {}
    }
}

// =============================================================================
// Lifecycle
// =============================================================================

void KrakenConnector::connect() {
    if (running_.load()) return;
    running_.store(true);
    io_thread_ = std::thread(&KrakenConnector::io_thread_main, this);

    std::unique_lock<std::mutex> lk(state_mutex_);
    bool ok = cache_cv_.wait_for(lk, std::chrono::seconds(20), [this] {
        return (ticker_ready_ && balance_ready_) || !running_.load();
    });

    if (!ok) {
        running_.store(false);
        if (io_thread_.joinable()) io_thread_.join();
        throw std::runtime_error("KrakenConnector::connect: timeout waiting for initial data");
    }
    if (!running_.load()) {
        if (io_thread_.joinable()) io_thread_.join();
        throw std::runtime_error("KrakenConnector::connect: io_thread failed during startup");
    }
    connected_ = true;
}

void KrakenConnector::disconnect() {
    running_.store(false);
    connected_ = false;
    cache_cv_.notify_all();
    if (io_thread_.joinable()) io_thread_.join();
}

// =============================================================================
// REST auth helpers
// =============================================================================

std::map<std::string, std::string> KrakenConnector::make_auth_headers(
    const std::string& path,
    const std::string& nonce,
    const std::string& body) const
{
    std::string sign = kraken_sign(api_secret_, path, nonce, body);
    return {
        { "API-Key",      api_key_  },
        { "API-Sign",     sign      },
        { "Content-Type", "application/x-www-form-urlencoded" },
    };
}

json KrakenConnector::rest_post_private(
    const std::string& path,
    const std::string& extra_body)
{
    std::lock_guard<std::mutex> lk(rest_mutex_);
    std::string nonce = timestamp_ms();
    std::string body  = "nonce=" + nonce;
    if (!extra_body.empty()) body += "&" + extra_body;
    auto hdrs = make_auth_headers(path, nonce, body);
    return kraken_unwrap(
        https_request(KRAKEN_REST_HOST, "POST", path, hdrs, body),
        "POST " + path);
}

json KrakenConnector::rest_get_public(const std::string& path_and_query) {
    auto resp = https_request(KRAKEN_REST_HOST, "GET", path_and_query, {}, "");
    return kraken_unwrap(resp, "GET " + path_and_query);
}

// =============================================================================
// WS token
// =============================================================================

std::string KrakenConnector::fetch_ws_token() {
    json result = rest_post_private("/0/private/GetWebSocketsToken");
    return result.value("token", "");
}

// =============================================================================
// IO thread (public + private WS loop)
// =============================================================================

void KrakenConnector::io_thread_main() {
    while (running_.load()) {
        try {
            // ── Initial balance via REST ─────────────────────────────────────
            {
                json bal_result = rest_post_private("/0/private/Balance");
                Balance bal;
                bal.quote_currency = quote_currency_;
                // Kraken balance keys: old-style uses Z prefix for fiat (ZUSD),
                // new-style (WS v2) uses plain names. Handle both.
                for (auto& [asset, amount_val] : bal_result.items()) {
                    double amount = jdbl(bal_result, asset);
                    if (amount == 0.0 && amount_val.is_string())
                        amount = safe_stod(amount_val.get<std::string>());

                    std::string upper_qc = quote_currency_;
                    std::transform(upper_qc.begin(), upper_qc.end(), upper_qc.begin(), ::toupper);

                    if (asset == upper_qc || asset == "Z" + upper_qc)
                        bal.usd = amount;
                    if (asset == base_token_ || asset == "X" + base_token_)
                        bal.token = amount;
                }
                std::lock_guard<std::mutex> lk(state_mutex_);
                latest_balance_ = bal;
                balance_ready_  = true;
            }

            // ── Initial open orders via REST (non-fatal) ─────────────────────
            try {
                json orders_result = rest_post_private("/0/private/OpenOrders");
                std::map<std::string, Order> omap;
                if (orders_result.contains("open") && orders_result["open"].is_object()) {
                    for (auto& [txid, order_data] : orders_result["open"].items())
                        omap[txid] = json_to_order_rest(txid, order_data);
                }
                std::lock_guard<std::mutex> lk(state_mutex_);
                open_orders_ = omap;
            } catch (const std::exception& e) {
                fprintf(stderr, "[KrakenConnector] open orders fetch skipped: %s\n", e.what());
                fflush(stderr);
            }

            // Bootstrap ticker from REST (low-liquidity pairs may not get a WS
            // push within the connect() timeout window).
            {
                json tk_result = rest_get_public("/0/public/Ticker?pair=" + kraken_rest_pair_);
                if (tk_result.contains("result") && !tk_result["result"].empty()) {
                    auto it = tk_result["result"].begin();
                    auto& tk = it.value();
                    Ticker t;
                    // Kraken: "b":[bid,"1","1.000"], "a":[ask,"1","1.000"], "c":[last,vol]
                    if (tk.contains("b") && tk["b"].is_array() && !tk["b"].empty())
                        t.bid = safe_stod(tk["b"][0].get<std::string>());
                    if (tk.contains("a") && tk["a"].is_array() && !tk["a"].empty())
                        t.ask = safe_stod(tk["a"][0].get<std::string>());
                    if (tk.contains("c") && tk["c"].is_array() && !tk["c"].empty())
                        t.last = safe_stod(tk["c"][0].get<std::string>());
                    if (t.bid <= 0 && t.ask <= 0) { t.bid = t.last * 0.999; t.ask = t.last * 1.001; }
                    t.mid = (t.bid + t.ask) / 2.0;
                    t.timestamp = now_s();
                    std::lock_guard<std::mutex> lk(state_mutex_);
                    latest_ticker_ = t;
                    ticker_ready_  = true;
                    cache_cv_.notify_all();
                }
            }

            // ── Fetch WS auth token ──────────────────────────────────────────
            ws_token_ = fetch_ws_token();
            if (ws_token_.empty())
                throw std::runtime_error("KrakenConnector: empty WS token");

            // ── Connect public WS (ticker) ───────────────────────────────────
            WsClient pub_ws;
            pub_ws.connect(KRAKEN_PUB_WS_HOST, KRAKEN_WS_PATH);
            pub_ws.send_text(json{
                {"method", "subscribe"},
                {"params", {
                    {"channel", "ticker"},
                    {"symbol",  json::array({kraken_pair_})},
                }},
            }.dump());

            // ── Connect private WS (executions + balances) ───────────────────
            WsClient priv_ws;
            priv_ws.connect(KRAKEN_PRIV_WS_HOST, KRAKEN_WS_PATH);

            priv_ws.send_text(json{
                {"method", "subscribe"},
                {"params", {
                    {"channel",          "executions"},
                    {"token",            ws_token_},
                    {"snapshot_trades",  false},
                    {"snapshot_orders",  true},
                }},
            }.dump());

            priv_ws.send_text(json{
                {"method",  "subscribe"},
                {"params", {
                    {"channel",  "balances"},
                    {"token",    ws_token_},
                    {"snapshot", true},
                }},
            }.dump());

            auto last_ping = std::chrono::steady_clock::now();

            while (running_.load()) {
                // Poll public WS (1 s timeout)
                std::string pub_msg = pub_ws.recv_msg(1000);
                if (!pub_msg.empty()) on_ws_message(pub_msg);

                // Poll private WS (1 s timeout)
                std::string priv_msg = priv_ws.recv_msg(1000);
                if (!priv_msg.empty()) on_ws_message(priv_msg);

                // Heartbeat to both connections
                auto now = std::chrono::steady_clock::now();
                if (std::chrono::duration_cast<std::chrono::milliseconds>(
                        now - last_ping).count() >= PING_INTERVAL_MS)
                {
                    pub_ws.send_text(R"({"method":"ping"})");
                    priv_ws.send_text(R"({"method":"ping"})");
                    last_ping = now;
                }
            }
        } catch (const std::exception& e) {
            fprintf(stderr, "[KrakenConnector] io_thread error: %s\n", e.what());
            fflush(stderr);
            if (!running_.load()) break;
            std::this_thread::sleep_for(std::chrono::seconds(2));
        }
    }
}

// =============================================================================
// WS message dispatch
// =============================================================================

void KrakenConnector::on_ws_message(const std::string& raw) {
    json j = json::parse(raw, nullptr, false);
    if (j.is_discarded()) return;

    // {"method":"pong",...} or {"method":"subscribe","success":true,...}
    if (j.contains("method")) return;

    std::string channel = j.value("channel", "");
    if (channel.empty() || channel == "heartbeat" || channel == "status") return;

    if (!j.contains("data") || !j["data"].is_array()) return;
    const json& data_arr = j["data"];
    if (data_arr.empty()) return;

    std::string type = j.value("type", "update");

    if (channel == "ticker") {
        for (auto& item : data_arr) on_ticker_msg(item);
    } else if (channel == "executions") {
        if (type == "snapshot") {
            // Replace open_orders_ with the authoritative WS snapshot.
            std::map<std::string, Order> snap;
            for (auto& item : data_arr) {
                std::string oid    = item.value("order_id", "");
                std::string status = item.value("order_status", "");
                if (oid.empty()) continue;
                if (status == "pending_new" || status == "new" ||
                    status == "open"        || status == "partially_filled")
                {
                    snap[oid] = [&]{
                        // Inline: build Order from WS execution item
                        Order o;
                        o.id            = oid;
                        o.exchange      = "kraken";
                        o.symbol        = kraken_pair_;
                        o.side          = item.value("side", "buy");
                        o.price         = jdbl(item, "limit_price");
                        o.amount        = jdbl(item, "order_qty");
                        o.amount_usd    = o.price * o.amount;
                        o.filled_amount = jdbl(item, "cum_qty");
                        o.filled_price  = jdbl(item, "avg_price");
                        o.status        = (status == "partially_filled") ? "partial" : "open";
                        o.timestamp     = now_s();
                        return o;
                    }();
                }
            }
            std::lock_guard<std::mutex> lk(state_mutex_);
            open_orders_ = snap;
        } else {
            for (auto& item : data_arr) on_execution_msg(item);
        }
    } else if (channel == "balances") {
        for (auto& item : data_arr) on_balance_msg(item);
    }
}

void KrakenConnector::on_ticker_msg(const nlohmann::json& data) {
    Ticker t;
    t.bid  = jdbl(data, "bid");
    t.ask  = jdbl(data, "ask");
    t.last = jdbl(data, "last");
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

void KrakenConnector::on_execution_msg(const nlohmann::json& data) {
    // WS v2 executions update — single order event.
    // Fields: order_id, order_status, side, order_qty, limit_price,
    //         cum_qty, avg_price, exec_type, timestamp
    std::string oid    = data.value("order_id", "");
    std::string status = data.value("order_status", "");
    if (oid.empty()) return;

    std::lock_guard<std::mutex> lk(state_mutex_);
    if (status == "pending_new" || status == "new" ||
        status == "open"        || status == "partially_filled")
    {
        Order o;
        o.id            = oid;
        o.exchange      = "kraken";
        o.symbol        = kraken_pair_;
        o.side          = data.value("side", "buy"); // already lowercase in Kraken v2
        o.price         = jdbl(data, "limit_price");
        o.amount        = jdbl(data, "order_qty");
        o.amount_usd    = o.price * o.amount;
        o.filled_amount = jdbl(data, "cum_qty");
        o.filled_price  = jdbl(data, "avg_price");
        o.status        = (status == "partially_filled") ? "partial" : "open";
        o.timestamp     = now_s();
        open_orders_[oid] = o;
    } else {
        // filled | canceled | expired
        open_orders_.erase(oid);
    }
}

void KrakenConnector::on_balance_msg(const nlohmann::json& data) {
    // WS v2 balances: {"asset":"USD","balance":1000.0,"wallets":[{"type":"spot","available":990.0}]}
    std::string asset = data.value("asset", "");

    // Prefer the "available" balance from the spot wallet; fall back to top-level balance.
    double balance = jdbl(data, "balance");
    if (data.contains("wallets") && data["wallets"].is_array()) {
        for (auto& w : data["wallets"]) {
            if (w.value("type", "") == "spot" && w.contains("available")) {
                balance = jdbl(w, "available");
                break;
            }
        }
    }

    std::string upper_qc = quote_currency_;
    std::transform(upper_qc.begin(), upper_qc.end(), upper_qc.begin(), ::toupper);

    std::lock_guard<std::mutex> lk(state_mutex_);
    if (asset == upper_qc || asset == "Z" + upper_qc)
        latest_balance_.usd = balance;
    if (asset == base_token_ || asset == "X" + base_token_)
        latest_balance_.token = balance;
    balance_ready_ = true;
    cache_cv_.notify_all();
}

// =============================================================================
// Cache reads
// =============================================================================

Ticker KrakenConnector::fetch_ticker() {
    if (!connected_) throw std::runtime_error("KrakenConnector::fetch_ticker: not connected");
    std::lock_guard<std::mutex> lk(state_mutex_);
    if (!ticker_ready_) throw std::runtime_error("KrakenConnector::fetch_ticker: no data yet");
    return latest_ticker_;
}

Balance KrakenConnector::fetch_balance() {
    if (!connected_) throw std::runtime_error("KrakenConnector::fetch_balance: not connected");
    std::lock_guard<std::mutex> lk(state_mutex_);
    return latest_balance_;
}

std::vector<Order> KrakenConnector::fetch_open_orders() {
    if (!connected_) throw std::runtime_error("KrakenConnector::fetch_open_orders: not connected");
    std::lock_guard<std::mutex> lk(state_mutex_);
    std::vector<Order> result;
    result.reserve(open_orders_.size());
    for (auto& [id, o] : open_orders_) result.push_back(o);
    return result;
}

// =============================================================================
// REST order management
// =============================================================================

Order KrakenConnector::create_limit_order(const std::string& side, double price, double amount) {
    if (!connected_) throw std::runtime_error("KrakenConnector::create_limit_order: not connected");

    auto fmt = [](double v) {
        std::ostringstream ss;
        ss << std::fixed << std::setprecision(8) << v;
        return ss.str();
    };

    std::string side_str = (side == "buy") ? "buy" : "sell";
    std::string extra    = "ordertype=limit"
                           "&type="   + side_str +
                           "&volume=" + fmt(amount) +
                           "&pair="   + kraken_rest_pair_ +
                           "&price="  + fmt(price);

    json result = rest_post_private("/0/private/AddOrder", extra);

    Order o;
    // Kraken AddOrder response: {"descr":{...},"txid":["order_id_1",...]}
    if (result.contains("txid") && result["txid"].is_array() && !result["txid"].empty())
        o.id = result["txid"][0].get<std::string>();
    o.exchange   = "kraken";
    o.symbol     = kraken_pair_;
    o.side       = side;
    o.price      = price;
    o.amount     = amount;
    o.amount_usd = price * amount;
    o.status     = "open";
    o.timestamp  = now_s();
    return o;
}

void KrakenConnector::cancel_order(const std::string& order_id) {
    if (!connected_) throw std::runtime_error("KrakenConnector::cancel_order: not connected");
    rest_post_private("/0/private/CancelOrder", "txid=" + order_id);
}

void KrakenConnector::cancel_all_orders() {
    if (!connected_) throw std::runtime_error("KrakenConnector::cancel_all_orders: not connected");
    rest_post_private("/0/private/CancelAll");
}

std::vector<Fill> KrakenConnector::fetch_fills(double since_ts, int limit) {
    if (!connected_) throw std::runtime_error("KrakenConnector::fetch_fills: not connected");

    std::string extra = "";
    if (since_ts > 0.0)
        extra += "start=" + std::to_string(static_cast<long long>(since_ts));

    json result = rest_post_private("/0/private/TradesHistory", extra);

    std::vector<Fill> fills;
    if (result.contains("trades") && result["trades"].is_object()) {
        for (auto& [trade_id, trade_data] : result["trades"].items()) {
            fills.push_back(json_to_fill_rest(trade_id, trade_data));
            if (static_cast<int>(fills.size()) >= limit) break;
        }
    }
    return fills;
}

std::vector<Candle> KrakenConnector::fetch_candles(const std::string& timeframe, int limit) {
    // OHLC is a public REST endpoint (no auth).
    std::string interval = map_timeframe(timeframe);
    std::string pq = "/0/public/OHLC?pair=" + kraken_rest_pair_ + "&interval=" + interval;

    auto resp = https_request(KRAKEN_REST_HOST, "GET", pq, {}, "");
    if (resp.status < 200 || resp.status >= 300)
        throw std::runtime_error("fetch_candles: HTTP " + std::to_string(resp.status));

    json j = json::parse(resp.body, nullptr, false);
    if (j.is_discarded()) throw std::runtime_error("fetch_candles: JSON parse error");
    if (j.contains("error") && j["error"].is_array() && !j["error"].empty())
        throw std::runtime_error("fetch_candles: Kraken error: " + j["error"][0].dump());
    if (!j.contains("result")) return {};

    // Kraken OHLC result has one key = pair name (may differ from input, e.g. "XALKIMIUSD"),
    // plus "last". Find the first non-"last" key.
    json candle_arr;
    for (auto& [key, val] : j["result"].items()) {
        if (key != "last" && val.is_array()) { candle_arr = val; break; }
    }
    if (!candle_arr.is_array()) return {};

    // Kraken OHLC row: [time(s), open, high, low, close, vwap, volume, count]
    std::vector<Candle> candles;
    for (auto& row : candle_arr) {
        if (!row.is_array() || row.size() < 7) continue;
        Candle c;
        c.timestamp = row[0].is_number() ? row[0].get<double>()
                                         : safe_stod(row[0].get<std::string>());
        auto col = [&](int i) -> double {
            if (row[i].is_string()) return safe_stod(row[i].get<std::string>());
            if (row[i].is_number()) return row[i].get<double>();
            return 0.0;
        };
        c.open   = col(1);
        c.high   = col(2);
        c.low    = col(3);
        c.close  = col(4);
        // col(5) = vwap
        c.volume = col(6);
        candles.push_back(c);
        if (static_cast<int>(candles.size()) >= limit) break;
    }
    return candles;
}

// =============================================================================
// Conversion helpers
// =============================================================================

Order KrakenConnector::json_to_order_rest(
    const std::string& txid, const nlohmann::json& j) const
{
    Order o;
    o.id       = txid;
    o.exchange = "kraken";
    o.symbol   = kraken_pair_;

    if (j.contains("descr") && j["descr"].is_object()) {
        const auto& descr = j["descr"];
        o.side  = descr.value("type", "buy");  // "buy" | "sell", already lowercase
        o.price = safe_stod(descr.value("price", "0"));
    }

    o.amount     = safe_stod(j.value("vol", "0"));
    o.amount_usd = o.price * o.amount;

    std::string raw = j.value("status", "open");
    if      (raw == "open"     || raw == "pending") o.status = "open";
    else if (raw == "closed")                        o.status = "filled";
    else if (raw == "canceled" || raw == "expired")  o.status = "canceled";
    else                                             o.status = raw;

    // opentm is a float Unix timestamp
    if (j.contains("opentm") && j["opentm"].is_number())
        o.timestamp = j["opentm"].get<double>();
    else
        o.timestamp = now_s();

    o.filled_amount = safe_stod(j.value("vol_exec", "0"));
    // "price" at top level = avg fill price (0.0 for unfilled limit orders)
    double avg_price = safe_stod(j.value("price", "0"));
    o.filled_price   = (o.filled_amount > 0.0) ? avg_price : 0.0;
    o.fee            = safe_stod(j.value("fee", "0"));
    o.fee_currency   = quote_currency_;
    return o;
}

Fill KrakenConnector::json_to_fill_rest(
    const std::string& trade_id, const nlohmann::json& j) const
{
    Fill f;
    f.id            = trade_id;
    f.order_id      = j.value("ordertxid", "");
    f.exchange      = "kraken";
    f.symbol        = kraken_pair_;
    f.side          = j.value("type", "buy");  // "buy" | "sell"
    f.filled_price  = safe_stod(j.value("price", "0"));
    f.filled_amount = safe_stod(j.value("vol",   "0"));
    f.fee           = safe_stod(j.value("fee",   "0"));
    f.fee_currency  = quote_currency_;
    if (j.contains("time") && j["time"].is_number())
        f.timestamp = j["time"].get<double>();
    else
        f.timestamp = 0.0;
    f.pnl_usd = 0.0;
    return f;
}

std::string KrakenConnector::map_timeframe(const std::string& tf) {
    // Kraken OHLC interval is in minutes as an integer string.
    // Supported: 1, 5, 15, 30, 60, 240, 1440, 10080, 21600
    static const std::map<std::string, std::string> kMap = {
        {"1m","1"}, {"5m","5"}, {"15m","15"}, {"30m","30"},
        {"1h","60"}, {"4h","240"}, {"1d","1440"}, {"1w","10080"},
    };
    auto it = kMap.find(tf);
    return (it != kMap.end()) ? it->second : "1";
}
