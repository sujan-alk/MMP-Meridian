#pragma once

/**
 * kucoin_connector.h — Full KuCoin C++ connector.
 *
 * Architecture:
 *   connect()     → fetches WS token via REST, starts background io_thread_
 *   io_thread_    → maintains a persistent WSS connection to KuCoin
 *                   subscribes to ticker, order, balance channels
 *                   keeps in-memory cache updated
 *                   sends heartbeat ping every ~18s (server-specified interval)
 *                   auto-reconnects on disconnect
 *   fetch_ticker() / fetch_balance() / fetch_open_orders()
 *                 → read from mutex-protected cache (no network call, <1ms)
 *   create_limit_order / cancel_order / etc.
 *                 → blocking HTTPS REST calls (from caller's thread)
 *
 * Threading:
 *   state_mutex_  → guards latest_ticker_, latest_balance_, open_orders_
 *   cache_cv_     → notified when ticker_ready_ && balance_ready_ flip to true
 *   rest_mutex_   → serialises concurrent REST calls (order mgmt from strategy)
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
// net_utils.h included by .cpp — not needed in the header

class KuCoinConnector : public BaseConnector {
public:
    KuCoinConnector(
        const std::string& symbol,
        const std::string& api_key,
        const std::string& api_secret,
        const std::string& passphrase,
        const std::string& quote_currency = "USDT"
    );

    ~KuCoinConnector() override;

    // Lifecycle -----------------------------------------------------------------
    void connect()    override;
    void disconnect() override;

    // Market data ---------------------------------------------------------------
    Ticker              fetch_ticker() override;
    std::vector<Candle> fetch_candles(const std::string& timeframe = "1m",
                                      int limit = 15) override;

    // Account -------------------------------------------------------------------
    Balance fetch_balance() override;

    // Order management ----------------------------------------------------------
    Order              create_limit_order(const std::string& side,
                                          double price,
                                          double amount) override;
    void               cancel_order(const std::string& order_id) override;
    void               cancel_all_orders() override;
    std::vector<Order> fetch_open_orders() override;
    std::vector<Fill>  fetch_fills(double since_ts = -1.0, int limit = 100) override;

protected:
    // Credentials
    std::string api_key_;
    std::string api_secret_;
    std::string passphrase_;
    std::string quote_currency_;
    std::string base_token_;    // parsed from symbol ("ALKIMI" from "ALKIMI-USDT")

    // Thread-safe cache
    mutable std::mutex      state_mutex_;
    std::condition_variable cache_cv_;
    Ticker                  latest_ticker_{};
    Balance                 latest_balance_{};
    std::map<std::string, Order> open_orders_{};
    bool ticker_ready_  = false;
    bool balance_ready_ = false;

    // REST serialisation
    std::mutex rest_mutex_;

    // Connection control
    std::atomic<bool> running_{false};
    std::thread       io_thread_;

    // WS connection parameters (from POST /api/v1/bullet-private)
    std::string ws_token_;
    std::string ws_host_;
    std::string ws_path_;
    int         ws_ping_interval_ms_ = 18000;

    // REST auth helpers
    std::map<std::string, std::string> make_auth_headers(
        const std::string& method,
        const std::string& path,
        const std::string& query,
        const std::string& body) const;

    // Low-level REST calls (new TLS connection per call)
    nlohmann::json rest_get(const std::string& path,
                            const std::string& query = "");
    nlohmann::json rest_post(const std::string& path,
                             const nlohmann::json& body);
    nlohmann::json rest_delete(const std::string& path,
                               const std::string& query = "");

    // Fetch WS token + endpoint from REST, set ws_host_/ws_path_/ws_ping_interval_ms_
    void fetch_ws_credentials();

    // IO thread entry point; loops (connect → stream → reconnect)
    void io_thread_main();

    // WS message dispatch
    void on_ws_message(const std::string& raw);
    void on_ticker_msg(const nlohmann::json& data);
    void on_order_msg(const nlohmann::json& data);
    void on_balance_msg(const nlohmann::json& data);

    // Conversion helpers (REST JSON → structs)
    Order json_to_order(const nlohmann::json& j) const;
    Fill  json_to_fill(const nlohmann::json& j)  const;

    // Candle timeframe string mapping (Python "1m" → KuCoin "1min")
    static std::string map_timeframe(const std::string& tf);
};
