#pragma once

/**
 * atomic_double_buffer.h  —  Lock-free triple-slot publish/subscribe buffer.
 *
 * Named "double buffer" because the interface is conceptually double-sided
 * (writer publishes, reader consumes) but internally uses 3 slots to avoid
 * the race condition that makes a naive 2-slot version unsafe.
 *
 * Why 2 slots are not enough
 * ──────────────────────────
 * With only 2 slots and one atomic index, the following race is possible:
 *
 *   active = 0  (reader is about to read buf[0])
 *   ① Writer writes buf[1], stores active = 1.
 *   ② Writer immediately starts the next write: considers buf[0] inactive,
 *      begins writing buf[0].
 *   ③ Reader (which loaded active = 0 back in step ①) reads buf[0].
 *      → Writer and reader are on buf[0] simultaneously → torn read.
 *
 * Why 3 slots are enough
 * ──────────────────────
 * Each slot is exclusively owned by one party at all times:
 *
 *   writer_slot_  — writer owns it exclusively; no reader ever touches it.
 *   reader_slot_  — reader owns it exclusively; no writer ever touches it.
 *   shared slot   — the "handoff zone", protected by one atomic exchange.
 *
 * Writer lifecycle:
 *   1. Fill writer_slot_ completely (no reader can see it yet).
 *   2. Atomic exchange: hand off writer_slot_ as the new shared slot,
 *      take back the old shared slot for the next write.
 *      Set bit 2 of the packed atomic to signal "new data available".
 *
 * Reader lifecycle:
 *   1. Check if bit 2 is set (new data available).
 *   2. If yes: atomic exchange — hand off reader_slot_ as the new shared slot,
 *      take back the slot that just arrived.  Clear bit 2.
 *   3. Read from reader_slot_ (now holds the latest complete write).
 *   4. If no new data: read from reader_slot_ again (same as last time).
 *
 * Because writer_slot_ and reader_slot_ are always exclusive, and the shared
 * slot is only touched during a single indivisible atomic exchange, writer and
 * reader can never be on the same slot at the same time.
 *
 * Comparison with seqlock
 * ───────────────────────
 *   Seqlock          : 1 copy of data, reader spins ~10 ns if write is in progress.
 *   AtomicDoubleBuffer: 3 copies of data, reader NEVER spins — zero retry loop.
 *                       Reader may get a value that is one write behind if no new
 *                       data has been published since the last load().
 *
 * When to choose AtomicDoubleBuffer over SeqlockBuffer:
 *   • Data written thousands of times per second (high contention seqlock spin).
 *   • Reader is in a hard real-time context where even a 10 ns spin is forbidden.
 *   • You can accept "latest complete write" semantics (reader never sees torn data
 *     but may not see the absolute latest write if it arrived between two load() calls).
 *
 * When to keep SeqlockBuffer:
 *   • Data written rarely (ticker updates, balance changes) — seqlock is fine.
 *   • Memory is tight — seqlock uses 1 copy, this uses 3.
 *   • You always want the single most recent value (seqlock always returns latest).
 *
 * Constraints
 * ───────────
 *   • T must be trivially copyable (same as SeqlockBuffer).
 *   • Exactly ONE writer thread calls store().
 *   • Exactly ONE reader thread calls load().
 *   • writer_slot_ and reader_slot_ are plain ints — not atomic — because they
 *     are only ever accessed by their respective owner thread.
 */

#include <atomic>
#include <cstdint>
#include <type_traits>

template<typename T>
class AtomicDoubleBuffer {
    static_assert(std::is_trivially_copyable_v<T>,
        "AtomicDoubleBuffer<T>: T must be trivially copyable.");

public:
    AtomicDoubleBuffer() = default;
    explicit AtomicDoubleBuffer(const T& init) {
        buf_[0] = buf_[1] = buf_[2] = init;
    }

    // ── Writer side ──────────────────────────────────────────────────────────
    // Call from exactly one thread.  Non-blocking.
    //
    // Fills writer_slot_ completely, then atomically hands it off to the
    // shared zone and reclaims the slot the reader just vacated.
    void store(const T& val) noexcept {
        buf_[writer_slot_] = val;

        // Publish:
        //   - Hand off writer_slot_ as the new shared slot.
        //   - Set bit 2 to signal "new data available".
        //   - Take back the old shared slot for the next write cycle.
        //
        // memory_order_release: ensures the buf_[writer_slot_] write above is
        // fully visible to the reader before the reader can observe the new
        // shared-slot index via its acquire-load / acquire-exchange.
        const uint32_t published = static_cast<uint32_t>(writer_slot_) | kNewDataFlag;
        const uint32_t old       = packed_.exchange(published, std::memory_order_release);

        // The slot we just got back is safe to reuse on the next write.
        writer_slot_ = static_cast<int>(old & kSlotMask);
    }

    // ── Reader side ──────────────────────────────────────────────────────────
    // Call from exactly one thread.  Never spins, never blocks.
    //
    // If new data has been published since the last call, swaps in the new slot
    // and returns the latest complete write.  Otherwise returns the same value
    // as the previous call (still consistent — just not the absolute latest).
    T load() noexcept {
        // Relaxed peek — cheap check before committing to an exchange.
        const uint32_t peek = packed_.load(std::memory_order_relaxed);

        if (peek & kNewDataFlag) {
            // New data available.  Swap: hand off our current slot to the shared
            // zone, take back the slot that the writer just published.
            //
            // memory_order_acquire: synchronises with the writer's release-exchange,
            // ensuring we see the buf_[] write the writer did before its exchange.
            const uint32_t old = packed_.exchange(
                static_cast<uint32_t>(reader_slot_),   // give back our old slot
                std::memory_order_acquire
            );
            reader_slot_ = static_cast<int>(old & kSlotMask);  // take the new one
        }

        return buf_[reader_slot_];
    }

    // Returns true if the writer has published at least one new value since the
    // last load().  Useful for skipping processing when nothing changed.
    bool has_new_data() const noexcept {
        return packed_.load(std::memory_order_relaxed) & kNewDataFlag;
    }

private:
    // Bit layout of packed_:
    //   bits [1:0] = current shared slot index (0, 1, or 2)
    //   bit  [2]   = new-data flag (set by writer, cleared by reader's exchange)
    static constexpr uint32_t kSlotMask    = 0x3u;
    static constexpr uint32_t kNewDataFlag = 0x4u;

    // Initial state:
    //   slot 0 → reader's private slot
    //   slot 1 → shared zone (packed_ starts pointing here, no new-data flag)
    //   slot 2 → writer's private slot
    alignas(64) T buf_[3]{};

    // packed_ on its own cache line — written by both threads during exchange.
    alignas(64) std::atomic<uint32_t> packed_{1u};  // shared slot = 1, no new data

    // writer_slot_ is only ever read/written by the writer thread.
    // reader_slot_ is only ever read/written by the reader thread.
    // No atomic needed — single-owner access.
    int writer_slot_ = 2;
    int reader_slot_ = 0;
};
