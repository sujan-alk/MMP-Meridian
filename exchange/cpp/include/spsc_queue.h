#pragma once

/**
 * spsc_queue.h  —  Single-Producer / Single-Consumer lock-free ring buffer.
 *
 * Background
 * ──────────
 * A mutex queue blocks one thread while the other holds the lock.
 * This queue uses two atomic integers (write_pos_ and read_pos_) and a
 * fixed-size array — no OS calls, no blocking, ever.
 *
 * How the ring buffer works
 * ─────────────────────────
 *   buf_[0..N-1]  fixed array, allocated once at construction
 *   write_pos_    index of the next slot the producer will write into
 *   read_pos_     index of the next slot the consumer will read from
 *
 *   Empty : write_pos_ == read_pos_
 *   Full  : (write_pos_ + 1) % N == read_pos_   (one slot always kept empty)
 *
 *   On push(item):
 *     1. Producer reads write_pos_ (relaxed — only producer writes it)
 *     2. Computes next = (write_pos_ + 1) & mask
 *     3. If next == read_pos_ (acquire) → full, return false
 *     4. buf_[write_pos_] = item
 *     5. write_pos_.store(next, release)  ← "item is ready to consume"
 *
 *   On pop(out):
 *     1. Consumer reads read_pos_ (relaxed — only consumer writes it)
 *     2. If read_pos_ == write_pos_ (acquire) → empty, return false
 *     3. out = buf_[read_pos_]          ← safe: write_pos_ acquire above
 *     4. read_pos_.store(next, release) ← "slot is free to reuse"
 *
 * Memory ordering
 * ───────────────
 *   release on write_pos_.store  → consumer's acquire-load of write_pos_
 *   ensures buf_[w] is fully written before the consumer can see the new index.
 *
 *   release on read_pos_.store   → producer's acquire-load of read_pos_
 *   ensures the consumer is truly done with the slot before the producer
 *   overwrites it.
 *
 * Cache-line separation
 * ─────────────────────
 *   write_pos_ is written by the producer, read_pos_ by the consumer.
 *   Placing them on the same 64-byte cache line would cause "false sharing":
 *   every push invalidates the consumer's cache line (and vice versa), adding
 *   ~60–200 ns of latency per operation.  alignas(64) on each fixes this.
 *
 * Constraints
 * ───────────
 *   • N must be a power of 2 (static_assert enforced).
 *   • Exactly ONE producer thread calls push().
 *   • Exactly ONE consumer thread calls pop().
 *   • If the queue is full, push() drops the item and returns false.
 *     Size N=512 is enough for any burst of order events between 1-second ticks.
 */

#include <array>
#include <atomic>
#include <cstddef>
#include <type_traits>

template<typename T, std::size_t N>
class SpscQueue {
    static_assert((N & (N - 1)) == 0,
        "SpscQueue: N must be a power of 2 (e.g. 64, 128, 256, 512, 1024).");
    static_assert(N >= 2,
        "SpscQueue: N must be at least 2.");

public:
    SpscQueue()  = default;
    ~SpscQueue() = default;

    SpscQueue(const SpscQueue&)            = delete;
    SpscQueue& operator=(const SpscQueue&) = delete;

    // ── Producer side ────────────────────────────────────────────────────────
    // Call from exactly one thread.  Returns false if the queue is full.
    bool push(const T& item) noexcept(std::is_nothrow_copy_assignable_v<T>) {
        const std::size_t w    = write_pos_.load(std::memory_order_relaxed);
        const std::size_t next = (w + 1u) & kMask;

        // If next write position == current read position → full.
        // acquire: ensures we see the latest read_pos_ written by the consumer.
        if (next == read_pos_.load(std::memory_order_acquire))
            return false;

        buf_[w] = item;

        // release: makes buf_[w] visible to the consumer before write_pos_
        // advances past w.  Without this, the consumer could read a slot
        // before its contents are committed.
        write_pos_.store(next, std::memory_order_release);
        return true;
    }

    // ── Consumer side ────────────────────────────────────────────────────────
    // Call from exactly one thread.  Returns false if the queue is empty.
    bool pop(T& out) noexcept(std::is_nothrow_copy_assignable_v<T>) {
        const std::size_t r = read_pos_.load(std::memory_order_relaxed);

        // If read position == current write position → empty.
        // acquire: synchronises with producer's release store on write_pos_,
        // guaranteeing buf_[r] is fully written before we read it.
        if (r == write_pos_.load(std::memory_order_acquire))
            return false;

        out = buf_[r];

        // release: makes the slot reusable by the producer.
        read_pos_.store((r + 1u) & kMask, std::memory_order_release);
        return true;
    }

    // How many items are approximately in the queue.
    // "Approximate" because the two atomics are read non-atomically together —
    // safe for monitoring/logging, not for correctness decisions.
    std::size_t size_approx() const noexcept {
        const std::size_t w = write_pos_.load(std::memory_order_relaxed);
        const std::size_t r = read_pos_.load(std::memory_order_relaxed);
        return (w - r) & kMask;
    }

    bool empty() const noexcept {
        return write_pos_.load(std::memory_order_relaxed) ==
               read_pos_.load(std::memory_order_relaxed);
    }

    static constexpr std::size_t capacity() noexcept { return N - 1; }

private:
    static constexpr std::size_t kMask = N - 1u;

    // Producer writes write_pos_, reads read_pos_.
    // Consumer writes read_pos_,  reads write_pos_.
    // Separate cache lines eliminate false sharing between the two cores.
    alignas(64) std::atomic<std::size_t> write_pos_{0};
    alignas(64) std::atomic<std::size_t> read_pos_{0};

    // The actual ring buffer.  Aligned to avoid straddling cache lines.
    alignas(64) std::array<T, N> buf_{};
};
