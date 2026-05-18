#pragma once

/**
 * seqlock.h  —  Single-writer / multiple-reader lock-free buffer.
 *
 * How it works
 * ────────────
 * The writer keeps a 32-bit counter (seq_) that obeys one rule:
 *   odd  = write in progress
 *   even = data is consistent and safe to read
 *
 * Writer protocol:
 *   1. seq_++  (0→1, even→odd:  "I'm writing, readers must retry")
 *   2. release fence  (prevents the CPU reordering the data write before step 1)
 *   3. data_ = new value
 *   4. release fence  (prevents step 5 being reordered before the data write)
 *   5. seq_++  (1→2, odd→even:  "done, data is consistent")
 *
 * Reader protocol:
 *   1. s1 = seq_.load(acquire)   — if odd, writer is active → spin
 *   2. copy = data_
 *   3. acquire fence
 *   4. s2 = seq_.load(relaxed)
 *   5. if s1 == s2: return copy  — no write happened during our read
 *      else: retry from step 1   — we caught a write mid-flight
 *
 * Why no OS lock:
 *   The critical section is a handful of CPU instructions.  A mutex would
 *   involve a syscall (futex) that costs thousands of ns.  The seqlock's
 *   worst-case retry is a tight spin of ~5–20 ns.
 *
 * Cache-line layout:
 *   seq_ is placed on its own 64-byte cache line.  Without this, every
 *   writer increment would invalidate the cache line that also holds data_,
 *   forcing readers on other cores to fetch it again even if data_ did not
 *   change.  Separating them means readers can keep data_ warm in L1 cache.
 *
 * Constraints:
 *   • T must be std::is_trivially_copyable (enforced by static_assert).
 *     std::string, std::vector, etc. are NOT allowed — use a POD struct.
 *   • Exactly ONE writer thread.  Multiple writers would corrupt seq_.
 *   • Any number of reader threads.
 */

#include <atomic>
#include <cstdint>
#include <type_traits>

template<typename T>
class SeqlockBuffer {
    static_assert(std::is_trivially_copyable_v<T>,
        "SeqlockBuffer<T>: T must be trivially copyable. "
        "Strip std::string / std::vector members into a POD struct.");

public:
    SeqlockBuffer() = default;
    explicit SeqlockBuffer(const T& init) : data_(init) {}

    // ── Writer side ──────────────────────────────────────────────────────────
    // Must be called from exactly one thread.  Non-blocking.
    void store(const T& val) noexcept {
        // Step 1 — mark "write in progress" (seq becomes odd).
        // relaxed: the fence below establishes the ordering.
        seq_.fetch_add(1u, std::memory_order_relaxed);

        // Step 2 — release fence.
        // Everything before this point (including the seq_ store above) is
        // ordered before everything after it.  Prevents the compiler or CPU
        // from sinking the seq_ store past the data write.
        std::atomic_thread_fence(std::memory_order_release);

        // Step 3 — write the data.
        data_ = val;

        // Step 4 — release fence.
        // Prevents the compiler or CPU from hoisting the next seq_ store
        // before the data write completes.
        std::atomic_thread_fence(std::memory_order_release);

        // Step 5 — mark "write complete" (seq becomes even again).
        seq_.fetch_add(1u, std::memory_order_relaxed);
    }

    // ── Reader side ──────────────────────────────────────────────────────────
    // Safe to call from any number of threads concurrently.  Never blocks;
    // retries in a tight loop (< 20 ns) if a write is in progress.
    T load() const noexcept {
        T out;
        uint32_t s1, s2;
        do {
            // Acquire load: synchronises with the writer's release fence,
            // so we are guaranteed to see all stores the writer made before
            // its even seq_ store.
            s1 = seq_.load(std::memory_order_acquire);

            // If seq is odd the writer is mid-update — spin without reading.
            if (s1 & 1u) continue;

            // Read the payload.
            out = data_;

            // Acquire fence: prevents the CPU from reordering the data_ read
            // after the seq_ re-read below (which would defeat the check).
            std::atomic_thread_fence(std::memory_order_acquire);

            // Re-read the sequence.  If it changed, a write started (and
            // possibly completed) while we were reading — our copy may be
            // a torn mix of old and new values, so we retry.
            s2 = seq_.load(std::memory_order_relaxed);
        } while (s1 != s2);

        return out;
    }

    // Returns true if a write is currently in progress (seq is odd).
    // Useful for diagnostics / monitoring — not required for correctness.
    bool is_writing() const noexcept {
        return seq_.load(std::memory_order_relaxed) & 1u;
    }

private:
    // seq_ on its own 64-byte cache line.
    // Writers touch this on every update; keeping it isolated stops writer
    // activity from evicting data_ from reader CPU caches.
    alignas(64) std::atomic<uint32_t> seq_{0};

    // data_ follows on the next cache line.
    // Readers keep this warm in L1; the writer only touches it briefly.
    alignas(64) T data_{};
};
