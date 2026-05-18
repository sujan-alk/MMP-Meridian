/**
 * kucoin_connector.cpp — Full KuCoin C++ connector implementation.
 *
 * Networking: TlsConn / WsClient / https_request from net_utils.h.
 * Auth signing: kucoin_sign / kucoin_sign_passphrase from auth_utils.h.
 *
 * Threading:
 *   io_thread_  — background thread that owns the WS connection + cache updates
 *   rest_mutex_ — serialises all REST calls (from caller threads)
 *   state_mutex_— guards cached ticker, balance, open_orders
 *   cache_cv_   — signalled when initial ticker + balance are ready
 */

#include "kucoin_connector.h"
#include "net_utils.h"
#include "auth_utils.h"

#include <openssl/rand.h>

#include <algorithm>
#include <chrono>
#include <cstring>
#include <iomanip>
#include <sstream>
#include <stdexcept>
#include <thread>
#include <vector>

#include <nlohmann/json.hpp>
using json = nlohmann::json;

// =============================================================================
// Internal helpers (private to this translation unit)
// =============================================================================
namespace {

// Normalise any symbol variant to KuCoin's dash format: "ALKIMI/USDT" → "ALKIMI-USDT"
std::string to_kucoin_symbol(const std::string& s) {
    std::string r;
    r.reserve(s.size());
    for (char c : s) {
        r += (c == '/' || c == '_') ? '-' : c;
    }
    return r;
}

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

// Parse KuCoin envelope {"code":"200000","data":{...}} and return the data.
json kc_unwrap(const HttpResponse& r, const std::string& ctx) {
    if (r.status == 0)
        throw std::runtime_error(ctx + ": no response from server");
    if (r.status < 200 || r.status >= 300)
        throw std::runtime_error(ctx + ": HTTP " + std::to_string(r.status) + " — " + r.body);
    json j = json::parse(r.body, nullptr, false);
    if (j.is_discarded())
        throw std::runtime_error(ctx + ": JSON parse error — " + r.body);
    std::string code = j.value("code", "");
    if (code != "200000")
        throw std::runtime_error(ctx + ": KuCoin error code=" + code + " msg=" + j.dump());
    return j["data"];
}

} // namespace

static constexpr char KC_REST_HOST[] = "api.kucoin.com";

// =============================================================================
// Constructor / Destructor
// =============================================================================

KuCoinConnector::KuCoinConnector(
    const std::string& symbol,
    const std::string& api_key,
    const std::string& api_secret,
    const std::string& passphrase,
    const std::string& quote_currency
)
    : BaseConnector("kucoin", to_kucoin_symbol(symbol))
    , api_key_(api_key)
    , api_secret_(api_secret)
    , passphrase_(passphrase)
    , quote_currency_(quote_currency)
{
    auto dash = symbol_.find('-');
    base_token_ = (dash != std::string::npos) ? symbol_.substr(0, dash) : symbol_;
}

KuCoinConnector::~KuCoinConnector() {
    if (running_.load()) {
        try { disconnect(); } catch (...) {}
    }
}

// =============================================================================
// Lifecycle
// =============================================================================

void KuCoinConnector::connect() {
    if (running_.load()) return;
    running_.store(true);
    io_thread_ = std::thread(&KuCoinConnector::io_thread_main, this);

    std::unique_lock<std::mutex> lk(state_mutex_);
    bool ok = cache_cv_.wait_for(lk, std::chrono::seconds(15), [this] {
        return (ticker_ready_ && balance_ready_) || !running_.load();
    });

    if (!ok) {
        running_.store(false);
        if (io_thread_.joinable()) io_thread_.join();
        throw std::runtime_error("KuCoinConnector::connect: timeout waiting for initial data");
    }
    if (!running_.load()) {
        if (io_thread_.joinable()) io_thread_.join();
        throw std::runtime_error("KuCoinConnector::connect: io_thread failed during startup");
    }
    connected_ = true;
}

void KuCoinConnector::disconnect() {
    running_.store(false);
    connected_ = false;
    cache_cv_.notify_all();
    if (io_thread_.joinable()) io_thread_.join();
}

// =============================================================================
// REST auth
// =============================================================================

std::map<std::string, std::string> KuCoinConnector::make_auth_headers(
    const std::string& method,
    const std::string& path,
    const std::string& query,
    const std::string& body) const
{
    std::string ts        = timestamp_ms();
    std::string full_path = query.empty() ? path : path + "?" + query;
    std::string sign      = kucoin_sign(api_secret_, ts, method, full_path, body);
    std::string pp_signed = kucoin_sign_passphrase(api_secret_, passphrase_);
    return {
        { "KC-API-KEY",         api_key_    },
        { "KC-API-SIGN",        sign        },
        { "KC-API-TIMESTAMP",   ts          },
        { "KC-API-PASSPHRASE",  pp_signed   },
        { "KC-API-KEY-VERSION", "2"         },
        { "Content-Type",       "application/json" },
    };
}

// =============================================================================
// REST method helpers
// =============================================================================

nlohmann::json KuCoinConnector::rest_get(const std::string& path, const std::string& query) {
    std::lock_guard<std::mutex> lk(rest_mutex_);
    auto hdrs = make_auth_headers("GET", path, query, "");
    std::string pq = query.empty() ? path : path + "?" + query;
    return kc_unwrap(https_request(KC_REST_HOST, "GET", pq, hdrs, ""), "GET " + path);
}

nlohmann::json KuCoinConnector::rest_post(const std::string& path, const nlohmann::json& body) {
    std::lock_guard<std::mutex> lk(rest_mutex_);
    std::string body_str = body.dump();
    auto hdrs = make_auth_headers("POST", path, "", body_str);
    return kc_unwrap(https_request(KC_REST_HOST, "POST", path, hdrs, body_str), "POST " + path);
}

nlohmann::json KuCoinConnector::rest_delete(const std::string& path, const std::string& query) {
    std::lock_guard<std::mutex> lk(rest_mutex_);
    auto hdrs = make_auth_headers("DELETE", path, query, "");
    std::string pq = query.empty() ? path : path + "?" + query;
    return kc_unwrap(https_request(KC_REST_HOST, "DELETE", pq, hdrs, ""), "DELETE " + path);
}

// =============================================================================
// WS credentials (POST /api/v1/bullet-private)
// =============================================================================

void KuCoinConnector::fetch_ws_credentials() {
    std::lock_guard<std::mutex> lk(rest_mutex_);
    auto hdrs = make_auth_headers("POST", "/api/v1/bullet-private", "", "");
    json data = kc_unwrap(
        https_request(KC_REST_HOST, "POST", "/api/v1/bullet-private", hdrs, ""),
        "bullet-private");

    ws_token_ = data.value("token", "");
    if (data.contains("instanceServers") && !data["instanceServers"].empty()) {
        auto& srv = data["instanceServers"][0];
        std::string ep = srv.value("endpoint", "wss://ws-api-spot.kucoin.com");
        auto wss = ep.find("://");
        ws_host_ = (wss != std::string::npos) ? ep.substr(wss + 3) : ep;
        // Strip trailing slash — getaddrinfo rejects "host.com/" as invalid
        while (!ws_host_.empty() && ws_host_.back() == '/') ws_host_.pop_back();
        ws_ping_interval_ms_ = srv.value("pingInterval", 18000);
    } else {
        ws_host_ = "ws-api-spot.kucoin.com";
    }

    unsigned char rand_bytes[8];
    RAND_bytes(rand_bytes, 8);
    std::ostringstream cid;
    for (int i = 0; i < 8; i++)
        cid << std::hex << std::setw(2) << std::setfill('0') << (int)rand_bytes[i];
    ws_path_ = "/?token=" + ws_token_ + "&connectId=" + cid.str();
}

// =============================================================================
// IO thread (WebSocket loop)
// =============================================================================

void KuCoinConnector::io_thread_main() {
    int retry_count = 0;
    while (running_.load()) {
        try {
            fetch_ws_credentials();

            // Load initial balance via REST
            {
                json accounts = rest_get("/api/v1/accounts", "type=trade");
                Balance bal;
                bal.quote_currency = quote_currency_;
                for (auto& acct : accounts) {
                    std::string ccy = acct.value("currency", "");
                    double avail = safe_stod(acct.value("available", "0"));
                    if (ccy == quote_currency_) bal.usd   = avail;
                    if (ccy == base_token_)     bal.token = avail;
                }
                std::lock_guard<std::mutex> lk(state_mutex_);
                latest_balance_ = bal;
                balance_ready_  = true;
            }

            // Open orders — non-fatal (may lack permissions or have no active orders)
            try {
                json orders_data = rest_get("/api/v1/orders",
                    "status=active&symbol=" + symbol_);
                std::map<std::string, Order> orders_map;
                for (auto& item : orders_data["items"])
                    orders_map[item.value("id","?")] = json_to_order(item);
                std::lock_guard<std::mutex> lk(state_mutex_);
                open_orders_ = orders_map;
            } catch (const std::exception& e) {
                fprintf(stderr, "[KuCoinConnector] open orders fetch skipped: %s\n", e.what());
                fflush(stderr);
            }

            // Bootstrap ticker from REST so connect() doesn't wait for a WS push.
            // For low-liquidity pairs the WS ticker channel only pushes on trade
            // activity — the REST snapshot ensures ticker_ready_ is set immediately.
            {
                json tk = rest_get("/api/v1/market/stats", "symbol=" + symbol_);
                Ticker t;
                t.bid       = safe_stod(tk.value("buy",  "0"));
                t.ask       = safe_stod(tk.value("sell", "0"));
                t.last      = safe_stod(tk.value("last", "0"));
                if (t.bid <= 0 && t.ask <= 0) { t.bid = t.last * 0.999; t.ask = t.last * 1.001; }
                t.mid       = (t.bid + t.ask) / 2.0;
                t.timestamp = now_s();
                std::lock_guard<std::mutex> lk(state_mutex_);
                latest_ticker_ = t;
                ticker_ready_  = true;
                cache_cv_.notify_all();
            }

            // Open WebSocket
            WsClient ws;
            ws.connect(ws_host_, ws_path_);

            auto make_sub = [](const std::string& id,
                               const std::string& topic,
                               bool priv) {
                return json{{"id",id},{"type","subscribe"},
                            {"topic",topic},{"privateChannel",priv},
                            {"response",true}}.dump();
            };
            ws.send_text(make_sub("sub1", "/market/ticker:" + symbol_, false));
            ws.send_text(make_sub("sub2", "/spotMarket/tradeOrders",    true));
            ws.send_text(make_sub("sub3", "/account/balance",            true));

            auto last_ping = std::chrono::steady_clock::now();
            int recv_timeout_ms = std::min(ws_ping_interval_ms_ / 3, 3000);

            bool backoff_reset = false;
            while (running_.load()) {
                std::string msg = ws.recv_msg(recv_timeout_ms);
                if (!msg.empty()) {
                    on_ws_message(msg);
                    if (!backoff_reset) { retry_count = 0; backoff_reset = true; }
                }

                auto now = std::chrono::steady_clock::now();
                auto elapsed = std::chrono::duration_cast<std::chrono::milliseconds>(
                    now - last_ping).count();
                if (elapsed >= ws_ping_interval_ms_) {
                    ws.send_text(json{{"id","ping"},{"type","ping"}}.dump());
                    last_ping = now;
                }
            }
        } catch (const std::exception& e) {
            fprintf(stderr, "[KuCoinConnector] io_thread error: %s\n", e.what());
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

void KuCoinConnector::on_ws_message(const std::string& raw) {
    json j = json::parse(raw, nullptr, false);
    if (j.is_discarded()) return;

    std::string type  = j.value("type",  "");
    std::string topic = j.value("topic", "");

    if (type == "message") {
        if (topic.find("/market/ticker")          != std::string::npos) on_ticker_msg(j["data"]);
        else if (topic.find("/spotMarket/tradeOrders") != std::string::npos) on_order_msg(j["data"]);
        else if (topic.find("/account/balance")   != std::string::npos) on_balance_msg(j["data"]);
    }
}

void KuCoinConnector::on_ticker_msg(const nlohmann::json& data) {
    Ticker t;
    t.bid       = safe_stod(data.value("bestBid", "0"));
    t.ask       = safe_stod(data.value("bestAsk", "0"));
    t.last      = safe_stod(data.value("price",   "0"));
    t.mid       = (t.bid + t.ask) / 2.0;
    t.timestamp = now_s();
    {
        std::lock_guard<std::mutex> lk(state_mutex_);
        latest_ticker_ = t;
        ticker_ready_  = true;
    }
    cache_cv_.notify_all();
}

void KuCoinConnector::on_order_msg(const nlohmann::json& data) {
    std::string oid  = data.value("orderId", "");
    std::string type = data.value("type",    "");
    if (oid.empty()) return;

    std::lock_guard<std::mutex> lk(state_mutex_);
    if (type == "open") {
        Order o;
        o.id         = oid;
        o.exchange   = "kucoin";
        o.symbol     = data.value("symbol", symbol_);
        o.side       = data.value("side",   "");
        o.price      = safe_stod(data.value("price", "0"));
        o.amount     = safe_stod(data.value("size",  "0"));
        o.amount_usd = o.price * o.amount;
        o.status     = "open";
        o.timestamp  = static_cast<double>(data.value("ts", 0LL)) / 1e9;
        open_orders_[oid] = o;
    } else if (type == "filled" || type == "canceled") {
        open_orders_.erase(oid);
    } else if (type == "match" && open_orders_.count(oid)) {
        auto& o = open_orders_[oid];
        o.filled_amount += safe_stod(data.value("matchSize",  "0"));
        o.filled_price   = safe_stod(data.value("matchPrice", "0"));
        o.amount        -= safe_stod(data.value("matchSize",  "0"));
    }
}

void KuCoinConnector::on_balance_msg(const nlohmann::json& data) {
    std::string ccy = data.value("currency",  "");
    double avail    = safe_stod(data.value("available", "0"));

    std::lock_guard<std::mutex> lk(state_mutex_);
    if (ccy == quote_currency_) latest_balance_.usd   = avail;
    if (ccy == base_token_)     latest_balance_.token = avail;
    balance_ready_ = true;
    cache_cv_.notify_all();
}

// =============================================================================
// Cache reads (no network, <1ms)
// =============================================================================

Ticker KuCoinConnector::fetch_ticker() {
    if (!connected_) throw std::runtime_error("KuCoinConnector::fetch_ticker: not connected");
    std::lock_guard<std::mutex> lk(state_mutex_);
    if (!ticker_ready_) throw std::runtime_error("KuCoinConnector::fetch_ticker: no data yet");
    return latest_ticker_;
}

Balance KuCoinConnector::fetch_balance() {
    if (!connected_) throw std::runtime_error("KuCoinConnector::fetch_balance: not connected");
    std::lock_guard<std::mutex> lk(state_mutex_);
    return latest_balance_;
}

std::vector<Order> KuCoinConnector::fetch_open_orders() {
    if (!connected_) throw std::runtime_error("KuCoinConnector::fetch_open_orders: not connected");
    std::lock_guard<std::mutex> lk(state_mutex_);
    std::vector<Order> result;
    result.reserve(open_orders_.size());
    for (auto& [id, o] : open_orders_) result.push_back(o);
    return result;
}

// =============================================================================
// REST calls (blocking)
// =============================================================================

Order KuCoinConnector::create_limit_order(const std::string& side, double price, double amount) {
    if (!connected_) throw std::runtime_error("KuCoinConnector::create_limit_order: not connected");

    unsigned char rand_bytes[8];
    RAND_bytes(rand_bytes, 8);
    std::ostringstream coid;
    coid << "alkimi";
    for (int i = 0; i < 8; i++)
        coid << std::hex << std::setw(2) << std::setfill('0') << (int)rand_bytes[i];

    json body = {
        {"clientOid", coid.str()},
        {"side",      side},
        {"symbol",    symbol_},
        {"type",      "limit"},
        {"price",     std::to_string(price)},
        {"size",      std::to_string(amount)},
    };

    json data = rest_post("/api/v1/orders", body);
    Order o;
    o.id         = data.value("orderId", "");
    o.exchange   = "kucoin";
    o.symbol     = symbol_;
    o.side       = side;
    o.price      = price;
    o.amount     = amount;
    o.amount_usd = price * amount;
    o.status     = "open";
    o.timestamp  = now_s();
    return o;
}

void KuCoinConnector::cancel_order(const std::string& order_id) {
    if (!connected_) throw std::runtime_error("KuCoinConnector::cancel_order: not connected");
    rest_delete("/api/v1/orders/" + order_id);
}

void KuCoinConnector::cancel_all_orders() {
    if (!connected_) throw std::runtime_error("KuCoinConnector::cancel_all_orders: not connected");
    rest_delete("/api/v1/orders", "symbol=" + symbol_);
}

std::vector<Fill> KuCoinConnector::fetch_fills(double since_ts, int limit) {
    if (!connected_) throw std::runtime_error("KuCoinConnector::fetch_fills: not connected");
    std::string query = "symbol=" + symbol_ + "&pageSize=" + std::to_string(limit);
    if (since_ts > 0.0)
        query += "&startAt=" + std::to_string(static_cast<long long>(since_ts * 1000));
    json data = rest_get("/api/v1/fills", query);
    std::vector<Fill> fills;
    if (data.contains("items"))
        for (auto& item : data["items"]) fills.push_back(json_to_fill(item));
    return fills;
}

std::vector<Candle> KuCoinConnector::fetch_candles(const std::string& timeframe, int limit) {
    std::string kc_tf = map_timeframe(timeframe);
    std::string query = "type=" + kc_tf + "&symbol=" + symbol_;
    auto resp = https_request(KC_REST_HOST, "GET",
        "/api/v1/market/candles?" + query, {}, "");
    if (resp.status < 200 || resp.status >= 300)
        throw std::runtime_error("fetch_candles: HTTP " + std::to_string(resp.status));
    json j = json::parse(resp.body, nullptr, false);
    if (j.is_discarded()) throw std::runtime_error("fetch_candles: JSON parse error");
    if (j.value("code","") != "200000")
        throw std::runtime_error("fetch_candles: code=" + j.value("code","?"));

    auto& rows = j["data"];
    int n = static_cast<int>(rows.size());
    int start = std::max(0, n - limit);
    std::vector<Candle> candles;
    for (int i = n - 1; i >= start; --i) {
        auto& row = rows[i];
        Candle c;
        c.timestamp = safe_stod(row[0].get<std::string>());
        c.open      = safe_stod(row[1].get<std::string>());
        c.close     = safe_stod(row[2].get<std::string>());
        c.high      = safe_stod(row[3].get<std::string>());
        c.low       = safe_stod(row[4].get<std::string>());
        c.volume    = safe_stod(row[5].get<std::string>());
        candles.push_back(c);
    }
    return candles;
}

// =============================================================================
// Conversion helpers
// =============================================================================

Order KuCoinConnector::json_to_order(const nlohmann::json& j) const {
    Order o;
    o.id           = j.value("id",      "");
    o.exchange     = "kucoin";
    o.symbol       = j.value("symbol",  symbol_);
    o.side         = j.value("side",    "");
    o.price        = safe_stod(j.value("price", "0"));
    o.amount       = safe_stod(j.value("size",  "0"));
    o.amount_usd   = o.price * o.amount;
    o.status       = j.value("isActive", false) ? "open"
                   : j.value("cancelExist", false) ? "canceled" : "filled";
    double ts_raw  = 0.0;
    if (j.contains("createdAt")) {
        const auto& v = j["createdAt"];
        if (v.is_number())      ts_raw = v.get<double>();
        else if (v.is_string()) ts_raw = safe_stod(v.get<std::string>());
    }
    o.timestamp     = ts_raw > 1e12 ? ts_raw / 1000.0 : ts_raw;
    o.filled_amount = safe_stod(j.value("dealSize",  "0"));
    double deal_funds = safe_stod(j.value("dealFunds", "0"));
    o.filled_price  = (o.filled_amount > 0.0) ? deal_funds / o.filled_amount : 0.0;
    o.fee           = safe_stod(j.value("fee", "0"));
    o.fee_currency  = j.value("feeCurrency", "");
    return o;
}

Fill KuCoinConnector::json_to_fill(const nlohmann::json& j) const {
    Fill f;
    f.id            = j.value("tradeId",  "");
    f.order_id      = j.value("orderId",  "");
    f.exchange      = "kucoin";
    f.symbol        = j.value("symbol",   symbol_);
    f.side          = j.value("side",     "");
    f.filled_price  = safe_stod(j.value("price", "0"));
    f.filled_amount = safe_stod(j.value("size",  "0"));
    f.fee           = safe_stod(j.value("fee",   "0"));
    f.fee_currency  = j.value("feeCurrency", "");
    double ts_raw   = 0.0;
    if (j.contains("createdAt")) {
        const auto& v = j["createdAt"];
        if (v.is_number())      ts_raw = v.get<double>();
        else if (v.is_string()) ts_raw = safe_stod(v.get<std::string>());
    }
    f.timestamp  = ts_raw > 1e12 ? ts_raw / 1000.0 : ts_raw;
    f.pnl_usd    = 0.0;
    return f;
}

std::string KuCoinConnector::map_timeframe(const std::string& tf) {
    static const std::map<std::string, std::string> kMap = {
        {"1m","1min"},{"3m","3min"},{"5m","5min"},{"15m","15min"},
        {"30m","30min"},{"1h","1hour"},{"2h","2hour"},{"4h","4hour"},
        {"6h","6hour"},{"8h","8hour"},{"12h","12hour"},
        {"1d","1day"},{"1w","1week"},
    };
    auto it = kMap.find(tf);
    return (it != kMap.end()) ? it->second : "1min";
}
