#pragma once

/**
 * kraken_connector.h — Full Kraken v2 WebSocket C++ connector.
 *
 * Key differences from KuCoin / Gate.io / MEXC:
 *   - TWO WebSocket connections:
 *       public  → wss://ws.kraken.com/v2        (ticker)
 *       private → wss://ws-auth.kraken.com/v2   (executions, balances)
 *   - WS auth: REST token from POST /0/private/GetWebSocketsToken,
 *     embedded in private channel subscribe params (not in URL or header)
 *   - REST auth: HMAC-SHA512 (2-step: SHA256 of nonce+body, then HMAC of path+hash)
 *     Headers: API-Key, API-Sign, Content-Type: application/x-www-form-urlencoded
 *     api_secret is base64-encoded (decoded internally before HMAC)
 *   - REST calls (even Balance, OpenOrders) use POST, not GET
 *   - Symbol: WS uses "ALKIMI/USD" (slash), REST uses "ALKIMIUSD" (no separator)
 *   - quote_currency default: "USD" (not USDT — Kraken has no USDT pair for ALKIMI)
 *   - Balance REST keys: "ALKIMI" for token, "ZUSD" or "USD" for USD
 *   - REST OHLC is public GET /0/public/OHLC (no auth); interval is minutes as integer
 *   - cancel_all_orders uses REST POST /0/private/CancelAll
 *   - Heartbeat: JSON {"method":"ping"} to both WS connections every 30 s
 */

#include "connector.h"
#include <atomic>
#include <condition_variable>
#include <map>
#include <mutex>
#include <string>
#include <thread>
#include <vector>
#include <nlohmann/json.hpp>

class KrakenConnector : public BaseConnector {
public:
    KrakenConnector(
        const std::string& symbol,
        const std::string& api_key,
        const std::string& api_secret,      // base64-encoded (as issued by Kraken)
        const std::string& quote_currency = "USD"
    );

    ~KrakenConnector() override;

    // Lifecycle
    void connect()    override;
    void disconnect() override;

    // Market data
    Ticker              fetch_ticker()                                        override;
    std::vector<Candle> fetch_candles(const std::string& timeframe = "1m",
                                      int limit = 15)                        override;

    // Account
    Balance fetch_balance() override;

    // Order management
    Order              create_limit_order(const std::string& side,
                                          double price,
                                          double amount)                     override;
    void               cancel_order(const std::string& order_id)            override;
    void               cancel_all_orders()                                   override;
    std::vector<Order> fetch_open_orders()                                   override;
    std::vector<Fill>  fetch_fills(double since_ts = -1.0, int limit = 100) override;

protected:
    // Credentials
    std::string api_key_;
    std::string api_secret_;        // base64-encoded; decoded inside kraken_sign()
    std::string quote_currency_;
    std::string base_token_;        // "ALKIMI"
    std::string kraken_pair_;       // WS v2 symbol:  "ALKIMI/USD"
    std::string kraken_rest_pair_;  // REST pair name: "ALKIMIUSD"

    // Thread-safe cache
    mutable std::mutex       state_mutex_;
    std::condition_variable  cache_cv_;
    Ticker                   latest_ticker_{};
    Balance                  latest_balance_{};
    std::map<std::string, Order> open_orders_{};
    bool ticker_ready_  = false;
    bool balance_ready_ = false;

    // REST serialisation (order management calls only)
    std::mutex rest_mutex_;

    // Connection control
    std::atomic<bool> running_{false};
    std::thread       io_thread_;

    // WS auth token (from /0/private/GetWebSocketsToken, valid ~15 min)
    std::string ws_token_;

    static constexpr int PING_INTERVAL_MS = 30000; // 30 s

    // REST auth helpers
    // Returns {API-Key, API-Sign, Content-Type} headers for a private POST.
    std::map<std::string, std::string> make_auth_headers(
        const std::string& path,
        const std::string& nonce,
        const std::string& body) const;

    // Authenticated REST POST (holds rest_mutex_).
    // extra_body: params without nonce, e.g. "type=buy&volume=1000&pair=ALKIMIUSD&price=0.001"
    nlohmann::json rest_post_private(const std::string& path,
                                     const std::string& extra_body = "");

    // Public REST GET (no auth, no rest_mutex_ — only used for candles).
    nlohmann::json rest_get_public(const std::string& path_and_query);

    // Fetch WS auth token via REST.
    std::string fetch_ws_token();

    // IO thread (runs public + private WS loop).
    void io_thread_main();

    // WS message dispatch (called for both public and private WS messages).
    void on_ws_message(const std::string& raw);
    void on_ticker_msg(const nlohmann::json& data);
    void on_execution_msg(const nlohmann::json& data);
    void on_balance_msg(const nlohmann::json& data);

    // Conversion helpers — REST JSON → structs.
    // txid is the map key from OpenOrders["open"], trade_id from TradesHistory["trades"].
    Order json_to_order_rest(const std::string& txid,
                              const nlohmann::json& j) const;
    Fill  json_to_fill_rest(const std::string& trade_id,
                             const nlohmann::json& j)  const;

    // Timeframe mapping: "1m"→"1", "1h"→"60", "1d"→"1440", "1w"→"10080"
    static std::string map_timeframe(const std::string& tf);
};
