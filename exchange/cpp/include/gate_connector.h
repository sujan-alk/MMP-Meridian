#pragma once

/**
 * gate_connector.h  —  Gate.io v4 C++ connector.
 *
 * Threading model
 * ───────────────
 * Two concurrent execution paths share this object:
 *
 *   io_thread_   (background)  — owns the WebSocket connection; runs the
 *                                REST bootstrap on connect/reconnect; writes
 *                                market data into the lock-free structures.
 *
 *   caller thread (Python)     — calls fetch_ticker / fetch_balance /
 *                                fetch_open_orders / create_limit_order etc.
 *                                from Meridian's asyncio event loop.
 *
 * Lock-free data path
 * ───────────────────
 *   Ticker   → SeqlockBuffer<Ticker>
 *              Single writer (io_thread), multiple readers.  Seqlock means
 *              readers never block — worst case is a ~10 ns spin while the
 *              writer is mid-update.
 *
 *   Balance  → SeqlockBuffer<BalanceData>
 *              Balance::quote_currency is a compile-time constant per connector
 *              instance, so only the two numeric fields (usd, token) need to be
 *              shared.  BalanceData is a trivially-copyable POD so the seqlock
 *              constraint is satisfied.
 *
 *   Orders   → SpscQueue<OrderEvent, 512>
 *              io_thread (producer) pushes OPEN / CANCEL / FILL events.
 *              fetch_open_orders() (consumer) drains the queue and applies
 *              events to consumer_orders_ — a map that only the consumer
 *              thread ever touches.  No lock anywhere on this path.
 *
 * WS order placement (create_limit_order / cancel_order)
 * ───────────────────────────────────────────────────────
 *   Orders are placed and cancelled over the already-open WebSocket connection
 *   instead of opening a new HTTPS connection each time.  This eliminates the
 *   TCP+TLS handshake cost (~150-200 ms) on every order, cutting round-trip
 *   latency from ~400 ms to ~150-250 ms.
 *
 *   ws_send_mutex_    — held only for the actual send_text() call (µs).
 *                       Prevents concurrent writes to the TLS socket from the
 *                       calling thread and the io_thread's heartbeat sender.
 *
 *   ws_ptr_           — raw pointer to the WsClient owned by io_thread.
 *                       Set after ws.connect(), cleared before reconnect.
 *                       Guarded by ws_send_mutex_ on all accesses.
 *
 *   pending_mutex_    — guards pending_requests_ map.
 *   pending_requests_ — req_id → std::promise<json>.  Fulfilled by
 *                       on_ws_message() when the matching API response arrives.
 *   req_id_counter_   — monotonically increasing request ID.
 *
 * Remaining mutexes
 * ─────────────────
 *   rest_mutex_    — serialises the REST bootstrap calls inside io_thread.
 *                    fetch_candles() and fetch_fills() also use REST (they are
 *                    not on the hot path and have no WS equivalent).
 *
 *   connect_mutex_ — used only inside connect() for the one-time condition-
 *                    variable wait during startup.  Never touched after that.
 *
 * Gate.io specifics
 * ─────────────────
 *   REST host   : api.gateio.ws        (HTTPS / port 443)
 *   WS  host    : api.gateio.ws        path: /ws/v4/
 *   REST auth   : HMAC-SHA512 (gate_sign in auth_utils)
 *   WS  auth    : per-channel HMAC-SHA512 (ws_sign below)
 *   Symbol fmt  : ALKIMI_USDT (underscore, not slash or dash)
 *   Heartbeat   : JSON ping {"time":ts,"channel":"spot.ping"} every 30 s
 */

#include "connector.h"
#include "net_utils.h"
#include "rate_limiter.h"
#include "seqlock.h"
#include "atomic_double_buffer.h"
#include "spsc_queue.h"

#include <atomic>
#include <condition_variable>
#include <future>
#include <map>
#include <mutex>
#include <sstream>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

#include <nlohmann/json.hpp>

// ─────────────────────────────────────────────────────────────────────────────
// POD types for the seqlock paths
// ─────────────────────────────────────────────────────────────────────────────

// Ticker is already all-doubles in connector.h → trivially copyable.
// SeqlockBuffer<Ticker> works directly.
static_assert(std::is_trivially_copyable_v<Ticker>,
    "Ticker must stay trivially copyable for SeqlockBuffer to work.");

// Balance contains std::string quote_currency which breaks trivial-copyability.
// We store only the two numeric fields in the seqlock and reconstruct the full
// Balance in fetch_balance() using the constant quote_currency_ member.
struct BalanceData {
    double usd   = 0.0;
    double token = 0.0;
};
static_assert(std::is_trivially_copyable_v<BalanceData>);

// ─────────────────────────────────────────────────────────────────────────────
// Order event for the SPSC queue
// ─────────────────────────────────────────────────────────────────────────────

enum class OrderEventType : uint8_t {
    OPEN,    // New open order — full Order object included.
    CANCEL,  // Order cancelled — only order.id is meaningful.
    FILL,    // Order fully filled — only order.id is meaningful.
};

struct OrderEvent {
    OrderEventType type  = OrderEventType::OPEN;
    Order          order;  // OPEN: full data.  CANCEL/FILL: only .id used.
};

// ─────────────────────────────────────────────────────────────────────────────
// Exchange order filter — precision rules and min/max constraints loaded from
// GET /api/v4/spot/currency_pairs/{pair} at connect time.
//
// round()    — formats price and amount to the decimal places Gate.io requires.
//              Sending too many decimal places causes a INVALID_PARAM rejection.
// validate() — throws if the order violates Gate.io's min/max size rules.
//              Only enforced when loaded == true (REST fetch succeeded).
// ─────────────────────────────────────────────────────────────────────────────

struct GateOrderFilter {
    int    price_precision  = 8;    // decimal places for price strings
    int    amount_precision = 8;    // decimal places for amount strings
    double min_base_amount  = 0.0;  // minimum token amount per order
    double max_base_amount  = 1e15; // maximum token amount (null → no limit)
    double min_quote_amount = 0.0;  // minimum notional value (price × amount)
    double max_quote_amount = 1e15; // maximum notional value
    bool   loaded           = false;

    // Returns (price_str, amount_str) rounded to the exchange-mandated precision.
    std::pair<std::string, std::string> round(double price, double amount) const {
        std::ostringstream ps, as;
        ps << std::fixed << std::setprecision(price_precision)  << price;
        as << std::fixed << std::setprecision(amount_precision) << amount;
        return {ps.str(), as.str()};
    }

    // Throws std::runtime_error if the order is outside allowed size bounds.
    // No-op when loaded == false (filter not yet fetched — safe defaults apply).
    void validate(double amount, double price) const {
        if (!loaded) return;
        if (amount < min_base_amount)
            throw std::runtime_error(
                "create_limit_order: amount " + std::to_string(amount) +
                " is below Gate.io min_base_amount " + std::to_string(min_base_amount));
        if (amount > max_base_amount)
            throw std::runtime_error(
                "create_limit_order: amount " + std::to_string(amount) +
                " exceeds Gate.io max_base_amount " + std::to_string(max_base_amount));
        double quote = price * amount;
        if (quote < min_quote_amount)
            throw std::runtime_error(
                "create_limit_order: notional " + std::to_string(quote) +
                " is below Gate.io min_quote_amount " + std::to_string(min_quote_amount));
    }
};

// ─────────────────────────────────────────────────────────────────────────────
// GateConnector
// ─────────────────────────────────────────────────────────────────────────────

class GateConnector : public BaseConnector {
public:
    GateConnector(
        const std::string& symbol,
        const std::string& api_key,
        const std::string& api_secret,
        const std::string& quote_currency = "USDT"
    );

    ~GateConnector() override;

    // ── Lifecycle ─────────────────────────────────────────────────────────────
    void connect()    override;
    void disconnect() override;

    // ── Market data ───────────────────────────────────────────────────────────
    Ticker              fetch_ticker()                                       override;
    std::vector<Candle> fetch_candles(const std::string& timeframe = "1m",
                                      int limit = 15)                        override;

    // ── Account ───────────────────────────────────────────────────────────────
    Balance fetch_balance() override;

    // ── Order management ──────────────────────────────────────────────────────
    Order              create_limit_order(const std::string& side,
                                          double price,
                                          double amount)                     override;
    void               cancel_order(const std::string& order_id)            override;
    void               cancel_all_orders()                                   override;
    std::vector<Order> fetch_open_orders()                                   override;
    std::vector<Fill>  fetch_fills(double since_ts = -1.0, int limit = 100) override;

protected:
    // ── Credentials (constant after construction) ─────────────────────────────
    const std::string api_key_;
    const std::string api_secret_;
    const std::string quote_currency_;
    std::string       base_token_;    // e.g. "ALKIMI"
    std::string       gate_symbol_;   // e.g. "ALKIMI_USDT"

    // ── Lock-free market data ─────────────────────────────────────────────────
    // Written by io_thread on every WebSocket message.
    // Read by the caller thread on every fetch_ticker() / fetch_balance() call.
    // Zero blocking on the read path.
    //
    // Ticker  → SeqlockBuffer: always returns the absolute latest value.
    //           Best choice for data that is read every tick and written on every
    //           WS price push (reader must always see the freshest bid/ask).
    //
    // Balance → AtomicDoubleBuffer: reader never spins (zero retry loop).
    //           Best choice for data that is written and read infrequently —
    //           "latest complete write" semantics are fine for balance checks.
    SeqlockBuffer<Ticker>          ticker_sl_;
    AtomicDoubleBuffer<BalanceData> balance_adb_;

    // ── Readiness flags ───────────────────────────────────────────────────────
    // ticker_ready_ / balance_ready_: set after REST bootstrap; never cleared.
    // ws_ready_: set after the WebSocket opens and ws_ptr_ is assigned.
    //            Cleared before each reconnect so connect() stalls correctly
    //            if called again, and so ws_api_request gets a clean "not
    //            connected" error during the reconnect window instead of
    //            using a stale pointer.
    std::atomic<bool> ticker_ready_{false};
    std::atomic<bool> balance_ready_{false};
    std::atomic<bool> ws_ready_{false};

    // ── Startup synchronisation ───────────────────────────────────────────────
    // Only used inside connect() for the one-time wait.  Not on any hot path.
    std::mutex              connect_mutex_;
    std::condition_variable cache_cv_;

    // ── SPSC order event queue ────────────────────────────────────────────────
    // Producer : io_thread  (pushes OPEN / CANCEL / FILL events)
    // Consumer : fetch_open_orders() caller
    // 512 slots handles any plausible burst of order events between 1-s ticks.
    SpscQueue<OrderEvent, 512> order_queue_;

    // Consumer-side order map.
    // ONLY touched by whoever calls fetch_open_orders().
    // Never shared with io_thread — no lock needed.
    std::map<std::string, Order> consumer_orders_;

    // ── Order rate limiter ────────────────────────────────────────────────────
    // Enforces Gate.io's 10 order-operations/second limit for create_limit_order
    // and cancel_order.  Applies exponential backoff on RATE_LIMIT responses.
    RateLimiter order_rate_limiter_{10};

    // ── Order precision filter ────────────────────────────────────────────────
    // Loaded from REST at connect time (GET /api/v4/spot/currency_pairs/{pair}).
    // Applied in create_limit_order to round price/amount and reject orders that
    // violate Gate.io's min/max size rules before they are even sent.
    GateOrderFilter order_filter_;

    // ── REST serialisation ────────────────────────────────────────────────────
    // Serialises the REST bootstrap calls and any remaining REST-only methods
    // (fetch_candles, fetch_fills, cancel_all_orders).
    std::mutex rest_mutex_;

    // ── WS order send ─────────────────────────────────────────────────────────
    // ws_ptr_ is the active WsClient owned by io_thread.  Set after ws.connect(),
    // cleared before reconnect.  Held only for the actual send_text() call.
    std::mutex   ws_send_mutex_;
    WsClient*    ws_ptr_ = nullptr;

    // Pending WS API requests: req_id → promise fulfilled by on_ws_message.
    std::mutex                                          pending_mutex_;
    std::map<std::string, std::promise<nlohmann::json>> pending_requests_;
    std::atomic<uint64_t>                               req_id_counter_{0};

    // ── Connection control ────────────────────────────────────────────────────
    std::atomic<bool> running_{false};
    std::thread       io_thread_;

    // WS heartbeat interval (ms).  Gate.io closes idle connections after ~60 s.
    int ping_interval_ms_ = 30000;

    // ── REST auth helpers ─────────────────────────────────────────────────────
    std::map<std::string, std::string> make_auth_headers(
        const std::string& method,
        const std::string& path,
        const std::string& query,
        const std::string& body) const;

    // Per-channel WS subscription signature: event=subscribe.
    std::string ws_sign      (const std::string& channel, long long ts) const;
    // spot.login signature: HMAC-SHA512(secret, "api\nspot.login\n\n{ts}")
    std::string ws_sign_login(long long ts) const;

    // Authenticate the WS session (spot.login).  Must be called once after
    // the socket opens.  Throws if the login fails or times out.
    void ws_login();

    // Send a WS API request and block until the response arrives (or timeout).
    // The WS session must already be authenticated via ws_login().
    nlohmann::json ws_api_request(const std::string&     channel,
                                  const nlohmann::json&  req_param,
                                  int                    timeout_ms = 5000);

    // Fail all in-flight WS API requests with the given reason.
    // Called when the WebSocket disconnects or reconnects.
    void fail_pending_requests(const std::string& reason);

    // ── Low-level REST ────────────────────────────────────────────────────────
    nlohmann::json rest_get   (const std::string& path, const std::string& query = "");
    nlohmann::json rest_post  (const std::string& path, const nlohmann::json& body);
    nlohmann::json rest_delete(const std::string& path, const std::string& query = "");

    // ── IO thread ─────────────────────────────────────────────────────────────
    void io_thread_main();

    // ── WS message handlers (called from io_thread) ───────────────────────────
    void on_ws_message (const std::string& raw);
    void on_ticker_msg (const nlohmann::json& result);  // writes ticker_sl_
    void on_order_msg  (const nlohmann::json& result);  // pushes to order_queue_
    void on_balance_msg(const nlohmann::json& result);  // writes balance_adb_

    // ── Conversion helpers ────────────────────────────────────────────────────
    Order json_to_order(const nlohmann::json& j) const;
    Fill  json_to_fill (const nlohmann::json& j) const;

    static std::string map_timeframe(const std::string& tf);
};
