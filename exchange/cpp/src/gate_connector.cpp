/**
 * gate_connector.cpp  —  Gate.io v4 C++ connector implementation.
 *
 * Lock-free data path
 * ───────────────────
 *   Ticker updates  → SeqlockBuffer<Ticker>
 *   Balance updates → SeqlockBuffer<BalanceData>
 *   Order events    → SpscQueue<OrderEvent, 512>
 *
 * The only mutexes remaining:
 *   rest_mutex_    — serialises HTTP requests (not on the read hot path)
 *   connect_mutex_ — one-time startup condition variable (never used after connect)
 */

#include "gate_connector.h"
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

#include <nlohmann/json.hpp>
using json = nlohmann::json;

// =============================================================================
// Internal helpers (private to this translation unit)
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

// Gate.io REST responses are bare JSON (no {"code":"200000"} envelope).
// Success: HTTP 200/201 with the data directly.
// Error:   HTTP 4xx/5xx with {"label":"...","message":"..."}.
json gate_unwrap(const HttpResponse& r, const std::string& ctx) {
    if (r.status == 0)
        throw std::runtime_error(ctx + ": no response from server");
    if (r.status < 200 || r.status >= 300)
        throw std::runtime_error(ctx + ": HTTP " + std::to_string(r.status) + " — " + r.body);
    json j = json::parse(r.body, nullptr, false);
    if (j.is_discarded())
        throw std::runtime_error(ctx + ": JSON parse error — " + r.body);
    if (j.is_object() && j.contains("label"))
        throw std::runtime_error(ctx + ": Gate.io error=" + j.value("label","?") +
                                 " msg=" + j.value("message","?"));
    return j;
}

// Normalise any symbol variant to Gate.io's underscore format.
// "ALKIMI/USDT" → "ALKIMI_USDT"
// "ALKIMI-USDT" → "ALKIMI_USDT"
std::string to_gate_symbol(const std::string& s) {
    std::string r = s;
    for (char& c : r) if (c == '/' || c == '-') c = '_';
    return r;
}

} // namespace

static constexpr char GATE_REST_HOST[] = "api.gateio.ws";
static constexpr char GATE_WS_HOST[]   = "api.gateio.ws";
static constexpr char GATE_WS_PATH[]   = "/ws/v4/";

// =============================================================================
// Constructor / Destructor
// =============================================================================

GateConnector::GateConnector(
    const std::string& symbol,
    const std::string& api_key,
    const std::string& api_secret,
    const std::string& quote_currency
)
    : BaseConnector("gate", symbol)
    , api_key_(api_key)
    , api_secret_(api_secret)
    , quote_currency_(quote_currency)
{
    gate_symbol_ = to_gate_symbol(symbol);
    auto underscore = gate_symbol_.find('_');
    base_token_ = (underscore != std::string::npos)
                ? gate_symbol_.substr(0, underscore)
                : gate_symbol_;
}

GateConnector::~GateConnector() {
    if (running_.load()) {
        try { disconnect(); } catch (...) {}
    }
}

// =============================================================================
// Lifecycle
// =============================================================================

void GateConnector::connect() {
    if (running_.load()) return;
    running_.store(true);
    io_thread_ = std::thread(&GateConnector::io_thread_main, this);

    // Block until io_thread completes its REST bootstrap (ticker + balance).
    // connect_mutex_ is only used here — not on any hot path.
    std::unique_lock<std::mutex> lk(connect_mutex_);
    bool ok = cache_cv_.wait_for(lk, std::chrono::seconds(15), [this] {
        return (ticker_ready_.load(std::memory_order_acquire) &&
                balance_ready_.load(std::memory_order_acquire) &&
                ws_ready_.load(std::memory_order_acquire)) ||
               !running_.load(std::memory_order_acquire);
    });

    if (!ok) {
        running_.store(false);
        if (io_thread_.joinable()) io_thread_.join();
        throw std::runtime_error("GateConnector::connect: timeout waiting for initial data");
    }
    if (!running_.load()) {
        if (io_thread_.joinable()) io_thread_.join();
        throw std::runtime_error("GateConnector::connect: io_thread failed during startup");
    }
    connected_ = true;
}

void GateConnector::disconnect() {
    running_.store(false);
    connected_ = false;
    cache_cv_.notify_all();
    if (io_thread_.joinable()) io_thread_.join();
}

// =============================================================================
// REST auth
// =============================================================================

std::map<std::string, std::string> GateConnector::make_auth_headers(
    const std::string& method,
    const std::string& path,
    const std::string& query,
    const std::string& body) const
{
    std::string ts   = timestamp_s();
    std::string sign = gate_sign(api_secret_, method, path, query, ts, body);
    return {
        { "KEY",          api_key_ },
        { "SIGN",         sign     },
        { "Timestamp",    ts       },
        { "Content-Type", "application/json" },
    };
}

// Per-channel WS auth: hex(HMAC-SHA512(secret, "channel=<ch>&event=subscribe&time=<ts>"))
std::string GateConnector::ws_sign(const std::string& channel, long long ts) const {
    std::string message = "channel=" + channel
                        + "&event=subscribe"
                        + "&time=" + std::to_string(ts);
    return hex_encode(hmac_sha512(api_secret_, message));
}

// spot.login signature: HMAC-SHA512(secret, "api\nspot.login\n\n{ts}")
// Formula from Gate.io's WS auth spec (event + channel + empty_body + ts).
std::string GateConnector::ws_sign_login(long long ts) const {
    std::string message = "api\nspot.login\n\n" + std::to_string(ts);
    return hex_encode(hmac_sha512(api_secret_, message));
}

// Authenticate the WS session via spot.login.
// Must be called once after the socket opens; order requests reuse the session.
//
// Gate.io's login acknowledgement uses ack=true on the result message — there
// is no separate non-ack final response.  We therefore fire-and-forget the
// login and sleep briefly (the same pattern MMP-CEX-GATE uses), relying on
// the fact that the same credentials already work for REST.  If the login
// were to fail (wrong key), the first order request would return an
// AUTHENTICATION_FAILED error and surface the problem clearly.
void GateConnector::ws_login() {
    long long ts = static_cast<long long>(
        std::chrono::duration_cast<std::chrono::seconds>(
            std::chrono::system_clock::now().time_since_epoch()).count());

    std::string req_id = std::to_string(req_id_counter_.fetch_add(1) + 1);

    json msg = {
        {"time",    ts},
        {"channel", "spot.login"},
        {"event",   "api"},
        {"payload", {
            {"api_key",   api_key_},
            {"signature", ws_sign_login(ts)},
            {"timestamp", std::to_string(ts)},
            {"req_id",    req_id},
        }},
    };

    {
        std::lock_guard<std::mutex> lk(ws_send_mutex_);
        if (!ws_ptr_)
            throw std::runtime_error("ws_login: WebSocket not connected");
        ws_ptr_->send_text(msg.dump());
    }

    // Give Gate.io time to process the login before we send order requests.
    std::this_thread::sleep_for(std::chrono::milliseconds(500));
    fprintf(stderr, "[GateConnector] WS login sent — session should be authenticated\n");
    fflush(stderr);
}

// =============================================================================
// REST method helpers
// =============================================================================

nlohmann::json GateConnector::rest_get(const std::string& path, const std::string& query) {
    std::lock_guard<std::mutex> lk(rest_mutex_);
    auto hdrs = make_auth_headers("GET", path, query, "");
    std::string pq = query.empty() ? path : path + "?" + query;
    return gate_unwrap(https_request(GATE_REST_HOST, "GET", pq, hdrs, ""), "GET " + path);
}

nlohmann::json GateConnector::rest_post(const std::string& path, const nlohmann::json& body) {
    std::lock_guard<std::mutex> lk(rest_mutex_);
    std::string body_str = body.dump();
    auto hdrs = make_auth_headers("POST", path, "", body_str);
    return gate_unwrap(https_request(GATE_REST_HOST, "POST", path, hdrs, body_str), "POST " + path);
}

nlohmann::json GateConnector::rest_delete(const std::string& path, const std::string& query) {
    std::lock_guard<std::mutex> lk(rest_mutex_);
    auto hdrs = make_auth_headers("DELETE", path, query, "");
    std::string pq = query.empty() ? path : path + "?" + query;
    return gate_unwrap(https_request(GATE_REST_HOST, "DELETE", pq, hdrs, ""), "DELETE " + path);
}

// =============================================================================
// WS API request / response
// =============================================================================

// Fail every in-flight WS API request with the given reason.
// Called when the WebSocket drops so callers unblock immediately with an error
// rather than waiting for the timeout.
void GateConnector::fail_pending_requests(const std::string& reason) {
    std::lock_guard<std::mutex> lk(pending_mutex_);
    for (auto& [id, prom] : pending_requests_) {
        try {
            prom.set_exception(std::make_exception_ptr(
                std::runtime_error(reason)));
        } catch (...) {}   // promise may already have a value — ignore
    }
    pending_requests_.clear();
}

// Send a WS API request (event=api) and block until the matching response
// arrives.  Returns the data.result object from Gate.io's response.
// Throws on timeout, WS not connected, or exchange-side error.
nlohmann::json GateConnector::ws_api_request(const std::string&    channel,
                                              const nlohmann::json& req_param,
                                              int                   timeout_ms) {
    if (!connected_)
        throw std::runtime_error("GateConnector::ws_api_request: not connected");

    long long ts = static_cast<long long>(
        std::chrono::duration_cast<std::chrono::seconds>(
            std::chrono::system_clock::now().time_since_epoch()).count());

    // Use a monotonically increasing counter as the request ID.
    // fetch_add returns the old value, so +1 gives us 1-based IDs.
    std::string req_id = std::to_string(req_id_counter_.fetch_add(1) + 1);

    // Register the promise BEFORE sending — if the response somehow arrives
    // before we finish registering, on_ws_message would drop it (not found in
    // the map).  Register first, then send.
    std::future<nlohmann::json> fut;
    {
        std::lock_guard<std::mutex> lk(pending_mutex_);
        fut = pending_requests_.emplace(req_id, std::promise<nlohmann::json>{})
                               .first->second.get_future();
    }

    // No auth block — the WS session was authenticated via ws_login().
    json msg = {
        {"time",    ts},
        {"channel", channel},
        {"event",   "api"},
        {"payload", {
            {"req_id",    req_id},
            {"req_param", req_param},
        }},
    };

    // Send — hold ws_send_mutex_ only for the duration of send_text().
    // Never hold ws_send_mutex_ and pending_mutex_ at the same time.
    bool sent = false;
    {
        std::lock_guard<std::mutex> lk(ws_send_mutex_);
        if (ws_ptr_) {
            ws_ptr_->send_text(msg.dump());
            sent = true;
        }
    }

    if (!sent) {
        // WS is not up — remove the promise we just registered and bail.
        std::lock_guard<std::mutex> lk(pending_mutex_);
        pending_requests_.erase(req_id);
        throw std::runtime_error("GateConnector::ws_api_request: WebSocket not connected");
    }

    // Block until io_thread delivers the response or the timeout fires.
    if (fut.wait_for(std::chrono::milliseconds(timeout_ms))
            == std::future_status::timeout) {
        std::lock_guard<std::mutex> lk(pending_mutex_);
        pending_requests_.erase(req_id);
        throw std::runtime_error(channel + " WS request timed out after "
                                 + std::to_string(timeout_ms) + " ms");
    }

    return fut.get();   // re-throws if promise was set with an exception
}

// =============================================================================
// IO thread (WebSocket loop)
// =============================================================================

void GateConnector::io_thread_main() {
    int retry_count = 0;

    while (running_.load()) {
        try {
            // ── REST bootstrap ──────────────────────────────────────────────
            // Fetch the initial snapshot so connect() can return immediately
            // instead of waiting for the first WebSocket push (which may never
            // come for a low-liquidity pair with no recent trades).

            // 1. Balance
            {
                json accounts = rest_get("/api/v4/spot/accounts", "");
                BalanceData bd;
                for (auto& acct : accounts) {
                    std::string ccy  = acct.value("currency",  "");
                    double avail     = safe_stod(acct.value("available", "0"));
                    if (ccy == quote_currency_) bd.usd   = avail;
                    if (ccy == base_token_)     bd.token = avail;
                }
                // Seqlock write: reader (fetch_balance) sees a consistent
                // snapshot — never a half-updated pair of doubles.
                balance_adb_.store(bd);
                balance_ready_.store(true, std::memory_order_release);
            }

            // 2. Open orders (non-fatal: wrong permissions or no active orders)
            try {
                json orders_arr = rest_get("/api/v4/spot/orders",
                    "currency_pair=" + gate_symbol_ + "&status=open");
                if (orders_arr.is_array()) {
                    for (auto& item : orders_arr) {
                        OrderEvent ev;
                        ev.type  = OrderEventType::OPEN;
                        ev.order = json_to_order(item);
                        // Drop silently if queue is full (should never happen
                        // at bootstrap — 512 slots dwarfs any realistic order book).
                        if (!order_queue_.push(ev)) {
                            fprintf(stderr, "[GateConnector] order_queue_ full during bootstrap\n");
                            fflush(stderr);
                        }
                    }
                }
            } catch (const std::exception& e) {
                fprintf(stderr, "[GateConnector] open orders fetch skipped: %s\n", e.what());
                fflush(stderr);
            }

            // 3. Ticker
            {
                json tickers = rest_get("/api/v4/spot/tickers",
                    "currency_pair=" + gate_symbol_);
                if (tickers.is_array() && !tickers.empty()) {
                    auto& t0 = tickers[0];
                    Ticker t;
                    t.ask  = safe_stod(t0.value("lowest_ask",  "0"));
                    t.bid  = safe_stod(t0.value("highest_bid", "0"));
                    t.last = safe_stod(t0.value("last", "0"));
                    if (t.bid <= 0.0 && t.ask <= 0.0) {
                        t.bid = t.last * 0.999;
                        t.ask = t.last * 1.001;
                    }
                    t.mid       = (t.bid + t.ask) / 2.0;
                    t.timestamp = now_s();
                    // Seqlock write: atomic from the reader's perspective.
                    ticker_sl_.store(t);
                    ticker_ready_.store(true, std::memory_order_release);
                }
            }

            // 4. Exchange order filter (price/amount precision, min/max sizes)
            // Non-fatal: if this REST call fails we fall back to safe defaults
            // (8 decimal places, no min/max).  Rejected orders will still throw
            // with a clear error message from Gate.io.
            try {
                json pair_info = rest_get(
                    "/api/v4/spot/currency_pairs/" + gate_symbol_, "");

                order_filter_.price_precision  = pair_info.value("precision",        8);
                order_filter_.amount_precision = pair_info.value("amount_precision", 8);

                // Both max fields can be JSON null — treat null as "no limit".
                auto nullable_double = [&](const char* key, double def) -> double {
                    if (!pair_info.contains(key) || pair_info[key].is_null()) return def;
                    const auto& v = pair_info[key];
                    if (v.is_string()) return safe_stod(v.get<std::string>());
                    if (v.is_number()) return v.get<double>();
                    return def;
                };
                order_filter_.min_base_amount  = nullable_double("min_base_amount",  0.0);
                order_filter_.max_base_amount  = nullable_double("max_base_amount",  1e15);
                order_filter_.min_quote_amount = nullable_double("min_quote_amount", 0.0);
                order_filter_.max_quote_amount = nullable_double("max_quote_amount", 1e15);
                order_filter_.loaded = true;

                fprintf(stderr,
                    "[GateConnector] filter loaded: price_precision=%d "
                    "amount_precision=%d min_base=%.4f min_quote=%.4f\n",
                    order_filter_.price_precision, order_filter_.amount_precision,
                    order_filter_.min_base_amount, order_filter_.min_quote_amount);
                fflush(stderr);
            } catch (const std::exception& e) {
                fprintf(stderr,
                    "[GateConnector] exchange info load failed: %s — using defaults\n",
                    e.what());
                fflush(stderr);
            }

            // Wake up connect() — it is waiting on this condition variable.
            // No lock needed when notifying; the predicate re-checks the
            // atomic flags so the notification can't be missed even if it
            // fires before wait_for is entered.
            cache_cv_.notify_all();

            // ── WebSocket connection ────────────────────────────────────────
            WsClient ws;
            ws.connect(GATE_WS_HOST, GATE_WS_PATH);

            // Publish ws_ptr_ so ws_login() / ws_api_request() can send.
            {
                std::lock_guard<std::mutex> lk(ws_send_mutex_);
                ws_ptr_ = &ws;
            }

            // Authenticate the WS session (spot.login) before signalling ready.
            // connect() waits for ws_ready_ — this ensures it cannot return
            // until order placement is possible.
            ws_login();

            ws_ready_.store(true, std::memory_order_release);
            cache_cv_.notify_all();

            auto sub_ts = static_cast<long long>(
                std::chrono::duration_cast<std::chrono::seconds>(
                    std::chrono::system_clock::now().time_since_epoch()).count());

            // Public ticker channel — no auth needed.
            ws.send_text(json{
                {"time",    sub_ts},
                {"channel", "spot.tickers"},
                {"event",   "subscribe"},
                {"payload", {gate_symbol_}},
            }.dump());

            // Private order channel — per-channel HMAC-SHA512 auth.
            ws.send_text(json{
                {"time",    sub_ts},
                {"channel", "spot.orders"},
                {"event",   "subscribe"},
                {"payload", {gate_symbol_}},
                {"auth",    {
                    {"method", "api_key"},
                    {"KEY",    api_key_},
                    {"SIGN",   ws_sign("spot.orders", sub_ts)},
                }},
            }.dump());

            // Private balance channel.
            ws.send_text(json{
                {"time",    sub_ts},
                {"channel", "spot.balances"},
                {"event",   "subscribe"},
                {"auth",    {
                    {"method", "api_key"},
                    {"KEY",    api_key_},
                    {"SIGN",   ws_sign("spot.balances", sub_ts)},
                }},
            }.dump());

            // ── Receive loop ────────────────────────────────────────────────
            auto last_ping      = std::chrono::steady_clock::now();
            int  recv_timeout   = std::min(ping_interval_ms_ / 3, 3000);
            bool backoff_reset  = false;

            while (running_.load()) {
                std::string msg = ws.recv_msg(recv_timeout);

                if (!msg.empty()) {
                    on_ws_message(msg);
                    // Reset backoff only after the first real message —
                    // confirms the connection is genuinely stable.
                    if (!backoff_reset) {
                        retry_count  = 0;
                        backoff_reset = true;
                    }
                }

                // Heartbeat — Gate.io drops idle connections after ~60 s.
                auto now     = std::chrono::steady_clock::now();
                auto elapsed = std::chrono::duration_cast<std::chrono::milliseconds>(
                    now - last_ping).count();
                if (elapsed >= ping_interval_ms_) {
                    long long pts = static_cast<long long>(
                        std::chrono::duration_cast<std::chrono::seconds>(
                            now.time_since_epoch()).count());
                    ws.send_text(json{
                        {"time",    pts},
                        {"channel", "spot.ping"},
                    }.dump());
                    last_ping = now;
                }
            }

        } catch (const std::exception& e) {
            fprintf(stderr, "[GateConnector] io_thread error: %s\n", e.what());
            fflush(stderr);
        }

        // ── Teardown (clean exit or exception) ─────────────────────────────
        // Clear ws_ready_ first so any caller blocked in ws_api_request gets
        // "not connected" immediately rather than sending on a dead socket.
        ws_ready_.store(false, std::memory_order_release);
        // Clear ws_ptr_ so ws_api_request() stops trying to send.
        {
            std::lock_guard<std::mutex> lk(ws_send_mutex_);
            ws_ptr_ = nullptr;
        }
        // Unblock any thread waiting on a WS API response.
        fail_pending_requests("[GateConnector] WebSocket disconnected — reconnecting");

        if (!running_.load()) break;

        // Exponential backoff: 2 → 4 → 8 → 16 → 32 → 60 s max.
        int delay_s = std::min(2 << retry_count, 60);
        retry_count++;
        std::this_thread::sleep_for(std::chrono::seconds(delay_s));
    }
}

// =============================================================================
// WS message dispatch
// =============================================================================

void GateConnector::on_ws_message(const std::string& raw) {
    json j = json::parse(raw, nullptr, false);
    if (j.is_discarded()) return;

    // ── WS API response (create_limit_order / cancel_order) ──────────────────
    // Gate.io's real format uses a top-level "request_id" field, NOT event="api":
    //   ACK (skip):  {"request_id":"...","ack":true,...}
    //   Result:      {"request_id":"...","header":{"status":"200"},
    //                 "data":{"result":{...},"errs":null},"ack":false}
    // Two messages arrive per WS API request — skip the ACK, fulfil on result.
    if (j.contains("request_id")) {
        if (j.value("ack", false)) return;   // ACK frame — real result comes next

        std::string req_id = j.value("request_id", "");
        if (req_id.empty()) return;

        std::lock_guard<std::mutex> lk(pending_mutex_);
        auto it = pending_requests_.find(req_id);
        if (it == pending_requests_.end()) return;

        // Status is a string like "200" or "400".  Any '2xx' is success.
        std::string status = "0";
        if (j.contains("header") && j["header"].contains("status"))
            status = j["header"].value("status", "0");
        bool ok = !status.empty() && status[0] == '2';

        if (ok) {
            // Return just data.result — callers (create_limit_order) use it directly.
            json result = (j.contains("data") && j["data"].contains("result"))
                        ? j["data"]["result"]
                        : json::object();
            it->second.set_value(std::move(result));
        } else {
            std::string label  = "UNKNOWN";
            std::string errmsg = "";
            if (j.contains("data") && j["data"].contains("errs") &&
                    !j["data"]["errs"].is_null()) {
                label  = j["data"]["errs"].value("label",   "UNKNOWN");
                errmsg = j["data"]["errs"].value("message", "");
            }
            it->second.set_exception(std::make_exception_ptr(
                std::runtime_error("Gate.io WS error [" + j.value("channel","?") + "]: "
                                   + label + (errmsg.empty() ? "" : " — " + errmsg))));
        }
        pending_requests_.erase(it);
        return;
    }

    std::string channel = j.value("channel", "");
    std::string event   = j.value("event",   "");

    // Skip subscription ACKs and heartbeat replies.
    if (event == "subscribe" || event == "pong" || channel == "spot.pong") return;
    if (!j.contains("result")) return;

    // ── Market data / account updates ─────────────────────────────────────────
    const json& result = j["result"];

    if      (channel == "spot.tickers")  on_ticker_msg(result);
    else if (channel == "spot.orders")   on_order_msg(result);
    else if (channel == "spot.balances") on_balance_msg(result);
}

// ── Ticker ────────────────────────────────────────────────────────────────────
// Gate.io pushes this on every trade that changes the best bid/ask.
// We write atomically via the seqlock — fetch_ticker() on the Python side
// reads without any lock.
void GateConnector::on_ticker_msg(const nlohmann::json& result) {
    Ticker t;
    t.bid       = safe_stod(result.value("highest_bid", "0"));
    t.ask       = safe_stod(result.value("lowest_ask",  "0"));
    t.last      = safe_stod(result.value("last",        "0"));
    t.mid       = (t.bid + t.ask) / 2.0;
    t.timestamp = now_s();

    // Seqlock store: reader sees either the old Ticker or the new one in full —
    // never a mix of old bid and new ask (which would give a nonsensical mid).
    ticker_sl_.store(t);

    // ticker_ready_ was set during bootstrap; no need to set it again.
    // Notify connect() only if it is still waiting (first update ever).
    if (!ticker_ready_.load(std::memory_order_relaxed)) {
        ticker_ready_.store(true, std::memory_order_release);
        cache_cv_.notify_all();
    }
}

// ── Orders ────────────────────────────────────────────────────────────────────
// Gate.io pushes an array of order objects whenever an order changes state.
// We push each change as an OrderEvent into the SPSC queue.
// fetch_open_orders() (consumer side) drains the queue and maintains its own
// map — no lock, no contention between producer and consumer.
void GateConnector::on_order_msg(const nlohmann::json& result) {
    const json& orders = result.is_array()
                       ? result
                       : (result.contains("orders") ? result["orders"] : json::array());

    for (const auto& item : orders) {
        std::string oid    = item.value("id",     "");
        std::string status = item.value("status", "");
        if (oid.empty()) continue;

        OrderEvent ev;
        if (status == "open") {
            ev.type  = OrderEventType::OPEN;
            ev.order = json_to_order(item);
        } else if (status == "closed") {
            // "closed" on Gate.io means fully filled.
            ev.type     = OrderEventType::FILL;
            ev.order.id = oid;
        } else {
            // "cancelled" or any unrecognised terminal status.
            ev.type     = OrderEventType::CANCEL;
            ev.order.id = oid;
        }

        if (!order_queue_.push(ev)) {
            // Queue is full — this is a safeguard.  At 512 slots it should
            // never happen in normal operation (Meridian drains every tick).
            fprintf(stderr, "[GateConnector] order_queue_ full — event dropped\n");
            fflush(stderr);
        }
    }
}

// ── Balance ───────────────────────────────────────────────────────────────────
// Gate.io may send an array of balance objects (e.g. on subscription ack) or a
// single object (on incremental update).  Handle both.
void GateConnector::on_balance_msg(const nlohmann::json& result) {
    if (result.is_array()) {
        for (const auto& item : result) on_balance_msg(item);
        return;
    }
    std::string ccy = result.value("currency",  "");
    double avail    = safe_stod(result.value("available", "0"));

    // Read current snapshot, apply update, write back atomically.
    BalanceData bd = balance_adb_.load();
    if (ccy == quote_currency_) bd.usd   = avail;
    if (ccy == base_token_)     bd.token = avail;
    balance_adb_.store(bd);

    if (!balance_ready_.load(std::memory_order_relaxed)) {
        balance_ready_.store(true, std::memory_order_release);
        cache_cv_.notify_all();
    }
}

// =============================================================================
// Cache reads — these are the hot-path methods called by Meridian
// =============================================================================

// ── fetch_ticker ──────────────────────────────────────────────────────────────
// Seqlock load: no mutex, no blocking.
// Worst case: ~10 ns spin if io_thread happens to be mid-write (rare).
Ticker GateConnector::fetch_ticker() {
    if (!connected_)
        throw std::runtime_error("GateConnector::fetch_ticker: not connected");
    if (!ticker_ready_.load(std::memory_order_acquire))
        throw std::runtime_error("GateConnector::fetch_ticker: no data yet");
    return ticker_sl_.load();
}

// ── fetch_balance ─────────────────────────────────────────────────────────────
// Seqlock load for the numeric fields.
// Reconstructs the full Balance struct (quote_currency is a compile-time constant).
Balance GateConnector::fetch_balance() {
    if (!connected_)
        throw std::runtime_error("GateConnector::fetch_balance: not connected");
    BalanceData bd = balance_adb_.load();
    Balance b;
    b.usd            = bd.usd;
    b.token          = bd.token;
    b.quote_currency = quote_currency_;   // constant — safe to read without a lock
    return b;
}

// ── fetch_open_orders ─────────────────────────────────────────────────────────
// Drains the SPSC order_queue_ and applies events to consumer_orders_.
// consumer_orders_ is only ever touched here — no io_thread involvement,
// no lock required.
//
// IMPORTANT: must be called from a single consumer thread (Meridian's asyncio
// loop satisfies this — it runs on one OS thread).
std::vector<Order> GateConnector::fetch_open_orders() {
    if (!connected_)
        throw std::runtime_error("GateConnector::fetch_open_orders: not connected");

    // Drain all pending events — apply them in the order they were produced.
    OrderEvent ev;
    while (order_queue_.pop(ev)) {
        switch (ev.type) {
            case OrderEventType::OPEN:
                consumer_orders_[ev.order.id] = ev.order;
                break;
            case OrderEventType::FILL:
            case OrderEventType::CANCEL:
                consumer_orders_.erase(ev.order.id);
                break;
        }
    }

    // Return a snapshot of the current open orders.
    std::vector<Order> result;
    result.reserve(consumer_orders_.size());
    for (auto& [id, o] : consumer_orders_) result.push_back(o);
    return result;
}

// =============================================================================
// Order management — place and cancel over the open WebSocket connection.
// Avoids the TCP+TLS handshake cost of a new HTTPS connection on every call.
// =============================================================================

Order GateConnector::create_limit_order(const std::string& side, double price, double amount) {
    if (!connected_)
        throw std::runtime_error("GateConnector::create_limit_order: not connected");

    // 1. Validate min/max constraints before touching the network.
    order_filter_.validate(amount, price);

    // 2. Round price and amount to the decimal places Gate.io requires.
    //    Sending too many decimal places → INVALID_PARAM rejection.
    //    e.g. ALKIMI has amount_precision=0 (whole numbers only).
    auto [price_str, amount_str] = order_filter_.round(price, amount);

    json req_param = {
        {"currency_pair", gate_symbol_},
        {"type",          "limit"},
        {"account",       "spot"},
        {"side",          side},
        {"amount",        amount_str},
        {"price",         price_str},
        {"time_in_force", "gtc"},
        {"text",          "t-alkimi"},
    };

    // 3. Acquire a rate-limit token (blocks if the bucket is empty).
    //    Gate.io allows 10 order operations/second; normal MM usage (1 cancel +
    //    1 place per second) never hits this — the call returns immediately.
    order_rate_limiter_.acquire();

    // 4. Send over the open WebSocket and wait for the response.
    json res;
    try {
        res = ws_api_request("spot.order_place", req_param);
        order_rate_limiter_.on_success();
    } catch (const std::exception& e) {
        // If Gate.io returned a rate-limit error, apply exponential backoff so
        // the next acquire() waits before retrying (1 s → 2 s → … → 32 s).
        std::string what = e.what();
        if (what.find("RATE_LIMIT") != std::string::npos ||
            what.find("429")        != std::string::npos) {
            order_rate_limiter_.on_rate_limit_hit();
        }
        throw;
    }

    if (!res.is_object() || res.value("id", "").empty())
        throw std::runtime_error("create_limit_order: unexpected WS response — " + res.dump());

    return json_to_order(res);
}

void GateConnector::cancel_order(const std::string& order_id) {
    if (!connected_)
        throw std::runtime_error("GateConnector::cancel_order: not connected");

    json req_param = {
        {"currency_pair", gate_symbol_},
        {"order_id",      order_id},
    };

    // Cancellations count against the same 10 ops/second limit as placements.
    order_rate_limiter_.acquire();
    try {
        ws_api_request("spot.order_cancel", req_param);
        order_rate_limiter_.on_success();
    } catch (const std::exception& e) {
        std::string what = e.what();
        if (what.find("RATE_LIMIT") != std::string::npos ||
            what.find("429")        != std::string::npos) {
            order_rate_limiter_.on_rate_limit_hit();
        }
        throw;
    }
}

void GateConnector::cancel_all_orders() {
    if (!connected_)
        throw std::runtime_error("GateConnector::cancel_all_orders: not connected");
    rest_delete("/api/v4/spot/orders", "currency_pair=" + gate_symbol_);
}

std::vector<Fill> GateConnector::fetch_fills(double since_ts, int limit) {
    if (!connected_)
        throw std::runtime_error("GateConnector::fetch_fills: not connected");

    std::string query = "currency_pair=" + gate_symbol_ +
                        "&limit=" + std::to_string(limit);
    if (since_ts > 0.0)
        query += "&from=" + std::to_string(static_cast<long long>(since_ts));

    json data = rest_get("/api/v4/spot/my_trades", query);
    std::vector<Fill> fills;
    if (data.is_array())
        for (auto& item : data) fills.push_back(json_to_fill(item));
    return fills;
}

std::vector<Candle> GateConnector::fetch_candles(const std::string& timeframe, int limit) {
    std::string tf    = map_timeframe(timeframe);
    std::string query = "currency_pair=" + gate_symbol_ +
                        "&interval=" + tf +
                        "&limit=" + std::to_string(limit);

    auto resp = https_request(GATE_REST_HOST, "GET",
        "/api/v4/spot/candlesticks?" + query, {}, "");
    if (resp.status < 200 || resp.status >= 300)
        throw std::runtime_error("fetch_candles: HTTP " + std::to_string(resp.status));

    json j = json::parse(resp.body, nullptr, false);
    if (j.is_discarded()) throw std::runtime_error("fetch_candles: JSON parse error");

    // Gate.io candle format: [timestamp, quote_vol, close, high, low, open, base_vol]
    std::vector<Candle> candles;
    if (j.is_array()) {
        for (auto& row : j) {
            Candle c;
            c.timestamp = safe_stod(row[0].is_string()
                ? row[0].get<std::string>()
                : std::to_string(row[0].get<long long>()));
            c.close  = safe_stod(row[2].get<std::string>());
            c.high   = safe_stod(row[3].get<std::string>());
            c.low    = safe_stod(row[4].get<std::string>());
            c.open   = safe_stod(row[5].get<std::string>());
            c.volume = row.size() > 6 ? safe_stod(row[6].get<std::string>()) : 0.0;
            candles.push_back(c);
        }
    }
    return candles;
}

// =============================================================================
// Conversion helpers
// =============================================================================

Order GateConnector::json_to_order(const nlohmann::json& j) const {
    Order o;
    o.id         = j.value("id",            "");
    o.exchange   = "gate";
    o.symbol     = gate_symbol_;
    o.side       = j.value("side",          "");
    o.price      = safe_stod(j.value("price",  "0"));
    o.amount     = safe_stod(j.value("amount", "0"));
    o.amount_usd = o.price * o.amount;

    std::string raw_status = j.value("status", "open");
    if      (raw_status == "open")      o.status = "open";
    else if (raw_status == "closed")    o.status = "filled";
    else if (raw_status == "cancelled") o.status = "canceled";
    else                                o.status = raw_status;

    o.timestamp = safe_stod(j.value("create_time", "0"));

    double left         = safe_stod(j.value("left",         "0"));
    double filled_total = safe_stod(j.value("filled_total", "0"));
    o.filled_amount     = o.amount - left;
    o.filled_price      = (o.filled_amount > 0.0) ? filled_total / o.filled_amount : 0.0;
    o.fee               = safe_stod(j.value("fee", "0"));
    o.fee_currency      = j.value("fee_currency", "");
    return o;
}

Fill GateConnector::json_to_fill(const nlohmann::json& j) const {
    Fill f;
    f.id            = j.value("id",       "");
    f.order_id      = j.value("order_id", "");
    f.exchange      = "gate";
    f.symbol        = gate_symbol_;
    f.side          = j.value("side",     "");
    f.filled_price  = safe_stod(j.value("price",  "0"));
    f.filled_amount = safe_stod(j.value("amount", "0"));
    f.fee           = safe_stod(j.value("fee",    "0"));
    f.fee_currency  = j.value("fee_currency", "");
    f.timestamp     = safe_stod(j.value("create_time", "0"));
    f.pnl_usd       = 0.0;
    return f;
}

std::string GateConnector::map_timeframe(const std::string& tf) {
    static const std::map<std::string, std::string> kMap = {
        {"1m","1m"},{"5m","5m"},{"15m","15m"},{"30m","30m"},
        {"1h","1h"},{"4h","4h"},{"8h","8h"},
        {"1d","1d"},{"7d","7d"},{"1w","7d"},{"30d","30d"},
    };
    auto it = kMap.find(tf);
    return (it != kMap.end()) ? it->second : "1m";
}
