#pragma once

#include <chrono>
#include <mutex>
#include <thread>

/**
 * Token-bucket rate limiter with exponential backoff on rate-limit errors.
 *
 * acquire()           — blocks until a send token is available.  Call this
 *                       before every order placement / cancellation.
 * on_rate_limit_hit() — called when the exchange returns a RATE_LIMIT/429
 *                       error.  Doubles the backoff period (1 s → 2 s → … → 32 s).
 *                       Drains the bucket so no further sends happen during backoff.
 * on_success()        — called on a successful response.  Resets backoff to
 *                       the initial 1 s so the next rate-limit hit starts fresh.
 *
 * Thread-safe: all methods may be called from any thread simultaneously.
 *
 * Gate.io spot order-placement limit: 10 operations / second.
 * Default construction uses max_per_second = 10 to match that limit exactly.
 */
class RateLimiter {
public:
    explicit RateLimiter(int max_per_second = 10)
        : tokens_(max_per_second)
        , max_tokens_(max_per_second)
        , refill_ns_(1'000'000'000LL / max_per_second)
        , last_refill_(std::chrono::steady_clock::now())
        , backoff_until_(std::chrono::steady_clock::time_point::min())
    {}

    // Block until one token is available, then consume it.
    // Sleeps in 10 ms increments — only runs the loop when the bucket is empty
    // (normal market-making never hits this with 1 cancel + 1 place per second).
    void acquire() {
        std::unique_lock<std::mutex> lk(mu_);
        for (;;) {
            refill_locked();
            if (tokens_ >= 1) { --tokens_; return; }
            lk.unlock();
            std::this_thread::sleep_for(std::chrono::milliseconds(10));
            lk.lock();
        }
    }

    // Called when the exchange returns a rate-limit error (status 429 / label
    // RATE_LIMIT).  Doubles the current backoff up to 32 s and drains the bucket.
    void on_rate_limit_hit() {
        std::lock_guard<std::mutex> lk(mu_);
        backoff_ms_ = std::min(backoff_ms_ * 2, kMaxBackoffMs);
        backoff_until_ = std::chrono::steady_clock::now()
                       + std::chrono::milliseconds(backoff_ms_);
        tokens_ = 0;
    }

    // Called after any successful order response.  Resets backoff to initial
    // so the next error starts the doubling sequence from 1 s again.
    void on_success() {
        std::lock_guard<std::mutex> lk(mu_);
        backoff_ms_ = kInitBackoffMs;
    }

private:
    static constexpr int kInitBackoffMs = 1000;   // 1 s initial backoff
    static constexpr int kMaxBackoffMs  = 32000;  // 32 s cap

    // Must be called with mu_ held.
    // During backoff: drains tokens to 0 so acquire() keeps waiting.
    // After backoff: computes how many tokens accrued since last_refill_ and
    // adds them (capped at max_tokens_).
    void refill_locked() {
        auto now = std::chrono::steady_clock::now();
        if (now < backoff_until_) { tokens_ = 0; return; }

        auto elapsed_ns = std::chrono::duration_cast<std::chrono::nanoseconds>(
            now - last_refill_).count();
        int new_toks = static_cast<int>(elapsed_ns / refill_ns_);
        if (new_toks > 0) {
            tokens_      = std::min(tokens_ + new_toks, max_tokens_);
            last_refill_ = now;
        }
    }

    std::mutex mu_;
    int        tokens_;
    const int        max_tokens_;
    const long long  refill_ns_;                              // ns per token
    std::chrono::steady_clock::time_point last_refill_;
    std::chrono::steady_clock::time_point backoff_until_;
    int        backoff_ms_ = kInitBackoffMs;
};
