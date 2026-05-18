#pragma once

/**
 * mexc_connector.h — Full MEXC v3 C++ connector.
 *
 * Key differences from KuCoin / Gate.io:
 *   - Symbol format: no separator  "ALKIMIUSDT" (uppercase, no /, -, _)
 *   - REST auth: all params in query string including timestamp;
 *     signature = hex(HMAC-SHA256(secret, full_query_string));
 *     header: X-MEXC-APIKEY: <api_key>
 *   - Private WS: requires listenKey (POST /api/v3/userDataStream);
 *     renewed every 30 min with PUT /api/v3/userDataStream
 *   - WS message format: {"c":"channel","d":{...},"t":ts}
 *   - WS ticker channel:  spot@public.miniTicker.v3.api@ALKIMIUSDT
 *   - WS order channel:   spot@private.orders.v3.api@<listenKey>
 *   - WS account channel: spot@private.account.v3.api@<listenKey>
 *   - WS heartbeat: JSON {"method":"PING"} every 20s
 *   - Candle intervals use "60m" for 1h, "1W" for 1w
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

class MexcConnector : public BaseConnector {
public:
    MexcConnector(
        const std::string& symbol,
        const std::string& api_key,
        const std::string& api_secret,
        const std::string& quote_currency = "USDT"
    );

    ~MexcConnector() override;

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
    std::string api_secret_;
    std::string quote_currency_;
    std::string base_token_;    // "ALKIMI" from "ALKIMIUSDT"
    std::string mexc_symbol_;   // uppercase no-separator form, e.g. "ALKIMIUSDT"

    // Thread-safe cache
    mutable std::mutex       state_mutex_;
    std::condition_variable  cache_cv_;
    Ticker                   latest_ticker_{};
    Balance                  latest_balance_{};
    std::map<std::string, Order> open_orders_{};
    bool ticker_ready_  = false;
    bool balance_ready_ = false;

    // REST serialisation
    std::mutex rest_mutex_;

    // Connection control
    std::atomic<bool> running_{false};
    std::thread       io_thread_;

    // Private WS stream key (valid 30 min; refreshed every 29 min)
    std::string listen_key_;

    // Heartbeat interval (ms)
    static constexpr int PING_INTERVAL_MS    = 20000;  // 20 s
    static constexpr int LK_REFRESH_MS       = 29 * 60 * 1000; // 29 min

    // REST auth helpers
    // Returns base_query + "&timestamp=<ms>&signature=<hex-hmac>"
    std::string make_auth_query(const std::string& base_query) const;

    // Returns {"X-MEXC-APIKEY": api_key_, "Content-Type": "application/json"}
    std::map<std::string, std::string> make_api_key_header() const;

    // listenKey lifecycle
    std::string fetch_listen_key();    // POST /api/v3/userDataStream
    void        refresh_listen_key();  // PUT  /api/v3/userDataStream

    // Low-level REST (hold rest_mutex_ internally)
    nlohmann::json rest_get(const std::string& path, const std::string& query = "");
    nlohmann::json rest_post_form(const std::string& path, const std::string& query);
    nlohmann::json rest_delete(const std::string& path, const std::string& query = "");

    // IO thread
    void io_thread_main();

    // WS message dispatch
    void on_ws_message(const std::string& raw);
    void on_ticker_msg(const nlohmann::json& d);
    void on_order_msg(const nlohmann::json& d);
    void on_balance_msg(const nlohmann::json& d);

    // Conversion helpers
    Order json_to_order(const nlohmann::json& j) const;
    Fill  json_to_fill(const nlohmann::json& j)  const;

    // Timeframe mapping ("1h" → "60m", "1w" → "1W", etc.)
    static std::string map_timeframe(const std::string& tf);
};
