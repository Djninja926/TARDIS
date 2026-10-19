# Multithreaded LRUForgive: Design, Implementation, and Findings

**Author:** Apia Okorafor
**Last updated:** October 18, 2026
**Status:** Two parts. **CacheLib (Sections 1-39, June-August):** the per-thread design (jane-perthread) holds the full ~5-6pp LRUForgive gap from 1 to 16 threads while the single-manager log design's gap collapses (Section 38, the headline); S3FIFOForgive gives about +0.7pp on cache-t-00 single-threaded; S3-FIFO cannot run multithreaded on real traces (artifact defect, Sections 23, 39). **libCacheSim (Section 40, September-October):** all 6,352 CMU and Wikimedia traces at 10%, 1% and 0.1% of the working set; defaults do almost nothing, tuned LRUForgive lowers the miss ratio on every paper workload at 1% (mean +4.8% on held-out traces), S3FIFOForgive stays near zero. A paper-style rerun (object size ignored, per-workload tuning) is ready to launch. Open items for the talk and the paper are in Section 41; the biggest is that the two implementations disagree on the same trace with the same defaults (Section 40.7).

**Reading guide.** Sections are a lab notebook in date order, and later sections correct earlier ones (look for "Corrected" and "INVALIDATED" banners). For the current state, read Sections 38, 39, 40 and 41.

---

## 1. Purpose and context

The single-threaded `MMLruForgive` implementation in CacheLib is validated and produces a mean 7.32% miss-ratio reduction over baseline LRU across all 21 Wikipedia text traces (default hyperparameters, MCACHE mode). The next milestone for the SIGCOMM resubmission is making `MMLruForgive` thread-safe so it runs with multiple CacheLib worker threads, which is required to ship a real production prototype rather than a libCacheSim simulation.

This document records the full multithreading effort from the start of implementation: the two locking designs we built and measured, two benchmark modes, the two core problems that surfaced, the architectural direction that the S3-FIFO reference implementation points toward, and the two workload-characterization findings (cache-size collapse and the temporal-correlation requirement) that reframed the project.

**What "success" means here (clarified with Jane, July 2).** We are building LRUForgive, i.e. LRU plus forgiveness. The pass/fail bar is therefore whether LRUForgive beats plain LRU on miss ratio. S3-FIFO is a categorically stronger base policy and is included in the sweeps as a reference ceiling, not as a bar LRUForgive must clear. Jane's expected ordering is LRU -> LRUForgive -> S3-FIFO, and TARDIS landing between LRU and S3-FIFO is the intended outcome. S3-FIFOForgive (forgiveness on top of the S3-FIFO base) is a later phase; that is the version that would be expected to challenge or beat plain S3-FIFO.

---

## 2. Single-threaded baseline (starting point)

Before multithreading, the validated single-threaded numbers, established on CloudLab C220G2 with the SOSP'23 CacheLib artifact:

- 21-trace Wikipedia text sweep, MCACHE mode (embedding store capped at 5x estimated cache items), 600 MB cache, default hyperparameters.
- Mean miss-ratio reduction over LRU: **7.32%** (median 7.31%, range 6.19% to 8.88%, stdev 0.72%).
- All 21 of 21 traces improved, no regressions.
- Mean throughput overhead vs LRU: 30.37%, uniform across traces.
- For comparison, the paper reports 9.93% on Wikipedia text with Bayesian-tuned hyperparameters. The 2.6 percentage-point gap is deferred tuning headroom; we are at ~74% of the tuned result on defaults, which indicates a faithful implementation.

These are the reference numbers any multithreaded version is measured against.

---

## 3. Shared state requiring synchronization

The `TARDISEmbeddingManager` holds five pieces of shared state that all worker threads read or write:

1. **Global context vectors** (K=8 vectors of D=8 doubles). Read on every embedding update, mutated every 10 accesses via the variance-preserving random walk.
2. **Embedding cache** (`unordered_map<uint64_t, EmbeddingArray>`). Read when a previously-evicted object is requested again, written on every access that touches an embedding.
3. **Access-count map** (`unordered_map<uint64_t, int>`). Read during the forgiveness qualification check, incremented on every access.
4. **Recent-access buffer** (three parallel arrays of size 16, plus index and count). Read during similarity computation at eviction, written on every access via circular-buffer update.
5. **Embedding-cache LRU structure** (`std::list` plus iterator map; MCACHE mode only). Mutated on every access that touches the embedding store.

In the single-threaded version all of this is safe because only one thread touches any of it. With multiple worker threads calling `recordAccess` and triggering forgiveness at eviction, every piece above has race conditions.

A critical observation about the access pattern: TARDIS is **write-dominated**. Every cache access triggers a write (`on_access`). Reads only occur at eviction time, inside the forgiveness loop. This fact drives everything that follows.

---

## 4. Approach 1: Lock-everything baseline

**Design.** A single `std::mutex` inside `TARDISEmbeddingManager`. Every public method acquires it before touching internal state. The write method (`on_access`) and the four read methods (`avg_top_k_similarity_to_recent`, `get_access_count`, `has_embedding`, `get_num_embeddings`) all take the same lock.

**Lock ordering.** `MMLruForgive` holds its `lruMutex_` when calling embedding-manager methods from `applyForgivenessLocked` (the eviction-time loop). The embedding mutex is always acquired inside `lruMutex_`, never the reverse, which avoids deadlock. This ordering is enforced by the call sites and required no change to `MMLruForgive`.

**Implementation note (build system).** Header-only changes to `cachelib/allocator/*.h` require running `mybench/build.sh`, not just `ninja lruforgive` in `mybench/_build/`. The cachelib library has explicit template instantiations of `CacheAllocator<LruForgiveCacheTrait>` that freeze the embedding manager's inlined call sites. A partial rebuild links the old, unsynchronized code and segfaults under multiple threads. This cost a debugging cycle and is worth remembering for every header iteration.

**Results.** Validated and correct at 1, 2, 4, 8, 16 threads on cache-t-00 using the `run.sh` cache-scaling methodology (cache size scales linearly with thread count, hashpower scales log2). No crashes.

| Threads | LRU MR | TARDIS MR | MR improvement | LRU TP (MQPS) | TARDIS TP (MQPS) | TP overhead |
|---------|--------|-----------|----------------|---------------|------------------|-------------|
| 1       | 0.8188 | 0.7624    | 6.89%          | 0.26          | 0.16             | 38%         |
| 2       | 0.9838 | 0.9840    | -0.02%         | 0.41          | 0.32             | 22%         |
| 4       | 0.8131 | 0.7771    | 4.43%          | 0.89          | 0.67             | 25%         |
| 8       | 0.8124 | 0.7885    | 2.94%          | 1.72          | 1.13             | 34%         |
| 16      | 0.8103 | 0.7984    | 1.47%          | 2.16          | 1.44             | 33%         |

Throughput scales 9x for TARDIS from 1 to 16 threads, slightly better than LRU's 8.3x. The global embedding mutex is not the catastrophic bottleneck the original design doc anticipated, because the per-access embedding work is short relative to CacheLib's other internals. The thread=2 row is an anomaly (high miss ratio) that affects both LRU and TARDIS identically, so it is a CacheLib configuration artifact at hashpower 26, not a bug in our code.

---

## 5. Approach 2: Async reads (shared_mutex)

**Design.** Replace the `std::mutex` with `std::shared_mutex`. The write method takes a `unique_lock` (exclusive); the four read methods take a `shared_lock` (concurrent readers allowed). The premise from the original design doc was that this would scale near-linearly for read-heavy workloads.

**Result: no improvement.** Throughput was essentially unchanged at every thread count, with a slight regression at 16 threads (1.44 to 1.41 MQPS). Miss ratios stayed within 0.08 percentage points of lock-everything, confirming correctness but no benefit.

| Threads | Lock-everything TP | Async-reads TP | Change |
|---------|--------------------|----------------|--------|
| 1       | 0.16               | 0.17           | +6%    |
| 4       | 0.67               | 0.67           | 0%     |
| 8       | 1.13               | 1.13           | 0%     |
| 16      | 1.44               | 1.41           | -2%    |

**Root cause.** The original design doc assumed a read-heavy workload. TARDIS is the opposite: write-dominated. Every cache access calls `on_access` (a write, exclusive lock). Reads only execute at eviction time inside `applyForgivenessLocked`. Writers always serialize under `unique_lock` regardless of the mutex flavor, so the concurrent-readers benefit never applies. `shared_mutex` also has higher per-acquire overhead than `std::mutex` for the uncontended case, producing the small 16-thread regression.

This was verified to be a genuine result, not a build artifact (confirmed the diff was correctly applied and compiled).

**Conclusion.** Async reads is a dead end for this workload. The path to throughput is reducing or restructuring the writes, not optimizing reads.

---

## 6. Two benchmark modes

The original mybench harness has each thread independently replay the full trace (call this **replicated mode**). This is unusual and does not model a production cache server, where worker threads handle a shared incoming request stream.

We added a second mode (**round-robin shared mode**, 5th CLI argument). Threads split the trace: thread `i` processes records where `record_idx % n_thread == i`, so every record is processed exactly once across all threads, and the total request count stays constant regardless of thread count. This models a production server where many workers serve one workload.

- Implementation touched `bench.h` (mode field), `cmd.h` (parse argv[5]), and `benchMT.cpp` (the round-robin skip plus run-to-completion semantics for shared mode).
- Validated correct: 1-thread shared mode matches 1-thread replicated mode (miss ratio 0.7619 vs 0.7621/0.7624).

Both modes are now available for every experiment, which let us answer the dilution question rigorously (next section).

---

## 7. Problem A: Miss-ratio degradation (the priority)

**Observation.** The improvement of LRUForgive over LRU degrades sharply as thread count increases. Using the improvement gap (LRU miss ratio minus TARDIS miss ratio, which cancels out effects common to both policies):

| Threads | Shared-mode improvement | Replicated-mode improvement |
|---------|-------------------------|-----------------------------|
| 1       | 6.88%                   | 6.89%                       |
| 4       | 4.29%                   | 4.43%                       |
| 8       | 3.18%                   | 2.94%                       |
| 16      | 2.13%                   | 1.47%                       |

**Both benchmark modes show the same degradation curve.** Two independently-designed benchmarks producing the same pattern is strong evidence the cause is intrinsic to the algorithm under concurrency, not a benchmark artifact.

**Note on absolute vs relative numbers.** In shared mode the absolute miss ratios rise with thread count (e.g., LRU goes from 0.82 to 0.96) because concurrent threads pulling from a partitioned workload scramble the access order, which hurts LRU's recency assumption. This affects both policies, so the **improvement gap** is the correct policy-comparison metric, not the absolute. Absolute miss ratios and throughput are still reported alongside the gap for completeness in the canonical per-cell table (Section 22.5); they simply must not be read as the comparison. We confirmed the rising absolutes are genuine concurrent-access behavior, not a time-advancement bug (tested a fix to the simulated-clock advancement; it did not change the result).

**Root cause (working theory).** The temporal relationships TARDIS learns are a property of the full, ordered access stream. Under N threads:

- The shared 16-slot recency buffer is written by threads at different trace positions, so at N threads each thread's genuinely-recent objects make up only ~16/N of the buffer; the rest is noise from other threads' temporally distant regions. (Read-side dilution.)
- More fundamentally, the embeddings and global context are updated by an out-of-order interleaving of accesses, so the learned relationships may not correspond to real relationships in the workload. (Write-side corruption, the deeper concern.)

These two sub-causes point to different fixes and have not yet been fully disambiguated.

**Candidate fixes considered:**

1. **Per-thread recency buffers.** Each thread keeps its own coherent buffer; forgiveness compares against the union at eviction. Fixes read-side dilution. Open concern (raised by Jane): per-thread buffers may capture meaningless local relationships, because trace properties emerge from the full trace, and this does not fix write-side corruption at all. Not greenlit.
2. **Ordered writer queue.** All threads enqueue accesses with a sequence number; a single writer thread drains and applies updates in trace order. Preserves global temporal ordering of updates, directly addressing the write-side cause. This is the most promising direction (see Section 9).
3. **Longer global buffer** (scale 16 to 16N). Widens the window to compensate for dilution. Does not fix ordering.
4. **Timestamp-gated buffer eviction.** Keep the buffer ordered by trace timestamp so stale cross-thread entries do not displace recent ones. Partial ordering fix.

**Decision (per Jane, June 13 meeting):** Fix miss-ratio degradation FIRST, before further throughput optimization. Reasoning: throughput optimizations such as approximate or unordered writes are likely to degrade miss ratio further, so we want a stable miss-ratio baseline locked in before chasing throughput.

---

## 8. Problem B: Throughput overhead (deprioritized for now)

The multithreaded implementation carries a consistent ~30% throughput overhead vs baseline LRU, driven by the per-access embedding work executing under the global lock on the critical path.

**Candidate optimizations (deferred until miss ratio is fixed):**

1. **Writer thread / queue** (primary direction, see Section 9). Hot path enqueues; background thread does the work.
2. **Merge / collapse thread.** Scans the write queue and collapses multiple updates to the same object ID into one aggregated write, reducing total write volume for popular objects.
3. **Lock striping** (~64 hash-striped mutexes). Writers on different objects run in parallel. Known flaw: a single hot object still serializes all threads on its bucket. Simple near-term win but limited.
4. **SIMD-vectorized cosine similarity (AVX2)**, **cached embedding norms**, **top-k via min-heap**: speed up the eviction-time forgiveness computation. Compound with anything that enlarges the comparison set. (Cached norms and bounded top-k are now implemented; see Section 25.)
5. **Atomic access counts**, **co-located access-count + embedding storage**: take work off the lock and improve cache locality.

---

## 9. Architectural direction: the S3-FIFO reference

Reviewing the multithreaded S3-FIFO implementation in this same codebase (SOSP'23 artifact) revealed a concurrency model that directly informs both problems and provides a working in-codebase template for the writer-queue idea.

**Correction to a prior assumption:** S3-FIFO does NOT use a single shared eviction lock. Its model is:

1. **Lock-free hot path.** `MMS3FIFO::recordAccess` does no list manipulation and acquires no lock. It sets the "accessed" bit on the object and the update time, then returns. All `moveToHead` and `lock_combine` logic is commented out. This is lazy promotion: on access, nothing is reordered, the object is just flagged, and the flag is consulted later at eviction.

2. **Atomic FIFO lists (hybrid, not fully lock-free).** The small (probationary) and main FIFOs are `AtomicDList` structures. The head, tail, and size are `std::atomic`, so reads like `getTail()` are pure atomic loads. But structural mutations (`linkAtHead`, `removeTail`) take locks: each `AtomicDList` has both a `DistributedMutex` and a `shared_mutex`. So "lock-free" is accurate for the hot path (which only sets a bit and touches no list) but not for the add/evict mutations. The ghost/history FIFO is an `AtomicFIFOHashTable`.

3. **Eviction logic lives in `getEvictionCandidate`, computed synchronously at eviction time.** This is the S3-FIFO algorithm proper. When eviction is needed it walks the FIFOs: if the probationary FIFO exceeds `pRatio_` (5%) of total size, pop its tail and check the accessed bit. If accessed, promote to the main FIFO (clear bit, move to main head, second chance). If not accessed, insert into the ghost history and return it as the victim. Otherwise evict from the main FIFO's tail with the same accessed-bit second-chance logic. The `mtx_` lock is taken only once, for lazy ghost-table initialization, not per eviction. This is where the cheap accessed-bit set on the hot path is finally consulted, confirming the deferral pattern.

4. **The background-thread machinery exists but is DISABLED in this build.** There is a second eviction path (`getEvictionCandidate0`) that reads pre-computed victims from a lock-free `folly::MPMCQueue` (`evictCandidateQueue_`, max 64), refilled by `prepareEvictionCandidates()`, optionally driven by a dedicated `evThread_` background thread. However: the background-thread spawn is gated behind `#ifdef ENABLE_SCALABILITY`, which is commented out, and `prepareEvictionCandidates()` itself is commented out in the `-inl` file. So in the build we run, the active path is the synchronous `getEvictionCandidate`; the background-worker version is prototyped-but-shelved infrastructure.

**The key insight (corrected):** S3-FIFO's shipping design moves all *policy* work off the critical path via a cheap accessed-bit, deferring promote-vs-evict decisions to eviction time. The hot path does near-zero work. The fully asynchronous background-worker version was built (`ENABLE_SCALABILITY`, `getEvictionCandidate0`, `prepareEvictionCandidates`) but left disabled, suggesting the synchronous deferral was sufficient for their needs or the async version was not yet production-ready. TARDIS currently does the exact opposite of even the shipping design: the full embedding update (64 doubles of math), recency-buffer write, and LRU-list move all happen under a global lock on every single access.

**Implication for TARDIS:** the lesson is the deferral *principle*, proven in S3-FIFO's shipping hot path, not the specific background thread (which they disabled). Our log-structured design with a background applier is essentially the asynchronous extension S3-FIFO sketched (`ENABLE_SCALABILITY`) but adapted for embeddings rather than eviction candidates. Unlike eviction candidates, embedding updates are order-sensitive and accumulate state, which is why our applier needs sequence ordering where their candidate queue did not.

**Proposed TARDIS redesign, modeled on S3-FIFO:**

- **Hot path (`on_access`):** instead of updating the embedding under lock, enqueue the accessed object ID plus a global sequence number into a lock-free MPMC queue. Cheap, no contention, no embedding math on the critical path.
- **Background writer thread:** drains the queue and performs the actual embedding updates, context advancement, and recency-buffer maintenance. Because a single thread owns the embedding state, no lock is needed on that state at all.
- **Sequence numbers:** tagging each enqueued access with the true trace position lets the writer apply updates in exact trace order.

**Why this addresses both problems at once:**

- **Throughput:** the hot path becomes a cheap enqueue, matching S3-FIFO's lazy philosophy and removing per-access lock contention.
- **Miss ratio:** the single writer applies updates in sequence order, restoring the temporal coherence that concurrent out-of-order updates destroyed. Crucially, this keeps ONE global embedding state (preserving cross-thread relationships, addressing Jane's concern about per-thread buffers losing global structure) while feeding it an ordered stream (so relationships are learned correctly).

**Open questions for design discussion:**

1. **Strict vs approximate ordering.** Does the writer need to apply updates in strict sequence order (correctness-critical, but the writer could become a bottleneck and the queue could grow unbounded), or is approximate ordering good enough? This is the central tension: Jane's original framing assumed order does not matter for embeddings (a throughput argument), but the miss-ratio root-cause theory says out-of-order updates are exactly the problem (a correctness argument). These cannot both be true; an experiment should settle it. (Settled: see Section 14b. True trace order is required.)
2. **Single writer vs multiple writers.** Can one writer keep up with N producers, or do we need multiple writers (which reintroduces ordering and contention questions)?
3. **Merge/collapse.** If the queue backs up, a merge step that collapses repeated updates to the same object reduces writer load, at the cost of some fidelity.

---

## 10. Chosen direction

Based on the S3-FIFO review and the two meetings on June 13, the decided direction is a **log-structured lazy update** for the embeddings, grounded in LFS and LSM-tree. This is the refined form of the writer-queue idea: the hot path appends access events to a log, a single background thread applies them lazily and in sequence order, and two optimizations layer on top (a merge/compaction thread and a hot/cold embedding split). The remaining sections derive and specify this design. A full status and ordered task list is in Section 15.

---

## 11. Why log-structured: the LFS and LSM-tree mapping

The two papers Jane pointed to are not arbitrary. Each justifies a specific piece of the design.

**LFS (log-structured file system).** The enemy LFS attacks is in-place random writes: to update data you seek to where it lives and overwrite, which is dominated by seek cost. LFS instead appends all modifications sequentially to a single log, turning random writes into sequential appends, and reclaims stale space later with a background segment cleaner. A notable detail: the cleaner segregates slowly-changing data from rapidly-changing data and treats them differently.

Mapping to TARDIS: our in-place embedding update under a global lock is the random write. Every thread mutating shared embedding state under a lock is the seek-and-overwrite cost. The LFS answer is to stop doing in-place mutation on the hot path and instead append the access event to a log. Appends do not contend the way locked in-place updates do. The cleaner's slow/fast segregation is the conceptual seed for the hot/cold embedding split (Section 14). We do NOT need LFS's durability, indexing, or crash-recovery machinery, because our log is in-memory and ephemeral; we get the write-speed benefit without the read-reconstruction cost.

**LSM-tree (log-structured merge tree).** LSM adds a second idea on top of the log: an in-memory buffer (memtable) absorbs writes, is flushed in batches, and a background compaction process merges entries, collapsing multiple updates to the same key. Updates are never applied in place; new versions are written with timestamps and old ones discarded at merge time.

Mapping to TARDIS: our access-event log is the memtable. The compaction/merge step is Jane's merge-thread optimization: collapse multiple access events for the same object into one combined update. The timestamps that LSM uses for versioning are our sequence numbers, which also serve to enforce trace order. We do NOT need the multi-level SSTable hierarchy (that manages terabytes of on-disk data across levels); we have exactly two tiers, the log and the applied embedding store.

The one place neither paper helps us directly: in a database, merging updates to one key is trivial (last-writer-wins). TARDIS embedding updates are not last-writer-wins; each applies a context-dependent transform. Merging therefore requires closed-form composition math, derived in Section 13.

---

## 12. The log-structured architecture

**Hot path (`on_access`).** Replace the locked in-place embedding update with a single append of an access event to a log. No lock on embedding state, no embedding math on the critical path.

```
struct AccessEvent { uint64_t obj_id; uint64_t seq; };  // 16 bytes
```

`seq` is the trace record position (not a runtime atomic counter), so ordering is exact by construction and independent of thread scheduling. In round-robin shared mode the global index is recoverable from the per-thread local count and thread id.

**Log structure: per-thread append buffers.** Each worker thread owns a single-producer ring buffer it appends to with zero contention (no other thread touches it). Each thread's buffer is internally seq-ordered for free, because a thread processes its own records in order. This is preferred over a single shared MPMC queue because it eliminates push-side contention entirely.

**Background applier.** A single thread drains the per-thread buffers, applying events in global sequence order. Because one thread owns all embedding state, no lock is needed on that state. The applier does an N-way merge across the per-thread buffers, emitting the lowest-seq event next. (A size-N min-heap keyed on seq is the natural structure for this merge; held in reserve as the implementation detail.) The exact ordering guarantee is specified in Section 12b.

**Reads at eviction.** Forgiveness still reads embeddings and the recency buffer at eviction time, on worker threads. This is single-writer (the applier) multi-reader (eviction). A low-traffic mutex or atomic-publish protects it. Crucially this lock is NOT on the hot path, so it does not reintroduce the throughput problem. Eviction reads see embedding state as current as the applier has caught up to (bounded staleness by log depth).

**Why this fixes both problems:**
- Throughput: the hot path becomes a cheap append, matching S3-FIFO's lazy philosophy; no per-access lock contention.
- Miss ratio: the single applier applies updates in sequence order, restoring the temporal coherence that concurrent out-of-order updates destroyed. We keep ONE global embedding state (preserving cross-thread relationships, which addresses the concern about per-thread buffers losing global structure) while feeding it an ordered stream (so relationships are learned correctly).

**On trace-order preservation.** Order is exactly preserved if (a) the sequence number comes from the trace record position rather than a runtime counter, and (b) the applier enforces strict sequential application. Under those two conditions the embedding state is identical to single-threaded. Relaxing either (runtime counter, or approximate ordering that tolerates small reorderings for throughput) trades exactness for speed; how much accuracy is lost is the empirical question the ordering experiment measures. Strict ordering has a head-of-line risk: if the applier must apply seq=k before k+1 and the owning thread is slow, the applier stalls. We build strict first (correctness ceiling), then relax and measure, mirroring the lock-everything-then-async-reads progression.

---

## 12b. How the applier guarantees global sequence order

This section answers Jane's question (July 2): in true-order mode, how does the applier guarantee global sequence order, and if events are queued out of order, does it wait for the correct index before processing.

**The invariant.** The applier emits and applies events in strictly increasing global `seq`. It never applies `seq=k+1` before `seq=k` has been applied. So yes: if a fast thread has already enqueued a higher-seq event but the thread that owns the next-needed lower-seq event has not produced it yet, the applier waits for that lower seq. It does not process the higher one first.

**The mechanism (N-way merge).** Each per-thread SPSC buffer is internally seq-sorted for free, because a worker appends its own records in trace order. The applier performs an N-way merge across the buffer heads, always selecting the globally minimum seq. The correctness-critical detail is the handling of an empty-but-live buffer: a buffer that is momentarily empty could still produce a smaller seq than any currently-visible head, so the applier must block on that buffer rather than apply the current minimum from the non-empty buffers. Selecting the minimum among only the currently-nonempty buffers would be subtly wrong (it can apply out of order whenever a buffer is transiently empty) and would behave like the enqueue-order failure in Section 14b.

**Simplification in round-robin shared mode.** Because thread `i` processes exactly the records where `record_idx % n_thread == i`, seq=k always belongs to thread `k mod N`. The applier therefore consumes seqs 0, 1, 2, ... in order, and to apply seq=k it reads the head of buffer `(k mod N)`, blocking if that buffer is momentarily empty. This is equivalent to the min-heap merge but with known routing, so there is no ambiguity about which buffer the next-needed seq will come from.

**Termination.** Each thread publishes an EOF sentinel when it finishes its slice; the applier drops a finished buffer from the merge set rather than waiting on it forever. Without this the strict-blocking applier would deadlock at end-of-trace.

**Cost.** The strict-blocking guarantee is the source of the head-of-line stall: one slow worker stalls the single applier. This is one reason single-applier throughput is poor (Section 14b caveat 3) and is exactly what the non-strict windowed-ordering relaxation (Section 15, Miss-ratio/throughput items) is designed to loosen, by requiring order only across window boundaries rather than every event.

**Correction (July 28): the shipped Stage 1 batched applier is NOT strict-blocking.** The strict guarantee above describes the Option B design intent. The batched applier that actually runs (Section 14c) relaxed it: its N-way merge takes the minimum seq among the buffers that currently hold an event and skips a momentarily-empty buffer rather than blocking for it, so a straggler low-seq event is deferred to a later batch and applied out of true order. The reorder is therefore already non-strict, but bounded only implicitly by SPSC buffer capacity, `kBatchSize`, and worker timing, and it widens with thread count (more buffers, more skew, more deferred stragglers). This implicit, N-dependent reorder is the write-side corruption that drives the gap decay in the tug-of-war (Section 24.1). Explicit windowed ordering (Section 15 item 2) replaces this uncontrolled window with a hard seq-width W: order is enforced across window boundaries and reorder is capped at W independent of thread count, which both bounds the corruption and provides the barrier that sharded appliers reconcile context and recency at.

---

## 13. Merge math (compaction viability)

The merge optimization collapses N access events for the same object into one applied update. Whether this is exact or approximate determines its viability. A single access does three things: possibly advance the global context (batched every 10 accesses), update the object embedding toward the current context, and write the object into the recency buffer. The three merge differently.

**Part 1: merging embedding updates with constant context (exact).** Writing `a = sqrt(1-alpha)`, `b = sqrt(alpha)`, with the update rule `E <- a*E + b*C`. After N updates with constant C:

```
E^(N) = a^N * E^(0) + b * C * (1 - a^N) / (1 - a)
      = (1-alpha)^(N/2) * E^(0) + sqrt(alpha) * (1 - (1-alpha)^(N/2)) / (1 - sqrt(1-alpha)) * C
```

This is O(1): one `pow` call gives the merged update regardless of N. Verified correct at the boundaries: N=1 reproduces the single-step rule exactly; N to infinity converges to the fixed point `E* = sqrt(alpha)*C / (1 - sqrt(1-alpha))`. So with constant context, merging N embedding updates is exact and constant-time.

**Part 2: context is not constant, but drift is tiny and bounded.** Context advances every 10 global accesses via `C <- sqrt(1-10*beta)*C + sqrt(10*beta)*xi`. With beta=0.001, each advance is approximately `C <- 0.995*C + 0.1*xi`, a small move. The exact merged form with varying context is a geometrically-weighted sum of the contexts seen: `E^(N) = a^N * E^(0) + b * sum_{k=1..N} a^(N-k) * C_k`. We make this exact by merging only within a constant-context run: split a burst at context-advance boundaries and apply the Part 1 closed form within each sub-run. A typical hot-object burst crosses at most one or two boundaries, so N events become one or two merged updates instead of N, still exact. The merge thread needs the context-advance boundary positions (derivable from the global access counter).

**Part 3: the recency buffer (the genuine approximation).** Each access writes the object into the 16-slot recency buffer. Merging N accesses into one changes how many times and with what embedding the object appears in the buffer, which changes forgiveness decisions (forgiveness reads the buffer). Three options, increasing fidelity:
1. Write the object once with the final merged embedding (simplest; loses the buffer-domination effect of repeated writes).
2. Write min(N,16) times with the final embedding (preserves domination, uses final rather than progressive embeddings).
3. Only merge accesses old enough that they would have been overwritten in the buffer anyway (most conservative, merges less).

Intuition suggests option 1 or 2 is fine because consecutive accesses to one object are exactly where the recency-buffer signal is least useful (recency already captures that a just-accessed object is hot), but this affects miss ratio and must be measured.

**Verdict.** The embedding merge is exact and O(1) within constant-context runs (Parts 1-2). The recency-buffer handling is a tunable approximation (Part 3) whose miss-ratio impact is empirical. Merge is viable and worth building, but unlike a last-writer-wins database it is not transparently free; the buffer behavior is a real design decision. The core log + applier do not need merge, so we build and validate those first and add merge as a measured optimization.

---

## 14. Optimization roadmap on top of the log

1. **Merge/compaction thread.** Collapse same-object runs within context boundaries using the Section 13 closed form. Reduces applier load for hot objects. Requires a decision on the Part 3 recency-buffer handling.
2. **Hot/cold embedding split.** Mirroring LFS's slow/fast segregation: hot objects (frequently accessed, likely to be read soon at eviction) get eager or near-eager application so their embeddings are fresh when needed; cold objects stay fully lazy. Reduces staleness where it matters most.
3. **Compute-level speedups (independent of the log design):** SIMD-vectorized cosine similarity (AVX2), cached embedding norms, top-k via a bounded heap instead of full sort, atomic access counts, co-located access-count and embedding storage. (Cached norms + bounded top-k landed July 28; see Section 25. AVX2 confirmed available but deferred as low-value.)

---

## 14b. Experimental result: true trace order fixes most of the degradation

The log-structured design (Option B: per-thread SPSC append buffers feeding a single background applier that does an N-way merge by seq) was implemented and measured on cache-t-00 in shared mode. The decisive variable turned out to be **what the sequence number represents**.

**First attempt: enqueue-order seq (a runtime atomic counter).** Each `on_access` grabbed a number from a global atomic counter. This produces a consistent total order, but not the trace's temporal order: a thread racing ahead stamps its later-in-trace accesses with lower seq numbers. Result: essentially no improvement over lock-everything. The improvement gap still decayed from ~6.9% at 1 thread to ~2% at 16. Conclusion: any single total order is insufficient.

**Second attempt: true-trace-order seq (the record's global trace position).** The seq is set from the benchmark's global record index (threaded through a `thread_local` set in `benchMT` immediately before each access, read inside `on_access`). The applier then applies events in genuine trace order. Result: the gap roughly doubled at every thread count.

| Threads | LRU miss | TARDIS miss (true order) | Improvement gap (true order) | gap (enqueue order) | gap (lock-everything) |
|---------|----------|--------------------------|------------------------------|---------------------|-----------------------|
| 1       | 0.8188   | 0.7623                   | 6.90%                        | 6.90%               | 6.88%                 |
| 4       | 0.9182   | 0.8288                   | 9.74%                        | 4.32%               | 4.29%                 |
| 8       | 0.9444   | 0.8783                   | 7.00%                        | 3.24%               | 3.18%                 |
| 16      | 0.9607   | 0.9142                   | 4.84%                        | 2.10%               | 2.13%                 |

(t=2 omitted: the known CacheLib hashpower anomaly at hp=26 inflates both policies' miss ratios there.)

**Interpretation.** The contrast between enqueue-order (no benefit) and true-trace-order (gap doubled) is itself strong evidence that the root cause was identified correctly: TARDIS's learned relationships are a property of the workload's real temporal order, so embedding updates must be applied in trace order, not merely serialized through some consistent order. The log-structured architecture with a true-trace-order applier delivers exactly that.

**Caveats.**
1. The degradation is substantially recovered but not fully eliminated: 6.9% at 1 thread to 4.84% at 16. Leading suspect for the residual is the 16-slot recency buffer being too small relative to N threads' combined recent working set. Untested. (Later falsified as the mechanism: recency-window scaling did not flatten the decay. See the recency-scaling result and Section 24.1; the residual is write-side, not read-side.)
2. The t=4 and t=8 gaps appear to exceed the single-thread 6.9%. **(Corrected Aug 2, void.)** This was an artifact of a single-sampled, inflated LRU baseline, not a real effect. A 5-rep turbo-off rerun at the same 600MB config (Section 30.5) gives monotonically-decreasing gaps 5.66/4.88/4.16/2.94 at t=1/4/8/16 with no bump; the LRUForgive miss ratios reproduce this table exactly (0.7622 vs 0.7623 at t=1), but the LRU column here was high (t=4 0.9182 vs the true 0.8777, t=16 0.9607 vs 0.9420). There is no super-linear gap. Gaps must be computed against multi-sampled LRU in shared mode.
3. This was a correctness-only run. Throughput is poor (0.10 to 0.36 MQPS) because the single applier is a serial bottleneck, well below lock-everything's ~1.4 MQPS. A naive single applier is a net throughput loss versus locking, so the throughput optimizations (multiple appliers, merge/compaction, hot/cold split) are now required for the log-structured approach to be a net win, not optional.

---

## 14c. Throughput work: batching (Stage 1) and merge (Stage 2)

With true-trace-order fixing most of the miss-ratio degradation, the single applier became the throughput bottleneck (0.10 to 0.36 MQPS vs lock-everything's ~1.4). Two optimizations were tried.

**Stage 1: batched apply.** The applier accumulates a seq-ordered batch of kBatchSize=256 events and applies them under a single state-lock acquisition instead of one per event. Semantics identical (within 0.04pp of Option B). Result: miss ratio unchanged (as designed), but throughput barely moved (+0 to 7%, nothing at t=1 or t=16). Diagnosis: lock overhead was not the bottleneck. The applier is compute-bound on the per-event embedding arithmetic (update_embedding is 64 doubles, update_recent copies 512 bytes, plus two hashmap lookups). Stage 1 is retained as the canonical implementation: clean, same miss ratio, no merge complexity.

**Stage 2: merge/compaction.** The applier merges same-object accesses within a window using the closed form (Section 13), collapsing u updates to one pow()-based update at the object's last occurrence, with exact access-count and init-threshold handling and write-once recency (option 1). Result: miss ratio held EXACTLY (single-thread 0.7624, confirming the merge math and approximations are correct), but throughput did not improve. Instrumentation revealed why: the merge ratio (applies per event) is approximately 0.998 to 1.0 even at window 128. Almost no object recurs within a window on cache-t-00; the working set is too spread out to merge.

A second realization: the effective merge window is min(kMergeWindow, instantaneous backlog). In single-thread the applier keeps up (the worker, doing the full cache operation, is the bottleneck, not the applier), so the backlog stays near zero and windows are tiny regardless of kMergeWindow. The applier only becomes the bottleneck under multithreading. Even there, the repeat density is too low for merge to fire.

Conclusion: merge is a dead end for this trace's access pattern. The math is correct and banked for future high-skew workloads, but it does not help here. Reverted to Stage 1.

**Why merge found nothing (corrected July 13).** An earlier draft attributed the near-1.0 merge ratio to the traces being mostly singletons ("low reuse density"). The Stage 0 one-hit-wonder measurement (Section 19.10) falsifies that: on msr_proj_0, 97.4% of requests hit objects that recur, so repeats are abundant. Merge found nothing to collapse because repeats are too far apart in the stream to land in the same window: high reuse count, long reuse distance. The correct fix would therefore be a larger or reuse-distance-aware merge window, not one-hit-wonder filtering. This decouples the merge diagnosis from the (now falsified) Bloom-filter throughput hypothesis; the two do not share a root cause.

**Throughput levers remaining:** multiple sharded appliers (hard: the global context and single 16-slot recency buffer resist sharding), SIMD/cached-norm compute speedups (modest, low-risk, semantics-preserving; landed July 28, Section 25), update subsampling (apply a fraction of accesses; trades miss ratio for throughput, untested, potentially a large cheap win if the embedding signal is robust to subsampling), and a Bloom filter to drop one-hit-wonders before they reach the applier (Section 15).

---

## 14d. Strategy menu for restrategizing (open questions for the team)

The obvious throughput levers (batching, merge) have hit diminishing returns. Fresh angles on both axes, to be prioritized with the team:

Throughput:
1. Multiple sharded appliers: partition objects by hash across K appliers. Open problem: the shared global context and single recency buffer. Possible relaxations: per-applier recency, read-mostly lock-free context. Biggest lever, hardest. Enabled by non-strict windowed ordering (see Section 15).
2. Update subsampling: apply only a fraction (e.g. 1 in 4) of accesses. Trivial to test; could reveal the embedding signal is robust to subsampling, a cheap large win.
3. SIMD the per-event math + cached embedding norms. Modest, safe. (Norm caching + bounded top-k done July 28, Section 25; net throughput-neutral on applier-bound traces because the applier, not the read path, is the bottleneck.)
4. Bloom filter to drop one-hit-wonders before the applier. Cheap; the hypothesized primary throughput fix given the near-1.0 merge ratio (singletons dominate the applier load). (Later falsified, Section 19.10-19.12.)

Miss ratio:
5. Residual-degradation hypothesis: the 16-slot recency buffer undersamples the larger combined working set at high thread counts. Test scaling recent_window with thread count (16*N) or a bigger fixed size. Cheap, could close the t=16 gap (4.8% back toward 6.9%). (Later falsified: recency-window scaling does not flatten the decay. The residual is write-side.)
6. Per-thread recency buffers feeding the global embedding state (distinct from the vetoed per-thread embeddings). Re-raise now that the dilution mechanism is understood.
7. Hyperparameter tuning (Bayesian) over lr / ctx_speed / threshold. We are at paper defaults and get 7.32% single-thread vs the paper's reported 9.93%, suggesting real headroom on both single and multithreaded miss ratio. Possibly the highest-value miss-ratio work.

---

## 15. Status summary and next steps

**Done:**
- Single-threaded MMLruForgive validated (21-trace, 7.32% mean improvement).
- MCACHE mode wired and validated.
- Round-robin shared benchmark mode added and validated.
- Lock-everything baseline: implemented, correct, measured.
- Async reads: implemented, measured, confirmed no benefit (write-dominated workload).
- Dilution confirmed real and benchmark-independent.
- S3-FIFO multithreaded model reviewed and documented.
- LFS and LSM-tree reviewed; merge math derived (embedding merge exact within constant-context runs; recency-buffer handling a tunable approximation).
- Log-structured Option B implemented (per-thread SPSC buffers + single applier).
- **Key result: true-trace-order application roughly doubles the improvement gap and mostly fixes the degradation (Section 14b). Enqueue-order does not help; trace-order does.**
- Applier global-order guarantee specified and documented (Section 12b).
- Option A (single shared MPMC queue + heap reorder) built and compared: identical miss ratio, slightly worse throughput, confirms the single applier is the sole bottleneck. Reverted to B.
- Stage 1 (batched apply) and Stage 2 (merge) built and measured (Section 14c): both miss-ratio-neutral, both throughput-neutral on this trace (not lock-bound; no repeats to merge). Stage 1 is canonical; merge banked for high-skew workloads.
- **Cache-size collapse characterized across 5 Wikipedia traces including the full 207M-request trace in the target regime (Section 16).**
- **Temporal-correlation requirement established via the zipf IRM trace (Section 16b): high reuse but zero correlation yields zero gap at every cache size.**
- **CMU-PDL + MSR-family sweeps complete; per-trace operating points selected via max-meaningful-gap (Sections 18, 20).**
- **Multithreaded phase complete at four operating points (Section 22); gap-vs-threads is NOT uniformly degrading (tug-of-war, Section 24.1).**
- **S3-FIFO concurrency defect diagnosed (Section 23); Bloom-filter arc closed (Section 19); recency-scaling falsified; bimodality shown to be a cliff property (Section 24).**
- **Eviction-path norm-caching + bounded top-k optimization landed and validated miss-ratio-neutral across t=1..16 (Section 25).**

**Decided:**
- Fix miss-ratio degradation before throughput. (Largely achieved via true-trace-order.)
- Log-structured lazy update is the chosen architecture, with Stage 1 batched applier as the canonical implementation.
- Success bar is LRUForgive beating plain LRU, not S3-FIFO. Expected ordering LRU -> LRUForgive -> S3-FIFO; S3-FIFOForgive is a later phase.

**Priority 0 (gating everything): the CMU-PDL sweep.** Per trace, find the knee of the miss-rate-vs-cache-size curve (where per-doubling miss reduction plateaus), which is the realistic operating size for that trace. At that size compare LRU, LRUForgive, S3-FIFO on a single thread. The gating question is whether LRUForgive beats plain LRU at any trace's realistic operating point. If yes, that trace + size becomes the operating point for re-running the multithreaded experiments and the optimization work below has a target. If no on all traces, the direction conversation with Jane moves to mechanism (Section 17, categories B/C), because the KV traces are now the only remaining candidates that could carry both conditions TARDIS needs (Section 16b). (Resolved: the sweep found operating points; see Sections 18, 20.)

**Next (ordered), post-characterization:**

1. **Compute-level speedups (DONE, Section 25).** Norm caching + bounded top-k. Miss-ratio-neutral across all thread counts; throughput neutral on applier-bound traces, small gain on read-heavy traces (prxy_1). AVX2 on the dot confirmed available (objdump shows scalar) but deferred as low-value.
2. **Non-strict windowed ordering, then sharded appliers.** Windowed ordering is the PRECONDITION for sharding: `context_` has a strict sequential dependency, so naive sharding scrambles order = the write-side corruption that drives the MT decay. Windows declare intra-window order irrelevant (parallel appliers) with a sync at each window boundary. W is the fidelity-vs-parallelism knob. This is ALSO miss-ratio work, because restoring ordering fidelity should recover some of the degraded gap. This attacks the actual bottleneck (the single applier: t=16 penalty is 3.3x to 6.6x vs LRU, Section 22.2/25.5).
3. **Recency-window 8/4 sweep (deferred, cheap).** Miss ratio was flat 16->64, so smaller may be a free throughput win. `recent_window_` is a runtime constructor argument (not compile-time), so this needs a CLI knob wired to the constructor, not a rebuild per value.
4. **Miss-ratio degradation root cause -> hyperparameter tuning** if nothing else remains. Bayesian over lr / ctx_speed / forgiveThreshold. 7.32% single-thread defaults vs the paper's tuned 9.93%, ~2.6pp headroom. Lifts both single-thread and MT numbers. Expensive in runs.

Dead levers (do not revisit): recency-buffer scaling, per-thread recency buffers (same mechanism), Bloom filter, lock striping, naive merge/compaction.

---

## 16. Cache-size sweep: the improvement collapses at realistic cache sizes (June 18)

Following the second sync, Jane asked us to increase the cache size (from 600MB) until miss ratio reaches a realistic plateau, because at 90%+ miss the multithreaded comparisons are noise-dominated. The sweep produced the most consequential finding of the project so far.

**Clarification on the target (Jane, July 2).** The earlier "10-20% miss ratio" was not a hard target. The point was only to avoid comparisons at absurdly high miss ratios. The actual instruction is: do not target a specific miss ratio, target the improvement from adding cache, and stop where that improvement plateaus (the knee). Two distinct quantities both get called "improvement" and behave oppositely, so keep them separate:
- **Improvement from adding cache:** the per-doubling miss-rate reduction. This plateaus; the plateau is the knee, which is the realistic operating cache size for the trace. This is what `plot_sweep.py` detects and what Jane means by "when it plateaus."
- **TARDIS's gap over LRU at a fixed size:** this does not plateau on Wikipedia; it collapses to zero as the cache grows (below).

The methodology is therefore: per trace, find the knee, use that as the operating size, and at that size compare LRU / LRUForgive / S3-FIFO. On low-reuse traces the knee sits past where forgiveness still helps, which is the tension this section documents.

**Single-thread LRU vs TARDIS, cache-t-00, increasing cache size:**

| Cache MB | LRU miss | TARDIS miss | Gap |
|----------|----------|-------------|-----|
| 600      | 0.8188   | 0.7624      | 6.89% |
| 1200     | 0.7543   | 0.7046      | 6.59% |
| 2400     | 0.6755   | 0.6419      | 4.97% |
| 4800     | 0.5805   | 0.5682      | 2.12% |
| 9600     | 0.4805   | 0.4784      | 0.44% |
| 19200    | 0.3818   | 0.3815      | 0.08% |

The TARDIS advantage shrinks monotonically toward zero as the cache grows, and even at 19.2GB we are still at 38% miss, never reaching a low-miss plateau. To reach that regime needs an even larger cache, where the gap is already gone.

**The collapse is universal across the cohort.** The same sweep on cache-t-05, cache-t-10, cache-t-15 gives the same picture:

| Trace | 600MB | 2400MB | 9600MB |
|-------|-------|--------|--------|
| cache-t-00 | 6.89% | 4.97% | 0.44% |
| cache-t-05 | 6.39% | 4.84% | 0.47% |
| cache-t-10 | 8.88% | 4.83% | 0.25% |
| cache-t-15 | 8.70% | 5.96% | 0.39% |

Every trace: meaningful gap at 600MB, roughly halved at 2400MB, essentially gone by 9600MB.

**Mechanism.** Forgiveness rescues objects that LRU would evict but that will be reused soon. This only helps under high eviction pressure (cache small relative to working set, i.e. a high working-set-to-cache ratio). When the cache is roomy, LRU already retains the reusable objects because there is space, so there is little for forgiveness to rescue. The benefit lives entirely in the high-pressure regime.

**Implication.** On real Wikipedia CDN traces, in the realistic-miss-rate regime, the current TARDIS provides almost no benefit. This reframes the project: optimizing the throughput of a system whose miss-ratio benefit disappears at realistic cache sizes (the Bloom-filter and trace-point work) is premature until we establish that a realistic operating point with a benefit exists. The throughput priority list is gated on this.

**Important: the size argument is total, not per-thread** (confirmed in cache.cpp: one setCacheSize, one cache object, all threads share bdata->cache). So these sweeps vary one shared cache's total capacity, which is the correct interpretation.

**Update: wiki_2019t confirms the collapse, in the target regime itself.** The full 5GB / 207M-request trace was swept at large caches:

| Cache | LRU miss | TARDIS miss | Gap |
|-------|----------|-------------|-----|
| 9.4GB | 0.4513 | 0.4486 | 0.60% |
| 19GB  | 0.3436 | 0.3430 | 0.17% |
| 38GB  | 0.2522 | 0.2520 | 0.08% |
| 75GB  | 0.1720 | 0.1720 | 0.00% |

At 75GB we reach 17.2% miss, a realistic low-miss regime, and the gap is exactly zero (0.1720 vs 0.1720). This is the most production-realistic trace, evaluated in the exact target miss-rate range, and TARDIS provides no benefit. The finding is confirmed across 5 traces (4 cohort plus the full trace) and in the target regime, not just extrapolated from the high-miss corner.

This also unifies the throughput and miss-ratio findings: these traces are low-reuse, which is simultaneously why merge found nothing to collapse (no repeats) and why forgiveness has nothing to rescue (LRU already keeps what little is reused). Same workload property, both symptoms.

---

## 16b. The generated trace is IRM by construction, so it isolates the second condition (temporal correlation)

An earlier draft of this document listed "sweep the S3-FIFO generated traces" as the single decisive positive experiment and claimed the traces were not in our directory. Both statements were wrong and are corrected here.

**We have the generator and the trace.** The generated trace comes from `libCacheSim/scripts/data_gen.py`, run as:

```
python3 libCacheSim/scripts/data_gen.py -m 1000000 -n 20000000 --alpha 1.0 --bin-output zipf1.0_1_100.oracleGeneral.bin
```

This produces `zipf1.0_1_100.oracleGeneral.bin`: 1,000,000 objects, 20,000,000 requests, drawn iid from a Zipf(alpha=1.0) popularity distribution.

**Result: TARDIS equals LRU exactly, at every cache size.** Single-thread sweep:

| Cache MB | hp | LRU miss | TARDIS miss | Gap |
|----------|----|----------|-------------|-----|
| 600   | 25 | 0.1923 | 0.1923 | 0.00% |
| 1200  | 26 | 0.1280 | 0.1280 | 0.00% |
| 2400  | 27 | 0.0616 | 0.0616 | 0.00% |
| 4800  | 28 | 0.0100 | 0.0100 | 0.00% |
| 9600  | 29 | 0.0100 | 0.0100 | 0.00% |
| 19200 | 30 | 0.0100 | 0.0100 | 0.00% |

The gap is zero everywhere, including moderate-miss regimes (19% down to 1%). This is not the pressure-driven collapse of Section 16; it is a flat zero across the entire curve.

**Why: the generated trace has no temporal structure to learn.** A Zipf trace from `data_gen.py` is a pure independent-reference-model (IRM) stream: each request is drawn iid from the popularity distribution, with no correlation between consecutive accesses. It is memoryless by construction. TARDIS's entire mechanism is learning temporal co-occurrence: each access nudges an object's embedding toward the slowly-drifting context vector, so objects accessed near each other in time acquire similar embeddings, and forgiveness rescues eviction candidates similar to recently-accessed objects. In an IRM trace there is no "near each other in time" structure. A popular object's occurrences are scattered uniformly at random across 20M requests, so its embedding averages the whole context walk into noise, and forgiveness similarity is noise. The gap is zero not because reuse is low (reuse is high here, hence the sub-1% miss at large caches) but because temporal correlation is zero.

This is why the generated trace was never used as the working trace and cannot be the decisive positive experiment: it is structurally outside TARDIS's mechanism. It functions instead as a clean negative control that isolates the second necessary condition.

**The two-condition characterization.** Combining Sections 16 and 16b, TARDIS's forgiveness benefit requires BOTH:

1. **High eviction pressure** (working-set-to-cache ratio high enough that LRU is forced to evict objects that will be reused). Established by the cache-size collapse: remove the pressure and the benefit vanishes.
2. **Temporal correlation in the access stream** (consecutive accesses are not independent, so co-occurrence structure exists to learn). Established by the zipf zero-gap: remove the correlation and the benefit vanishes even under pressure and high reuse.

Wikipedia CDN traffic has condition 1 only in the extreme small-cache corner and weak condition 2, so the benefit exists only where the cache is unrealistically small. Zipf-IRM has strong reuse but zero condition 2, so no benefit anywhere. The remaining candidates that could satisfy both are real KV traces with genuine temporal/session structure (Twitter clusters, metaKV), which is exactly what the CMU-PDL sweep evaluates.

**Optional check worth running.** On the zipf run, confirm whether forgiveness even fires and how often. A gap of exactly 0.0000 to four decimals is compatible with either "forgiveness never fires on IRM" or "it fires but nets exactly zero." If it fires but nets zero, that is a stronger and stranger claim worth understanding before it goes in the paper; if it never fires, the clean story is "TARDIS disengages on uncorrelated streams."

---

## 17. Direction decision (for the sync)

The generated-trace result (Section 16b) removes what an earlier draft called the decisive positive experiment: the IRM trace cannot show a benefit by construction, so it settles nothing about whether TARDIS works when its assumptions hold. The only remaining place a realistic-regime benefit can appear is a workload with both high pressure and real temporal correlation, i.e. the CMU-PDL KV traces. The sweep on those is Priority 0 (Section 15). The options below are the branches after the sweep result is in.

**A. The sweep finds a KV trace where LRUForgive beats LRU at its knee.** Best case. That trace + size is the operating point; re-run the multithreaded experiments and the optimization roadmap (Bloom filter, tracing, recency scaling, windowed ordering) there. The Wikipedia collapse and the zipf zero-gap become a clean characterization result: "embedding-based forgiveness helps under high pressure AND temporal correlation, and we show exactly where that regime is." (This is what happened; see Section 18.)

**B. The sweep finds no benefit on any real trace.** Then the value proposition itself is in question, and the honest contribution is the characterization: a two-condition map (pressure x correlation) of when forgiveness helps, plus the negative results on real CDN and IRM traffic. Worth sanity-checking first whether the 600MB Wikipedia gap is a real algorithmic effect (stable across seeds and minor config perturbations) rather than an artifact of the extreme-pressure corner.

**C. Rethink the mechanism for low-reuse / low-correlation traffic.** The embeddings capture co-occurrence structure; forgiveness is one way to use it. For traffic where forgiveness has nothing to rescue, admission control or prefetching might use the same embeddings more effectively. Bigger rethink, keeps the embedding investment.

**D. The multithreading work transfers regardless.** All of it (true-order, log-structured, Stage 1, the applier ordering guarantee) is workload-independent architecture. Whatever regime TARDIS ends up helping in, the concurrency design carries over; it just needs re-baselining at the valid operating point.

Recommended: let the sweep finish, answer the LRUForgive-vs-LRU question per trace, and branch on the result. Everything else waits on that. (Sweep is complete; results and the resulting per-trace operating points are in Section 18.)

---

## 18. CMU-PDL sweep results and per-trace operating-point selection (July 2)

The single-threaded sweep completed across all downloaded traces (LRU, LRUForgive, S3-FIFO, cache sizes 8MB to 8192MB). This section records the results, the operating point selected per trace, and the methodology change the results forced.

### 18.1 Selection rule: max meaningful gap, not the knee

The original instruction was to pick each trace's operating size at the miss-rate knee (where per-doubling miss reduction plateaus). The sweep shows that rule selects the wrong size for this project's question, because on these workloads the LRUForgive-over-LRU gap is a **pre-knee, high-pressure phenomenon**: the gap has already decayed to near zero by the time the miss rate plateaus. Picking the knee therefore hands back a zero-gap size and makes the multithreaded comparison meaningless.

The corrected rule: **for each trace, the operating size is where the LRU-vs-LRUForgive gap is largest, subject to a meaningfulness floor.** A gap counts only if it is clearly above the trace's own noise (floor ~+0.5pp) AND sits in a smooth run of positive gaps rather than as a lone spike. Below the floor, the honest entry is "no meaningful gap; forgiveness does not engage," which is itself a characterization result. At the selected size, all three policies (LRU, LRUForgive, S3-FIFO) are reported.

### 18.2 Per-trace operating points

| Trace | Operating size | LRU | LRUForgive | S3-FIFO | LF-over-LRU | Ordering |
|-------|----------------|-----|------------|---------|-------------|----------|
| msr_proj_0 | 256MB | 0.5062 | 0.4442 | 0.4216 | +6.20pp | LRU -> LF -> S3F (clean) |
| cache-t-00 | 512MB | 0.8325 | 0.7763 | 0.7750 | +5.62pp | LRU -> LF ~= S3F |
| cluster53 | 128MB | 0.2618 | 0.2426 | 0.2359 | +1.92pp | LRU -> LF -> S3F (clean) |
| cluster50 | 256MB | 0.4748 | 0.4575 | 0.4459 | +1.73pp | LRU -> LF -> S3F (noisy) |
| metaKV 202206 | 64MB | 0.2525 | 0.2482 | 0.2103 | +0.43pp | below floor |
| metaKV 202210 | none | | | | +0.07pp max | below floor |
| cluster45 | none | | | | ~0 | below floor |
| cluster26 | none | | | | 0.00 everywhere | below floor |
| cluster10 | none | | | | 0.00 | one-hit-wonder control |

**Four traces have a genuine operating point** (msr_proj_0, cache-t-00, cluster53, cluster50); five do not. That split is the finding, not a failure to find a universal size.

### 18.3 Two structural observations

**1. The operating size varies per trace and is not fixed.** 128MB (cluster53), 256MB (msr, cluster50), 512MB (cache-t-00). This is expected under the two-condition model (Section 16b): the max-gap size tracks each trace's working-set-to-cache pressure point, which differs by trace. This is why a single global cache size would have been the wrong methodology, and it is the direct justification for a per-trace operating-point selection.

**2. The max-gap size is systematically below each trace's knee.** On traces that reach a plateau, the gap has already decayed by the time miss stabilizes: msr's +6.20pp at 256MB shrinks to +0.01pp at 4096MB; cluster53's +1.92pp at 128MB is gone by 1024MB. Forgiveness benefit is anti-correlated with reaching the miss-rate knee, because both the benefit and the knee are governed by eviction pressure in opposite directions (benefit needs pressure; the knee is where pressure ends). This restates Section 16/16b per-trace on real KV and storage traffic, and is the clean characterization contribution.

### 18.4 The knee is a red herring: cluster26

cluster26 has the cleanest knee in the sweep (sharp transition at 64MB, 0.9998 -> 0.0904, flat 0.0780 above 256MB) and was previously flagged as the best multithreaded candidate on that basis. The sweep shows LRU, LRUForgive, and S3-FIFO are **identical at every size** (gap exactly 0.00 throughout). A sharp knee means the working set fits abruptly once the cache is large enough: below the knee nothing helps, above it LRU already retains everything reusable, and there is no intermediate high-pressure regime for forgiveness to operate in. A clean knee is not evidence of a policy gap; it is closer to the opposite. The multithreaded pick must be driven by the policy gap, not by curve shape.

### 18.5 S3-FIFO comparison at the operating points

At all four operating points the predicted ordering LRU -> LRUForgive -> S3-FIFO holds. S3-FIFO is best or tied everywhere: it beats LRUForgive by 2.26pp on msr, 0.67pp on cluster53, 1.16pp on cluster50, and is a near-tie on cache-t-00 (0.7750 vs 0.7763). Nowhere does LRUForgive clear S3-FIFO at a size where it also meaningfully beats LRU. This is the intended result: forgiveness has only been added to the weaker base policy. It is the concrete motivation for the S3-FIFOForgive phase, whose question is whether forgiveness on the stronger base beats plain S3-FIFO.

### 18.6 Implications for the multithreaded phase

All four operating points are valid for the single-threaded characterization table. They are not equally useful for the multithreaded experiments, because the gap shrinks under concurrency (Section 14b: cache-t-00's gap decays 6.9pp -> 4.8pp from t=1 to t=16). The two large-gap points survive that decay with signal to spare:
- **msr_proj_0 at 256MB (+6.20pp)** and **cache-t-00 at 512MB (+5.62pp)** are the primary multithreaded targets.
- cluster53 (+1.92pp) and cluster50 (+1.73pp) may have their gap eaten entirely by concurrency, making the MT result ambiguous; run them for completeness but do not rely on them for the headline MT comparison.

msr_proj_0 is the strongest single result in the sweep and is a different workload class (MSR storage, not CDN or KV), which broadens the characterization beyond the Wikipedia traces.

### 18.7 Data-quality items (do not change the selection, but fix before publishing)

- Several S3-FIFO runs returned `nan` (failed): cache-t-00 at 128/256/2048MB, cluster45 at 128/256MB, cluster10 at 16MB. Refill these, especially cache-t-00, where the failed cells sit right in the interesting region and leave the headline Wikipedia trace missing its S3-FIFO comparison around the operating point. (Root cause later found: the S3-FIFO artifact defect, Section 23; nan/failures cluster in the high-eviction-pressure regime.)
- The msr_proj_0 +6.20pp peak should get one confirmation run before it anchors the multithreaded phase. S3-FIFO is valid there (0.4216, not nan), so the three-way comparison stands; this is a reproducibility check on the headline number, not a gap in the data.

### 18.8 Consolidated operating-point summary (the deliverable)

This is the single-reference table: per trace, the cache size to test at (the max-meaningful-gap size), the three-policy miss ratios there, and the multithreaded designation. It supersedes the scattered mentions above. (Itself superseded by Section 20.5, which adds the MSR family.)

| Trace | Test size | LRU | LRUForgive | S3-FIFO | LF-over-LRU | MT use |
|-------|-----------|-----|------------|---------|-------------|--------|
| msr_proj_0 | 256MB | 0.5062 | 0.4442 | 0.4216 | +6.20pp | primary |
| cache-t-00 | 512MB | 0.8325 | 0.7763 | 0.7750 | +5.62pp | primary |
| cluster53 | 128MB | 0.2618 | 0.2426 | 0.2359 | +1.92pp | secondary |
| cluster50 | 256MB | 0.4748 | 0.4575 | 0.4459 | +1.73pp | secondary (noisy) |
| metaKV 202206 | 64MB | 0.2525 | 0.2482 | 0.2103 | +0.43pp | borderline, below floor |
| metaKV 202210 | none | | | | +0.07pp | no gap |
| cluster45 | none | | | | ~0 | no gap |
| cluster26 | none | | | | 0.00 | no gap (clean knee, red herring) |
| cluster10 | none | | | | 0.00 | no gap (one-hit-wonder control) |

Reading rules for the table:
- **Test size** is the max-meaningful-gap size, not the knee. It is the size at which single-threaded LRU / LRUForgive / S3-FIFO are compared for that trace, and the size at which the multithreaded experiments run for that trace.
- **Four traces have a genuine operating point** (msr_proj_0, cache-t-00, cluster53, cluster50). **metaKV 202206 is a judgment call**: its peak (+0.43pp at 64MB) sits just under the ~0.5pp floor, but it is a smooth positive bump (0.21 / 0.43 / 0.42 at 32 / 64 / 128MB) rather than noise, so it is defensible as a marginal-benefit KV example if broader KV coverage is wanted. The other four "none" traces are genuinely flat and are not operating points.
- **MT primary** (msr_proj_0, cache-t-00) are the two whose gaps survive the concurrency decay; the multithreaded headline comparison rests on these. **MT secondary** (cluster53, cluster50) get single-threaded rows and MT runs for completeness, but their smaller gaps may vanish under threading.

---

## 19. Bloom filter: one-hit-wonder admission design

This section designs the Bloom-filter one-hit-wonder filter (Jane's next-step throughput lever). The build is staged and is gated on the sweep having produced an operating point with a benefit (Section 18: it did, msr_proj_0 at 256MB and cache-t-00 at 512MB).

### 19.1 Hypothesis and grounding

The hypothesis from the July 2 meeting: one-hit-wonders (objects accessed exactly once, never reused) dominate the applier's load, and this is the same workload property that made merge find nothing to collapse (singletons never merge, Section 14c). If so, filtering them out is the primary throughput lever.

A one-hit-wonder cannot be forgiven regardless, because forgiveness requires access count >= minAccessCount (=2). So every embedding computation spent on a one-hit-wonder is pure waste: it can never influence a forgiveness decision through its own eviction. This is what makes the filter safe to reason about: it removes work that by construction cannot affect the forgiveness outcome for the filtered object itself.

**Stage 0 (grounding, cheap, do first):** the S3-FIFO artifact ships `scripts/plot_one_hit_trace.py`, which computes the one-hit-wonder ratio for a trace directly. Run it on the two operating-point traces (msr_proj_0, cache-t-00) to confirm the one-hit-wonder ratio is actually high before building anything. If the ratio is low on these traces, the filter's throughput ceiling is low and the priority drops. Grounding the hypothesis on the exact traces we will optimize is a few minutes of work and prevents building a lever with no payoff.

### 19.2 What each access does, and what the filter should gate

Each applied access performs three operations:
1. **Embedding update** (the expensive part: 64-double arithmetic, the applier's compute bottleneck).
2. **Recency-buffer write** (writes the object into the 16-slot recent buffer).
3. **Context-advance accounting** (every 10th global access advances the context vector).

The filter's target is operation 1 for first-sighting objects: skip the embedding update until an object is proven a repeat. Operation 2 is a design knob with a likely bonus: skipping the recency-buffer write for one-hit-wonders keeps never-recurring objects out of the recency signal, which may **denoise** forgiveness and improve the miss ratio, not just throughput. Operation 3 should be left alone: the context advance is a global clock, and skipping one-hit-wonders from its counter would shift the context walk for every object's embedding. Keep counting all accesses toward the context advance; only gate operations 1 and (optionally) 2.

### 19.3 Key realization: the applier already knows, exactly, for free

The access-count map is maintained by the applier itself, in trace order. So at the moment the applier processes an event for object X, it can check the count: `count[X] == 0` means this is X's first application (a provisional one-hit-wonder), `count[X] >= 1` means X is a proven repeat. This is exact, in-order, and has zero false positives, and it requires no Bloom filter at all.

Consequence: the applier-side version of one-hit-wonder filtering needs no new data structure. In the applier, when processing X: if `count[X] == 0`, increment the count (and optionally skip the recency write), but skip the embedding update; if `count[X] >= 1`, do the full update. This directly removes embedding compute (the actual bottleneck) with no approximation error.

So the Bloom filter is not needed to skip embedding compute. It is needed only for the hot path, below.

### 19.4 Where the Bloom filter is actually required: the hot path

Applier-side skipping (19.3) cuts the applier's compute, but the one-hit-wonder events are still appended by the hot path and still dequeued by the applier (it just skips their math). If append and dequeue volume is itself a cost (per-thread buffer pressure, memory traffic, dequeue overhead), the filter has to move to the hot path, before the append.

The hot path cannot cheaply consult the count map: the map is lazily maintained by the applier, so it is not eagerly current at enqueue time, and reading it from every worker thread reintroduces exactly the contention the log-structured design removed. What the hot path needs is a cheap, concurrent, approximate "have I seen this object before?" oracle. That is precisely a Bloom filter (used as a doorkeeper).

**Hot-path logic:** `if (seen.test(obj_id)) append(event); else { seen.set(obj_id); /* skip append */ }`. First sighting is recorded and dropped; subsequent sightings pass through to the applier.

**Concurrency:** the Bloom filter is a bit array with k hash functions. `set` is a lock-free atomic `fetch_or` on k words; `test` is k atomic loads. No lock on the hot path, consistent with the design's lock-free-append principle. Two threads first-seeing the same object concurrently may both drop it, so the object is admitted on its third access rather than second; this is an accepted approximation.

### 19.5 Bloom filter specifics

**False positives are safe, in a useful direction.** A false positive means the filter reports "seen" for an object that is actually new, so a true one-hit-wonder passes through and gets embedded. The cost is doing the wasted work the filter was meant to avoid, occasionally. A false positive never causes incorrect forgiveness; it only erodes the throughput benefit. Bloom filters have no false negatives (a genuinely-recorded object is never reported absent), so a proven repeat is never wrongly dropped, except through deliberate reset (below).

**Saturation over long traces.** A plain Bloom filter fills up: after enough distinct objects its false-positive rate approaches 1 and it stops filtering. On the long traces (cache-t-00, and any 100M+ request trace) this is real. Two mitigations:
- **Size for the distinct-object count.** Standard sizing: `m = -(n ln p) / (ln 2)^2` bits and `k = (m/n) ln 2` hash functions, for n distinct objects and target FP rate p. At p=0.01, m/n ~= 9.6 bits and k ~= 7. For n=10M distinct objects that is ~12MB, cheap. Get n by counting distinct obj_id in the trace (or from the cache footprint reported at the operating size).
- **Doorkeeper reset (TinyLFU style).** Periodically clear the filter (or rotate between two filters) so its fill, and therefore its FP rate, stays bounded as the working set turns over. A reset forgets past sightings, so an object seen long ago is treated as new again and its next access is dropped once; this is a bounded, rare miss-ratio perturbation. Reset cadence ties to the distinct-object turnover window.

Recommendation: start with a sized plain filter for the operating-point traces (n is known there and the traces are bounded), and add doorkeeper reset only if extending to very long traces where a single filter cannot stay under the target FP rate.

### 19.6 Relationship to minAccessCount

The two thresholds are complementary and aligned. `minAccessCount=2` gates **forgiveness** (an object is not forgiven until its 2nd access). The filter gates **embedding work** (an object is not embedded until its 2nd sighting). Embedding an object before it is forgiveness-eligible is wasted work, so the filter simply makes the embedding pipeline lazy up to the forgiveness-eligibility threshold. For minAccessCount=2 the "admit on 2nd access" plain Bloom filter is exactly right. If minAccessCount were larger, the filter would generalize to a small counting structure (counting Bloom or count-min sketch) that admits on the k-th sighting; note this generalization, but build the plain second-access filter first since minAccessCount is 2.

### 19.7 Two effects, measured separately

- **Throughput:** applier events/sec and end-to-end MQPS, before vs after, at the operating points. Expected win scales with the one-hit-wonder ratio measured in Stage 0.
- **Miss ratio:** could improve (denoising, if the recency-write skip removes noise) or degrade (if first-access signal mattered). Hypothesis: neutral to positive. Measure at msr_proj_0 256MB and cache-t-00 512MB, single-threaded first (isolates the filter's effect from concurrency), then multithreaded.

Run an ablation on the recency-write skip (operation 2 on vs off) to separate the denoising effect from the pure compute-skip effect, so the miss-ratio change is attributable.

### 19.8 Staged build plan

1. **Stage 0:** run `plot_one_hit_trace.py` on msr_proj_0 and cache-t-00. Confirm the one-hit-wonder ratio is high. Gate the rest on this.
2. **Stage A (no Bloom filter):** applier-side skip via the count map (19.3). Exact, zero new structure. Measure throughput and miss ratio at both operating points, single-threaded then multithreaded. This alone may capture most of the compute win, because the applier compute is the bottleneck.
3. **Stage B (Bloom filter):** only if append/dequeue volume is still a cost after Stage A. Add the concurrent hot-path doorkeeper (19.4, 19.5) to cut appends before they enter the buffers. Measure the incremental gain over Stage A.
4. **Ablation:** recency-write skip on vs off (19.7), to attribute the miss-ratio effect.

The staging matters: Stage A is where the compute win is and it needs no Bloom filter, so build and measure it first. Stage B (the actual Bloom filter) is justified only if the remaining bottleneck after Stage A is append/dequeue volume rather than compute. Do not build the Bloom filter before Stage A shows it is needed.

### 19.9 Open questions

- Should one-hit-wonders count toward the context advance? Leaning yes (context is a global clock; excluding them perturbs all embeddings). Worth a quick on/off check.
- Sized plain filter vs doorkeeper reset: decided by the distinct-object count at each operating point. Measure n first.
- Does the recency-write skip help or hurt the miss ratio? Empirical, via the 19.7 ablation. This is the denoising hypothesis and is the most interesting possible result, because it would mean the one-hit-wonder filter improves both axes at once.

### 19.10 Stage 0 result (July 13): the throughput hypothesis is falsified; the value shifts to miss ratio

Stage 0 was run with a standalone counter on the two operating-point traces. The result reframes the whole section, so it is recorded here in full.

| Trace | requests | distinct objects | one-hit-wonders | OHW / distinct objects | OHW / requests |
|-------|----------|------------------|-----------------|------------------------|----------------|
| msr_proj_0 | 4,224,524 | 286,228 | 108,232 | 37.8% | **2.6%** |
| cache-t-00 | 9,826,681 | 2,768,652 | 1,756,376 | 63.4% | **17.9%** |

**The throughput ceiling is the request share, and it is tiny on the trace that matters.** The applier processes requests (events), not objects, so the fraction of applier load a one-hit-wonder filter can remove is OHW / requests, not OHW / distinct objects. On msr_proj_0 that ceiling is 2.6%: even a perfect, zero-overhead filter removes at most 2.6% of applier work. That is not a throughput lever. On cache-t-00 the ceiling is 17.9%, a real but modest 1.2x on applier load.

**Why the hypothesis was wrong.** The object-population figures (37.8%, 63.4%) are large and were the intuition behind Jane's hypothesis, but a one-hit-wonder contributes exactly one event while a hot object contributes many. Singletons dominate the object population and are a negligible fraction of the request stream. The applier is flooded by hot-object repeats, not by singletons. This is consistent with the Stage 1 finding that batching did nothing (the applier is compute-bound on hot-object arithmetic, Section 14c).

**This corrects the merge diagnosis in Section 14c.** An earlier draft attributed the near-1.0 merge ratio to the traces being mostly singletons. That is false on msr_proj_0: 97.4% of requests hit objects that recur. Merge found nothing to collapse not because repeats are absent but because repeats are too far apart in the stream to fall in the same window: high reuse count, long reuse distance. The implied fix is therefore a larger or reuse-distance-aware merge window, not one-hit-wonder filtering. (See the corrected note in Section 14c.)

**Consequence for the build.** The Bloom filter is falsified as a throughput lever on the primary operating point. It is still worth building, but the justification moves off throughput and onto two other things:
1. **Miss ratio via denoising (untested, plausible).** 37.8% of the object population never recurs, yet each such object still gets an embedding, a recency-buffer write, and a slot in the MCACHE store (capped at 5x cache items). Filtering singletons may sharpen the forgiveness signal. This is a miss-ratio hypothesis and is testable cheaply via Stage A (no Bloom filter needed).
2. **cache-t-00 has a real 17.9% ceiling.** If a throughput demonstration is wanted, cache-t-00 is where the filter does measurable work.

**Revised plan (supersedes the throughput framing of 19.1-19.8):** build Stage A (applier-side count-map skip, Section 19.3) first and measure BOTH axes at both operating points. Stage A is the cheap, exact way to test the denoising idea. If denoising shows a miss-ratio improvement, Stage B (the hot-path Bloom filter) is then justified to move that filtering off the applier, and we already know the throughput delta to expect (about 2.6% on msr, about 15-18% on cache-t-00), so Stage B confirms the model rather than hoping for a surprise. If Stage A shows no miss-ratio benefit either, the honest outcome is a documented negative result on both axes, and the roadmap moves to update subsampling (the compute-scaling lever that shrinks with hot-object load, unlike the filter).

### 19.11 Full-family OHW measurement (July 13): the ratio varies wildly and may predict the gap

The two-trace Stage 0 was extended to the whole MSR family plus the cluster operating points. The vectorized counter was cross-validated against the original on msr_proj_0 (37.8% / 2.6%) and cache-t-00 (63.4% / 17.9%), exact matches, so the numbers below are trusted.

| Trace | OHW / requests | OHW / distinct objects |
|-------|----------------|------------------------|
| msr_usr_2 | 64.4% | 92.2% |
| msr_proj_1 | 59.7% | 91.3% |
| msr_proj_4 | 37.1% | 80.0% |
| msr_proj_2 | 34.2% | 61.8% |
| msr_usr_1 | 25.9% | 83.9% |
| cache-t-00 | 17.9% | 63.4% |
| msr_prn_1 | 13.2% | 68.3% |
| msr_web_2 | 6.5% | 25.5% |
| msr_src1_0 | 6.2% | 41.1% |
| msr_prn_0 | 5.5% | 42.8% |
| msr_hm_0 | 3.5% | 31.6% |
| msr_proj_0 | 2.6% | 37.8% |
| msr_src1_1 | 1.2% | 9.2% |
| cluster53 | 0.3% | 9.5% |
| msr_prxy_1 | 0.2% | 75.4% |
| cluster50 | 0.1% | 6.4% |
| msr_prxy_0 | 0.1% | 9.2% |

**The throughput ceiling is trace-dependent, not uniformly low.** The request-share OHW spans 0.1% to 64.4%. So the "Bloom filter cannot be a throughput lever" conclusion from 19.10 is specific to msr_proj_0, which sits near the bottom at 2.6%. On msr_usr_2 and msr_proj_1 singletons are the majority of the request stream, where a one-hit-wonder filter could shed 60% of applier load. The throughput hypothesis is not dead; it is workload-dependent and happens to be weak on the trace we chose as the primary operating point.

**msr_proj_0 is not representative.** It has one of the lowest OHW request-shares in the family and, from the first sweep, the largest LRUForgive gap (+6.20pp). This suggests an anti-correlation worth stating as a hypothesis:

**Hypothesis (untested): OHW request-share is inversely related to the LRUForgive-over-LRU gap.** A stream dominated by never-repeating objects has little co-occurrence structure to learn, so forgiveness should help least exactly where OHW is highest. This is the two-condition model (Section 16b) measured a second way: OHW request-share is a proxy for the absence of the temporal-correlation condition. If it holds, the story is clean: forgiveness helps when the workload is NOT one-hit-wonder-dominated.

**Status: not yet checkable.** The sweep runs baselines first (Pass 1: lru, s3fifo) then TARDIS last (Pass 2), so as of this writing the high-OHW MSR traces (proj_1, proj_2, usr_1, usr_2) have LRU curves but no LRUForgive numbers. The LRU curves alone are consistent with the prediction: the high-OHW traces (proj_1 at 60%, proj_2 at 34%) sit pinned at 0.92-0.99 miss across the whole ladder, the signature of one-hit-wonder-dominated traffic where no policy can do much. But this is LRU-shape evidence, not the gap. Confirm once Pass 2 reaches these traces: plot LRUForgive-over-LRU gap against OHW request-share across the family. (Confirmed in the one-directional form, Section 20.2.)

**The prxy traces are the informative extreme.** msr_prxy_1 has 75.4% of objects as singletons but only 0.2% of requests: a tiny hot set absorbs essentially all 168M requests. High reuse concentration, near-zero singleton traffic. If forgiveness needs reuse structure, prxy is where it should either help or show the pressure-without-correlation pattern. Worth watching in Pass 2. (It became the largest confirmed gap, Section 20.3, but is publishing-blocked, Section 25.6.)

**Consequence for Stage A design.** The OHW spread gives the denoising experiment a natural independent variable. Rather than testing embed_threshold on msr_proj_0 alone (lowest OHW, least to denoise), test it across traces spanning the OHW range: msr_proj_0 (2.6%) and a high-OHW trace such as msr_proj_1 (59.7%) or msr_usr_1 (25.9%). The denoising prediction is that raising the embedding-admission threshold helps more where OHW is higher, because that is where the never-recurring pollution is worst. This turns Stage A into a test of the denoising mechanism itself, not a single-point check.

### 19.12 Stage A results (July 13): miss-ratio null; thresholds deliver the throughput goal without a Bloom filter

The embed_threshold_ knob was implemented (decoupled from min_access_count_, default preserving baseline exactly; k=2 reproduced the banked msr_proj_0 number, 0.4443 vs 0.4442). The k-sweep was run on msr_usr_2 @64MB (64% OHW, the denoising design point) and msr_proj_0 @256MB (2.6% OHW, control):

| embedThreshold | proj_0 @256MB | usr_2 @64MB |
|----------------|----------------|--------------|
| 2 (baseline) | 0.4442 | 0.8870 |
| 3 | 0.4439 | 0.8869 |
| 4 | 0.4440 | 0.8868 |
| 12 | 0.4413, 0.4421, 0.4446, 0.4484 (4 runs) | 0.8868 |
| 32 | 0.4420 | 0.8868 |

**Miss-ratio verdict: clean null everywhere.** usr_2 is flat at every threshold despite being the trace with the most junk to filter, falsifying the denoising hypothesis at its design point. proj_0's apparent k=12 improvement (single run 0.4413) did not survive repetition: four runs scatter 0.4413-0.4484, mean 0.4441 = baseline. Likely explanation for usr_2: its population is bimodal (singletons vs genuinely hot), with few objects in the 2-11 access band the threshold filters.

**Revised noise floor (amends 20.4; corrected July 18).** proj_0 jitter spans about +/-0.35pp (a 0.71pp range), roughly 7x the +/-0.05pp estimated from duplicate cells. An earlier version of this section speculated that aggressive thresholds increase variance, because the k=12 runs scattered 0.4413-0.4484 while the two k=2 runs agreed to 0.0001. That is now falsified: after Stage A was fully reverted, three runs of the stock baseline binary gave 0.4485 / 0.4440 / 0.4414, the same 0.71pp spread. **The variance is inherent to msr_proj_0 under the log-structured applier, not threshold-induced**; the tight k=2 readings were coincidence. Operational rule: any single-run LRUForgive delta under ~0.5pp on proj_0 is unresolvable; use multi-run means (3 runs gives a standard error of roughly +/-0.2pp). (Further corrected in Section 24.3: the variance is trace-specific, not inherent to the design; cluster53 and cache-t-00 reproduce to 0.0001-0.0003.)

**Unexpected throughput result: raising BOTH thresholds sheds the applier compute on high-OHW traces.** Setting minAccessCount=12 AND embedThreshold=12 together on usr_2: runtime 157s / 0.13 MQPS vs baseline 214-230s / 0.09 MQPS, a 30-44% single-thread speedup, with miss ratio unchanged (0.8872, within noise; the 1.2pp gap over LRU survives). Mechanism: on a 92%-singleton trace nearly no object reaches 12 accesses, so the applier skips the embedding math for almost every event and eviction runs almost no similarity computations. **This is the Bloom filter's throughput objective achieved with zero new machinery**: the count-map check the applier already performs IS the filter. The same setting on proj_0 cost 0.9pp of miss ratio (0.4529, outside the scatter band): raising forgiveness eligibility to 12 excludes legitimately forgivable objects on low-OHW traces. So threshold-raising is a per-workload knob: free compute shedding where OHW is high (where TARDIS has little miss-ratio value anyway), harmful where OHW is low (where the gap lives). Pending: 2 confirmation reruns of the usr_2 throughput number (single run so far; 30%+ exceeds normal node wobble but should be repeated).

**Stage B (hot-path Bloom filter) recommendation: close the line.** The miss-ratio case is falsified. The throughput case is delivered by the thresholds without building anything. The only work a hot-path Bloom filter uniquely removes beyond this is enqueue/dequeue volume (events still enter the per-thread buffers under the threshold approach), a second-order cost on a system whose applier is compute-bound (14c), and that compute is now skippable. The Bloom-filter thread of work closes with: the OHW screening result (20.2), the request-vs-object-share correction (19.10), and the thresholds-as-filter throughput result above. The next throughput lever is update subsampling, which the threshold result makes more plausible (the embedding signal tolerates sparse admission; sparse updates may follow), or proceeding directly to the multithreaded phase at the confirmed operating points (20.5).

**Code state:** embed_threshold_ machinery is committed on tardis-implementation with defaults reverted to 2/2, so baseline behavior is unchanged and the knob remains available.

---

## 20. MSR-family sweep results (July 13): the gap generalizes to low-OHW traces; prxy_1 is the new headline

The second sweep (13 additional MSR traces, TARDIS capped at 1024MB) completed. Three findings, plus a data-integrity note and a methodological discovery.

### 20.1 Data integrity: concurrent sweep instances contaminated the CSV

Two sweep instances ran concurrently at some point (evidenced by duplicate (trace, algo, size) rows and doubled log lines). A conflict check found 12 duplicated lruforgive cells with disagreeing values, 11 of them in the fourth decimal and one large: msr_prxy_1 at 256MB (0.1606 vs 0.1633). The CSV was deduplicated (first occurrence kept, backup at sweep_results.csv.bak). Notably ALL conflicts were lruforgive rows, none lru or s3fifo, which exposed a real property of the system (20.4).

### 20.2 The OHW hypothesis confirmed in the one-directional form

Max LRUForgive-over-LRU gap per trace against OHW request-share (from 19.11):

| Trace | OHW/req | Max LF-over-LRU gap | At size |
|-------|---------|---------------------|---------|
| msr_usr_2 | 64.4% | 1.25pp | 64MB |
| msr_proj_1 | 59.7% | 0.13pp | |
| msr_proj_4 | 37.1% | 0.16pp | |
| msr_proj_2 | 34.2% | 0.05pp | |
| msr_usr_1 | 25.9% | 0.86pp | 512MB |
| cache-t-00 | 17.9% | 5.62pp | 512MB |
| msr_prn_1 | 13.2% | 1.12pp | 1024MB |
| msr_web_2 | 6.5% | ~0 | |
| msr_src1_0 | 6.2% | 0.12pp | |
| msr_prn_0 | 5.5% | 0.35pp | |
| msr_hm_0 | 3.5% | 1.23pp | 128MB |
| msr_proj_0 | 2.6% | 6.20pp | 256MB |
| msr_src1_1 | 1.2% | 0.17pp | |
| msr_prxy_1 | 0.2% | ~8pp confirmed (20.3) | 256MB |
| msr_prxy_0 | 0.1% | 0.24pp | |

**High OHW reliably kills the gap:** every trace above 25% OHW request-share has a gap of at most 1.25pp; every gap above 5pp sits below 18% OHW. **Low OHW permits but does not guarantee a gap:** prxy_0, src1_1, and web_2 are low-OHW and flat. So OHW request-share is a valid one-directional screen: a five-minute OHW count can rule TARDIS out (high OHW), but ruling it in requires the sweep.

**Why low-OHW traces can still be flat: the three-condition refinement.** Low OHW only establishes that reuse exists. The gap additionally requires (a) eviction pressure (cache smaller than working set) and (b) reuse distances near the cache boundary, where a rescue changes the outcome. The two flat low-OHW traces each fail a different one: msr_prxy_0 fails pressure (its hot set is tiny; miss falls to 1.2% by 1024MB, LRU barely errs, nothing to rescue), and msr_src1_1 fails rescuable distance (miss pinned at 0.87-0.99 across the whole ladder; reuse exists but at distances far beyond any tested cache, so rescued objects would not return in time regardless). This refines the two-condition model of 16b into: reuse exists (low OHW) AND eviction pressure AND rescuable reuse distances.

### 20.3 msr_prxy_1 at 256MB: the largest confirmed gap, but S3-FIFO clearly wins

The deduplicated sweep showed LRU 0.2846 / LRUForgive 0.1606 / S3-FIFO 0.1621 at 256MB, a +12.4pp gap with LRUForgive nominally beating S3-FIFO. Because this cell sat on a cliff (LRU falls 0.6437 -> 0.2846 -> 0.0032 across 128/256/512MB) and was one of the contaminated cells, it was re-run 3x per policy:

| Run | LRU | LRUForgive | S3-FIFO |
|-----|-----|------------|---------|
| 1 | 0.2758 | 0.1969 | 0.1095 |
| 2 | 0.2758 | 0.1956 | 0.1090 |
| 3 | 0.2844 | 0.1960 | 0.1163 |

**Confirmed result: LRUForgive-over-LRU is approximately +8.0 to +8.8pp**, the largest gap in the project (vs msr_proj_0's +6.20pp). **The sweep's apparent S3-FIFO tie was an artifact of the contaminated run: S3-FIFO clearly wins here (~0.11 vs ~0.196, about 8.5pp better than LRUForgive).** The expected ordering LRU -> LRUForgive -> S3-FIFO holds, emphatically.

The confirmation also revealed that even plain LRU varies across runs at this operating point (0.2758 twice, 0.2844 once), which should not happen for deterministic code and indicates some nondeterminism in the harness that the cliff amplifies. LRUForgive was the most stable of the three (0.1956-0.1969). All prxy_1 numbers should carry error bars from repeated runs; single-run values at this operating point are not trustworthy to better than ~1pp. (This became the open prxy_1 blocker: LRU gives three reproducible-within-session values across sessions, 0.2846 / 0.2758 / 0.3574, unexplained. Nothing from prxy_1 publishes until resolved. See Section 25.6.)

### 20.4 Methodological note: LRUForgive has inherent run-to-run jitter

All 12 duplicate-cell conflicts were lruforgive rows; zero were lru or s3fifo. Mechanism: the log-structured design has a background applier thread, and worker/applier timing varies run to run, so eviction-time forgiveness reads embedding state at slightly different staleness each run, flipping near-threshold decisions. Plain LRU and S3-FIFO have no such thread. Normal jitter is about +/-0.05pp, amplified to +/-0.27pp on prxy_1's cliff. Consequence: single runs cannot resolve LRUForgive gaps below ~0.1pp, and any headline number should be a multi-run mean. (The prxy_1 confirmation shows the harness itself adds further nondeterminism on cliffs, affecting all policies.) (Corrected in Section 24.3: the jitter is trace-specific, concentrated in proj_0 and prxy_1; cluster53 and cache-t-00 reproduce to 0.0001-0.0003.)

### 20.5 Revised operating-point table (supersedes 18.8)

| Trace | Test size | LRU | LRUForgive | S3-FIFO | LF-over-LRU | MT use |
|-------|-----------|-----|------------|---------|-------------|--------|
| msr_prxy_1 | 256MB | ~0.276-0.284 | ~0.196 | ~0.109-0.116 | ~+8.0-8.8pp | primary (3-run confirmed; use multi-run means; PUBLISHING-BLOCKED, 25.6) |
| msr_proj_0 | 256MB | 0.5062 | 0.4442 | 0.4216 | +6.20pp | primary |
| cache-t-00 | 512MB | 0.8325 | 0.7763 | 0.7750 | +5.62pp | primary |
| cluster53 | 128MB | 0.2618 | 0.2426 | 0.2359 | +1.92pp | secondary |
| cluster50 | 256MB | 0.4748 | 0.4575 | 0.4459 | +1.73pp | secondary (noisy) |
| msr_hm_0 | 128MB | 0.4316 | 0.4193 | 0.4046 | +1.23pp | marginal |
| msr_usr_2 | 64MB | 0.8995 | 0.8870 | 0.8890 | +1.25pp | denoising test trace (highest OHW with a gap) |

All other MSR traces: no meaningful gap (below the 0.5pp floor or noise-level). The MSR family answer: the msr_proj_0 gap does NOT generalize to the whole family; it generalizes to the low-OHW, high-pressure, rescuable-distance subset (proj_0, prxy_1, hm_0 marginally). That subset structure IS the characterization result.

**Single-thread throughput at the operating points** (t=1, MQPS; LRU/LRUForgive turbo-off backoff from the Aug 2 run, S3-FIFO from the July run, turbo-on): msr_proj_0 LRU 0.22 / LF 0.16 / S3-FIFO 0.74; cache-t-00 LRU 0.25 / LF 0.15 / S3-FIFO 0.56; cluster53 LRU 0.90 / LF 0.27 / S3-FIFO 1.68; msr_prxy_1 LRU 0.71 / LF 0.35 / S3-FIFO 1.73. LRUForgive's single-thread throughput trails LRU because of the applier's per-event embedding work; the full thread-scaling throughput is in Section 22.5.

(Note: an earlier revision of this document repeated the Stage A embedding-admission-threshold result as a stray, misnumbered "19.12" block at the end of this section. It duplicated the canonical Section 19.12 with no additional data and was removed in the July 28 reconstruction.)

---

## 21. Multithreaded run methodology (settled July 18)

The multithreaded phase re-runs the three-policy comparison at the confirmed operating points (20.5) across thread counts 1/2/4/8/16. Getting the harness configuration right turned out to be non-obvious, and a first attempt produced contaminated results. This section records the settled methodology and why each choice is what it is, because these details are easy to lose and expensive to rediscover.

### 21.1 Shared vs replicated mode, and what that implies for cache scaling

The harness has two modes (Section 6). `cmd.h` documents them as `mode: 0=replicated replay (default), 1=round-robin shared`, parsed from `argv[5]`.

- **Replicated (mode 0):** each thread independently replays the ENTIRE trace. Total work scales with thread count.
- **Shared (mode 1):** threads split the trace by `record_idx % n_thread`, so every record is processed exactly once. Total work is constant regardless of thread count.

**`run.sh` is replicated.** It passes only four arguments (`./_build/lru trace $sz $hp ${nThread}`), so `mode` defaults to 0. This explains its cache-scaling line, `sz = sz_base * nThread`: in replicated mode each thread replays the whole trace, so keeping per-thread cache pressure constant requires scaling total cache with thread count. That is correct FOR REPLICATED MODE.

**Our operating-point runs are shared mode**, because the banked log-structured curve we are comparing against (Section 14b, gap 6.90/9.74/7.00/4.84 at t=1/4/8/16) was measured in shared mode. Shared mode is also the production-realistic model (many workers serving one request stream).

**Therefore cache size is held FIXED at the operating point, and only hashpower scales.** In shared mode the trace is partitioned, so total work is constant and total cache should be too. (The size argument is total, not per-thread; confirmed in `cache.cpp`, one `setCacheSize`, one cache object shared by all threads, Section 16.) Copying run.sh's `sz * nThread` into a shared-mode run would be wrong twice over: it would multiply capacity while work stayed constant, and for msr_proj_0 it would move the cache from 256MB (the max-gap point) to 4096MB at t=16, where the single-threaded sweep already shows the gap collapsed to ~0.01pp. The gap would then vanish for eviction-pressure reasons, not threading reasons, and the experiment would measure the wrong thing.

**Hashpower does scale**, `hp = hp_base + log2(threads)`, following run.sh. This is independent of mode: more threads means more concurrent inserts and more bucket contention, so the hash table is sized up accordingly. A first attempt held hashpower fixed across thread counts, which added hash-table pressure at high thread counts and inflated the absolute miss ratios of all policies; those results were discarded.

### 21.2 Environment requirements

- **numactl pinning** (`numactl --cpunodebind=0 --membind=0`), following run.sh, for consistent memory locality on this two-socket box. It is NOT installed by default on a fresh CloudLab image (`sudo apt-get install -y numactl`). The run script treats it as optional and reports which mode it is in, but throughput numbers are noisier without it, which matters because single-applier throughput is itself a deliverable.
- **Turbo boost disabled** (`mybench/turboboost.sh disable`) before any timing run, per the artifact's own setup, for consistent throughput.

### 21.3 Repetition and failure handling

- **lruforgive runs 3x per cell; lru and s3fifo run 1x.** LRUForgive has inherent run-to-run jitter from applier-timing staleness (20.4, 19.12): about +/-0.35pp on msr_proj_0, larger on cliffs. The deterministic baselines showed zero disagreement across duplicate cells. All reported LRUForgive numbers are means of 3. (Refined by 24.3: only proj_0 and prxy_1 actually need the 3x; cluster53 and cache-t-00 are single-run-trustworthy.)
- **s3fifo is unstable at t>=4 in shared mode.** It produced blank cells at cache-t-00 t=4/16 and cluster53 t=8/16, and in one case HUNG indefinitely on msr_prxy_1 at t=4, burning about 50 CPU-hours before it was noticed. The run script therefore wraps every invocation in `timeout` and reaps orphaned processes. This is an artifact-baseline limitation, not a defect in our code, but it means some S3-FIFO reference cells may be missing at high thread counts. (Diagnosed fully in Section 23: it is a concurrency defect in the artifact's S3-FIFO.)
- **Resume logic must require a numeric result.** An early version treated any row for a cell as "done", so `FAIL` rows (written when numactl was missing and every invocation errored out) permanently blocked retries and a rerun silently completed in seconds having done nothing. The guard now matches only rows whose miss-ratio field starts with a digit.

### 21.4 Validation anchor

Every multithreaded batch is validated at t=1 against the banked single-threaded operating-point numbers before the rest of the curve is trusted. For msr_proj_0 @256MB hp=24: LRU must reproduce 0.5061 and S3-FIFO 0.4216, with LRUForgive averaging about 0.4442. A batch that fails this anchor is discarded rather than interpreted. (The July 28 optimization re-run validated all three t=1 LRU anchors exactly: proj_0 0.5061, cache-t-00 0.8325, cluster53 0.2618. See Section 25.4.)

---

## 22. Multithreaded results at the operating points (July 18)

Full batch: 4 traces x 5 thread counts x (1 lru + 1 s3fifo + 3 lruforgive), shared mode, fixed cache size, scaled hashpower, numactl pinned, per the Section 21 methodology. All four t=1 anchors validated against banked single-threaded values before interpretation (21.4).

### 22.1 The improvement gap versus thread count

Gap = LRU miss ratio minus the mean of 3 LRUForgive runs, in percentage points.

| Threads | msr_proj_0 @256MB | cache-t-00 @512MB | cluster53 @128MB | msr_prxy_1 @256MB |
|---------|-------------------|-------------------|------------------|-------------------|
| 1 | 6.47 | 5.65 | 1.92 | 7.99 (see 22.3) |
| 2 | 6.91 | 5.66 | 1.93 | 12.16 |
| 4 | 5.30 | 4.94 | 2.33 | 11.84 |
| 8 | 4.13 | 3.89 | **2.88** | **15.71** |
| 16 | 2.00 | 2.82 | 2.06 | 9.69 |

**The central finding: degradation under concurrency is NOT universal.** Two traces decay monotonically after t=2 (msr_proj_0 6.47 -> 2.00, a 69% erosion; cache-t-00 5.65 -> 2.82, 50%). Two traces IMPROVE with thread count, peaking at t=8 before falling back (cluster53 1.92 -> 2.88; msr_prxy_1 7.99 -> 15.71, roughly double its single-threaded gap). This was not predicted by any prior hypothesis and supersedes the working assumption (from the cache-t-00-only measurements of Section 14b) that the gap always erodes with threads. (The July 28 optimization re-run reproduced all of these shapes; see Section 25.4.)

**Candidate explanation, untested.** The correlate that fits is working-set size and concentration. The two decaying traces have the smallest working sets (msr_proj_0: 286K distinct objects; cache-t-00: 2.77M). The two improving traces have either the largest (cluster53: 7.26M) or the most concentrated (msr_prxy_1: 390K objects absorbing 168M requests, OHW request-share 0.2%). This is consistent with the recency-buffer dilution hypothesis (Section 14b caveat 1): with a fixed 16-slot buffer written by N threads at scattered trace positions, dilution should corrupt the forgiveness signal fastest when the working set is small relative to the buffer's coverage. Why concurrency would actively HELP on the other two is not explained by dilution and remains open; one possibility is that shared-mode reordering raises absolute miss ratios (it does: LRU rises on every trace) and thereby creates more bad LRU decisions for forgiveness to correct, an effect that would dominate wherever dilution is weak. Section 14b caveat 2 raised the same mechanism to explain t=4/t=8 gaps exceeding t=1 on cache-t-00. (Resolved to a single tug-of-war mechanism in Section 24.1.)

**Note the earlier contaminated run overstated the decay.** A first attempt held hashpower fixed across thread counts, adding hash-table pressure at high thread counts. It reported msr_proj_0 decaying 6.26 -> 0.96pp; with correctly scaled hashpower the same trace decays 6.47 -> 2.00pp. Those results were discarded (21.1).

### 22.2 Throughput

LRUForgive throughput (MQPS), means across runs, against LRU on the same cell:

| Trace | LF t=1 | LF t=16 | LF scaling | LRU t=16 |
|-------|--------|---------|------------|----------|
| msr_proj_0 | 0.06 | 0.24 | 4.0x | 0.89 |
| cache-t-00 | 0.05 | 0.34 | 6.8x | 1.15 |
| cluster53 | 0.10 | 0.34 | 3.4x | 3.06 |
| msr_prxy_1 | 0.13 | 0.17 | **1.3x** | 1.19 |

The single applier remains the bottleneck and it is worst on the largest trace: msr_prxy_1 barely scales at all (1.3x across 16 threads) and actually dips at t=4 and t=8 (0.10, 0.09-0.13) before recovering. cluster53 shows the widest absolute gap, 0.34 vs LRU's 3.06, a 9x throughput penalty at t=16. This quantifies the prize for sharded appliers (Section 15 item 1): LRUForgive is leaving between 3.5x and 9x on the table at 16 threads depending on trace.

Note also that numactl pinning reduced measured throughput relative to the unpinned contaminated run (msr_proj_0 LF t=1 went 0.09-0.10 unpinned to 0.06 pinned). Confining workers plus the applier thread to one socket costs throughput; the pinned numbers are more controlled but may understate what the design achieves unpinned. Worth an explicit pinned-vs-unpinned comparison before any throughput number is published.

### 22.3 msr_prxy_1 at t=1 is BIMODAL, not noisy

The three t=1 LRUForgive runs in the batch were 0.1971, 0.1947, and 0.1594, and the 0.1594 was initially treated as an outlier. Three additional runs were done to settle it: 0.1997, 0.1953, **0.1601**. The low value reproduced, so it is not an outlier.

Pooling all nine t=1 runs at this operating point (three from the Section 20.3 confirmation, three from the MT batch, three from the reruns):

| Mode | Values | Count | Mean | Gap vs LRU 0.2758 |
|------|--------|-------|------|-------------------|
| High | 0.1969, 0.1956, 0.1960, 0.1971, 0.1947, 0.1997, 0.1953 | 7 | 0.1965 | 7.93pp |
| Low | 0.1594, 0.1601 | 2 | 0.1598 | 11.60pp |

**The distribution is bimodal with two stable outcomes about 3.7pp apart**, and single-threaded execution lands in the better mode roughly 22% of the time. Critically, **all three t=2 runs landed in the low (better) mode** (0.1591, 0.1609, 0.1608), as did the t=4 runs in the same neighborhood.

**Interpretation.** This reframes the "concurrency helps prxy_1" result of 22.1. It is not that added threads gradually improve the gap; it is that concurrency reliably reaches a regime that single-threaded execution only reaches occasionally. The likely mechanism is applier staleness: with the log-structured design, eviction-time forgiveness reads embedding state at whatever point the applier has caught up to, and at this operating point the trace sits mid-cliff (LRU falls 0.6437 -> 0.2846 -> 0.0032 across 128/256/512MB), so a small difference in staleness flips a cascade of forgiveness decisions and the run settles into one of two attractors. More threads changes the worker-to-applier production ratio systematically, which would explain why t=2 lands in the low mode every time.

**Reporting rule for this operating point:** report the bimodality explicitly rather than a mean. A mean of nine runs (0.1884, gap 8.74pp) describes neither mode and would be misleading.

**This is a lead for the open question in 22.1.** If concurrency-induced staleness is what selects the mode, the same mechanism may explain why cluster53 also improves with threads, and it is directly testable: instrument the applier backlog depth (events pending) per run and check whether backlog correlates with which mode the run lands in. (Tested in Section 24: mode-selection is NOT the general explanation; cluster53 is unimodal yet still grows.)

### 22.4 S3-FIFO reference line is incomplete

Five of twenty S3-FIFO cells failed (cache-t-00 at t=2 and t=8; cluster53 at t=8 and t=16; msr_prxy_1 at t=4, t=8, t=16). This is not flakiness in our harness; it is a concurrency defect in the artifact's S3-FIFO, diagnosed in Section 23. S3-FIFO numbers are reliable only at t=1 and t=2 and should be reported with that limitation stated.

---

### 22.5 Complete per-cell results (absolute miss ratios, gap, and throughput)

**SUPERSEDED for shared mode by Section 31** (Aug 6): this table was produced on the harness that had the three shared-mode bugs (key offset, double increment, off-by-one), so its shared-mode t>1 cells are corrupt. Section 31.3 has the corrected 5-run shared-mode numbers. The table is retained below for provenance and because its throughput columns and single-thread cells remain informative, but do not cite its t>1 shared-mode miss ratios.

The canonical per-cell record, from `final_mt_20260802.csv`: **turbo-off**, the SIMD + adaptive-backoff champion (Section 30), LRUForgive = mean of 5 runs, LRU = 1 run (near-deterministic on these traces), gap = LRU minus LRUForgive mean in pp, throughput in MQPS. This supersedes the July 18 pre-backoff, turbo-on numbers; the LRUForgive throughput roughly doubles at high thread counts versus that run, while miss ratios are unchanged within noise. cache-t-00 is 3-way-verified backoff-neutral (Section 30.3); the other traces' backoff-neutrality is corroborated by their agreement with the July miss ratios but full 3-way verification is an open task. **S3-FIFO columns are from the July 18 run (turbo-on)**, since S3-FIFO was not in the turbo-off rerun; its `FAIL` cells are the artifact concurrency defect (Section 23). All throughput carries the turbo-on-history caveat only for the S3-FIFO column now; LRU and LRUForgive throughput here are genuine turbo-off.

**msr_proj_0 @256MB** (hp = 24 + log2 threads)

| t | LRU | LRUForgive (5-run) | S3-FIFO | gap (pp) | LRU MQPS | LF MQPS | S3F MQPS |
|---|-----|--------------------|---------|----------|----------|---------|----------|
| 1 | 0.5061 | 0.4457 | 0.4213 | 6.04 | 0.22 | 0.16 | 0.74 |
| 2 | 0.5062 | 0.4459 | 0.4214 | 6.03 | 0.21 | 0.16 | 0.73 |
| 4 | 0.5901 | 0.5463 | 0.5341 | 4.38 | 0.39 | 0.25 | 0.81 |
| 8 | 0.6507 | 0.6156 | 0.6079 | 3.51 | 0.71 | 0.39 | 0.85 |
| 16 | 0.6933 | 0.6784 | 0.6512 | 1.49 | 0.83 | 0.48 | 0.87 |

**cache-t-00 @512MB** (hp = 25 + log2 threads)

| t | LRU | LRUForgive (5-run) | S3-FIFO | gap (pp) | LRU MQPS | LF MQPS | S3F MQPS |
|---|-----|--------------------|---------|----------|----------|---------|----------|
| 1 | 0.8325 | 0.7759 | 0.7750 | 5.66 | 0.25 | 0.15 | 0.56 |
| 2 | 0.9850 anom | 0.7760 | FAIL | n/m | 0.23 | 0.15 | - |
| 4 | 0.8872 | 0.8387 | 0.8301 | 4.85 | 0.46 | 0.27 | 0.95 |
| 8 | 0.9241 | 0.8831 | FAIL | 4.10 | 0.80 | 0.51 | - |
| 16 | 0.9463 | 0.9177 | 0.9006 | 2.86 | 0.95 | 0.79 | 1.59 |

**cluster53 @128MB** (hp = 23 + log2 threads)

| t | LRU | LRUForgive (5-run) | S3-FIFO | gap (pp) | LRU MQPS | LF MQPS | S3F MQPS |
|---|-----|--------------------|---------|----------|----------|---------|----------|
| 1 | 0.2618 | 0.2426 | 0.2359 | 1.92 | 0.90 | 0.27 | 1.68 |
| 2 | 0.2619 | 0.2426 | 0.2359 | 1.93 | 0.88 | 0.26 | 1.68 |
| 4 | 0.3631 | 0.3406 | 0.3245 | 2.25 | 1.45 | 0.30 | 2.68 |
| 8 | 0.4533 | 0.4286 | FAIL | 2.47 | 2.18 | 0.40 | - |
| 16 | 0.5252 | 0.5034 | FAIL | 2.18 | 2.41 | 0.43 | - |

**msr_prxy_1 @256MB** (hp = 24 + log2 threads) DIAGNOSTIC ONLY, do not publish (LRU discrepancy, Sections 20.3 and 25.6; bimodal at t=1)

| t | LRU | LRUForgive (5-run) | S3-FIFO | gap (pp) | LRU MQPS | LF MQPS | S3F MQPS |
|---|-----|--------------------|---------|----------|----------|---------|----------|
| 1 | 0.2846 | 0.2551 hi / 0.1976 lo | 0.1097 | n/m | 0.71 | 0.35 | 1.73 |
| 2 | 0.2820 | 0.1968 | 0.1099 | 8.52 | 0.69 | 0.38 | 1.67 |
| 4 | 0.5694 | 0.4663 | FAIL | 10.31 | 0.90 | 0.32 | - |
| 8 | 0.6885 | 0.5626 | FAIL | 12.59 | 1.13 | 0.36 | - |
| 16 | 0.7597 | 0.6859 | FAIL | 7.38 | 1.19 | 0.36 | - |

At t=1 msr_prxy_1's five runs split bimodally (one at 0.1976, four averaging 0.2551); prxy_1 stays diagnostic-only until the LRU discrepancy is root-caused. Its t=16 LRUForgive also has a wide spread (0.678 to 0.692).

Two cross-cutting reads. First, the ordering LRU worse than LRUForgive worse than S3-FIFO holds in every cell where S3-FIFO ran, on absolute miss ratios, not just the gap. Second, absolute miss ratios rise with thread count on every trace (cache-t-00 LRU 0.8325 at t=1 to 0.9463 at t=16), the shared-mode reordering raising everyone's miss ratio, which is why the gap, not the absolute, is the policy-comparison metric (Section 7). On throughput, the turbo-off backoff LRUForgive now runs 0.15 to 0.79 MQPS on cache-t-00 across threads (was 0.05 to 0.34 pre-fix); it still trails LRU (the applier is a serialization point, Section 29.4), but by 1.2 to 6x rather than 3 to 9x, and applier sharding was shown to be the wrong way to close the rest (Section 29).

The cache-t-00 t=2 cell (hp=26) is the longstanding hashpower anomaly (Section 14b, marked `anom`): hp=26 inflates LRU independent of policy (0.9850 here), so its gap is not meaningful. It is a fixed property of that hashpower, reproduced across runs, not a per-run fluke.



While collecting the S3-FIFO reference line, its failures were traced to a concurrency defect in the SOSP'23 artifact itself. This is documented here because it constrains what the project can claim and directly affects the planned S3-FIFOForgive phase.

### 23.1 Symptoms

Escalating failure modes with thread count, on cache-t-00 @512MB, both benchmark modes:

| Threads | Exit code | Failure |
|---------|-----------|---------|
| 1 | 0 | completes |
| 2 | 0 | completes |
| 4 | 124 or 139 | hang, or segfault |
| 8 | 134 | abort, prints `pfifo_->size() = 71` |
| 16 | 139 | segfault |

Nondeterministic: the same command at t=4 hung on one attempt and completed on another. Each failing run processes several million requests and prints an intermediate checkpoint before dying, so the corruption accumulates rather than failing immediately.

### 23.2 It is the artifact's code, not ours

- **Both benchmark modes fail.** Replicated mode (mode 0) is the artifact's own default, used by their `run.sh`, and it segfaults at t=4 (3 of 3 attempts). Our added shared mode (Section 6) is not implicated.
- **LRU is unaffected** in both modes at all thread counts, isolating the fault to S3-FIFO specifically rather than to the shared harness.
- **The active eviction path is labeled single-threaded in the source.** `S3FIFOList-inl.h` line 20 carries the comment `// used for single thread` directly above `getEvictionCandidate()`, which Section 9 established is the live path in this build. The concurrent alternative (`getEvictionCandidate0` reading pre-computed victims from an MPMC queue, driven by a background thread under `ENABLE_SCALABILITY`) is commented out by the authors, visible at line 84: `// #define ENABLE_SCALABILITY`.

### 23.3 Mechanism: check-then-remove is not atomic

In `getEvictionCandidate()`:

```
while (true) {
  if (pfifo_->size() > (pfifo_->size() + mfifo_->size()) * pRatio_) {   // CHECK
    curr = pfifo_->removeTail();                                        // USE
    if (curr == nullptr) {
      if (pfifo_->size() != 0) {
        printf("pfifo_->size() = %zu\n", pfifo_->size());
        abort();                                                        // exit 134
      }
      continue;                                                         // benign: genuinely empty
    }
    if (pfifo_->isAccessed(*curr)) { ... }
```

`AtomicDList` provides atomic `size()` and a locked `removeTail()` individually, but nothing holds across the pair. Multiple threads can pass the size check, all call `removeTail()`, and only one gets the node. The author anticipated a null return and handles the legitimate case (list empty, retry) but calls `abort()` for the case considered impossible single-threaded: null returned while size reads nonzero. Under concurrency that is routine, which produces exit 134 and the diagnostic print. The segfaults (139) are not explained by this branch (the mfifo path uses `continue` rather than `abort`) and most likely come from two threads both removing and operating on the same node, i.e. use-after-free.

### 23.4 Why eviction pressure is the trigger

Controlled test, t=4, mode 0, cache-t-00, varying size and hashpower independently:

| Cache | hashpower | Result |
|-------|-----------|--------|
| 512MB | 27 | exit 124 (hang) |
| 512MB | 23 | exit 139 (segfault) |
| 16000MB | 23 | exit 0 |
| 16000MB | 27 | exit 0 |

Cache size determines the outcome; hashpower is irrelevant. The mechanism is not cache size per se but **eviction frequency**: at 512MB cache-t-00 runs at 86-90% miss so nearly every request evicts and the racy region executes constantly; at 16000MB it runs at ~54% miss and the window is rarely open. Thread count is the second multiplier: the artifact's own `run.sh` (which scales cache from 4000MB to 64000MB) completed t=1 through t=8 and still aborted at t=16.

This explains why the defect was not caught upstream: their evaluation harness operates entirely in the low-pressure regime.

### 23.5 Consequences

1. **The S3-FIFO reference line is unobtainable at our operating points above t=2.** The operating points are by construction high-eviction-pressure (Sections 18.1, 20.2), which is exactly the regime that triggers the defect. Report S3-FIFO at t=1 and t=2 with the limitation noted.
2. **The S3-FIFOForgive phase cannot be evaluated multithreaded** without first repairing the artifact's concurrent eviction path. Single-threaded S3-FIFOForgive remains viable. This should be raised with the team before that phase is scheduled.
3. **Do not patch the abort into a retry.** Changing `abort()` to `continue` would likely let runs complete, but the underlying non-atomic check-then-remove would still corrupt FIFO ordering, producing miss ratios of uncertain validity. A wrong baseline number is worse than a missing one. If the baseline is needed, the correct fix is making check-and-remove atomic (or enabling and validating the authors' shelved `getEvictionCandidate0` path), which is real systems work and likely out of scope for the resubmission.
4. **Corollary in our favor:** the log-structured LRUForgive completed all 20 multithreaded cells across four traces at up to 16 threads, with repeat variance under 0.001 on cache-t-00 and cluster53, in the same high-pressure regime where the artifact's own policy aborts.

## 24. Bimodality is a cliff property, not a design property (July 22)

Section 22.3 established that msr_prxy_1 at 256MB has a bimodal t=1 distribution (seven runs at ~0.196, two at ~0.160). The open question was whether this reflects a general property of the log-structured design (two attractors selected by applier-staleness timing) or something specific to that operating point. It also raised the possibility that "concurrency selects the better mode" explained why some traces' gaps GROW with thread count (22.1).

**Probe.** Ten single-threaded LRUForgive runs at the operating point of each of three traces, using the stock recentWindow=16 binary: msr_proj_0 @256MB (gap DECAYS with threads), cluster53 @128MB (gap GROWS), cache-t-00 @512MB (gap DECAYS).

| Trace | 10 sorted miss ratios | Spread | Verdict |
|-------|----------------------|--------|---------|
| msr_proj_0 | 0.4410 0.4411 0.4414 0.4414 0.4414 0.4415 0.4416 0.4421 0.4439 0.4485 | 0.0075 | unimodal (one cluster with a tail) |
| cluster53 | 0.2426 x8, 0.2427 x2 | **0.0001** | unimodal, near-deterministic |
| cache-t-00 | 0.7757, 0.7759 x5, 0.7760 x4 | **0.0003** | unimodal, near-deterministic |

**All three are unimodal. msr_prxy_1 is the only bimodal trace found.**

### 24.1 Consequence 1: mode selection is NOT the explanation for growing gaps

cluster53's gap grows with thread count (1.92 -> 2.88pp, Section 22.1) while being the single most reproducible measurement in the project (spread 0.0001 across ten runs). A trace can therefore improve under concurrency with no bimodality whatsoever. The hypothesis floated in 22.3, that concurrency reliably selects a discretely better forgiveness regime, is falsified as a general mechanism.

**What remains is a single mechanism for gap direction, the tug-of-war:**
- Shared-mode interleaving scrambles access order. LRU's entire premise is recency ordering, so scrambling damages LRU more than LRUForgive, which has forgiveness to recover some of the resulting bad evictions. This **widens** the gap.
- Recency-buffer dilution damages LRUForgive only: a fixed 16-slot buffer written by N threads at scattered trace positions carries proportionally less genuinely-recent signal. This **narrows** the gap.
- Whichever force dominates on a given trace sets the sign of its gap-versus-threads curve.

This is simpler and more defensible than two competing mechanisms, and it makes the recency-buffer experiment (Section 15 miss-ratio item) the direct test of the narrowing force rather than a side investigation. (Update, July: that experiment was run. Scaling recentWindow 16 -> 64 did NOT flatten msr_proj_0's thread-decay, was neutral at t=1, and cost about 2x throughput, so recency scaling is falsified as a lever. Its value is diagnostic: because enlarging the read-side buffer does nothing, the MT degradation is write-side, i.e. embeddings corrupted by out-of-order application, not read-side buffer capacity. That is what points the next miss-ratio work at ordering fidelity, Section 15.)

### 24.2 Consequence 2: bimodality is a property of operating on a cliff

msr_prxy_1 @256MB sits mid-cliff: LRU miss falls 0.6437 -> 0.2846 -> 0.0032 across 128/256/512MB. Where the miss-ratio surface is that steep, a small difference in applier staleness tips a cascade of forgiveness decisions and the run settles into one of two attractors. The other three operating points sit on gentle parts of their curves and show no such behavior.

So bimodality is a caveat about WHERE a trace is measured, not a defect in the log-structured design. Practical rule: before trusting any single-run number, check whether the operating point sits on a steep section of the trace's miss-ratio curve; if it does, run repeats and report the modes.

### 24.3 Consequence 3: the noise-floor claims in 19.12 and 20.4 were wrong as stated

Sections 19.12 and 20.4 generalized from msr_proj_0 that LRUForgive carries inherent run-to-run jitter of about +/-0.35pp and that single-run deltas below ~0.5pp are unresolvable. **That is now shown to be trace-specific, not inherent.** cluster53 and cache-t-00 reproduce to 0.0001 and 0.0003 respectively, roughly 30x tighter than the claimed floor. The variance is concentrated in exactly two operating points: msr_proj_0 (spread 0.0075) and msr_prxy_1 (bimodal, 3.7pp between modes).

Corrected operational rule:
- **cluster53 @128MB and cache-t-00 @512MB:** single runs are trustworthy; effects well below 0.1pp are detectable.
- **msr_proj_0 @256MB:** use 3-run means; the resolvable floor is roughly 0.1pp (spread 0.0075 over 10 runs).
- **msr_prxy_1 @256MB:** bimodal; report modes, never a mean.

This materially cheapens the recency-buffer experiment: on the two tight traces, single runs suffice, so the sweep needs a third of the runs originally planned.

### 24.4 Follow-up: is prxy_1's bimodality specific to the cliff position?

Direct test of 24.2: run repeats at cache sizes stepping across and past the cliff (200, 400, 600, 800, 1000MB). The prediction is that bimodality appears only at sizes on the steep section and disappears once the curve flattens (by 512MB LRU is already at 0.0032, so 600MB and above should be firmly unimodal). If bimodality persists at flat sizes, the cliff explanation is wrong and the mechanism is something else.

---

## 25. Eviction-path norm caching and bounded top-k (July 28)

The first optimization on the post-characterization roadmap (Section 15) was the compute-level speedup of the eviction-time forgiveness path flagged in Section 14 item 3: cache embedding norms, replace the full sort with a bounded top-k, and check whether AVX2 is warranted. It landed, validated miss-ratio-neutral across the full thread grid, and is now canonical. This section records what changed, why it is bitwise-faithful, and the validation.

### 25.1 What changed

The eviction-time similarity read (`avg_top_k_similarity_to_recent`) recomputed, on every candidate eviction, the L2 norm of every recent-buffer embedding and of the candidate, once per (candidate, recent) pair. With K=8 subvectors of D=8 and up to 16 recent entries, the candidate's K norms were recomputed up to 16 times per eviction, and each recent entry's K norms were recomputed on every candidate. It then built a `std::vector` of similarities and `std::sort`ed it to take the top 3.

Three changes:
1. **Co-locate a cached norm with each embedding.** `EmbeddingArray` became a small struct `EmbeddingData { std::array<std::array<double,D>,K> vectors; std::array<double,K> norms; }`, so every existing `emb.vectors[k][d]` site reads the same data. `norms[k]` holds `sqrt(sum of squares of vectors[k])`, computed inline in the same loop that writes the vector, at both mutation sites (`init_embedding`, `update_embedding`). (Implementation note: `update_embedding` stores `sqrt(sq_norm) + 1e-10`, `init_embedding` stores `sqrt(sq_norm)` with no epsilon. That asymmetry and the choice to cache the norm rather than the squared norm are what break bitwise identity, see 25.2.)
2. **`update_recent` carries the norm for free.** Because the recent buffer stores the struct by value, the existing `recent_embeddings_[idx] = it->second` copy brings `norms` along, so a recent entry's norm is never recomputed at eviction. No new maintenance.
3. **Single-pass bounded top-3.** `avg_top_k_similarity_to_recent` was rewritten to track the three best similarities in scalars (`t1,t2,t3`) as it scans the recent buffer, removing the similarity vector and the sort. `cosine_similarity` was refactored to accept the two cached norms and compute `denom = norm_a * norm_b` instead of recomputing them; it is left defined in case anything else calls it, and `avg_top_k` no longer builds a vector or sorts.

### 25.2 The implementation departs from bitwise identity; it is empirically neutral

The plan of record (and an earlier draft of this section) was to cache the SQUARED norm and keep the exact `denom = sqrt(norm2_a * norm2_b)` form, which would have been bit-for-bit identical to the original cosine. **The shipped code does not do that.** It caches `norms[k] = sqrt(sq_norm)` and computes `denom = norm_a * norm_b`, i.e. `sqrt(x) * sqrt(y)`, which is not equal to `sqrt(x*y)` in floating point. It also adds `+1e-10` to the stored norm in `update_embedding` but not in `init_embedding`. Both choices change the rounding of near-threshold cosine values relative to the original, so the optimization is **not** provably identical.

Because Sections 22.3 and 24.2 documented that msr_prxy_1 (and to a lesser degree msr_proj_0) are FP-sensitive enough to shift forgiveness decisions on a cliff, this departure is not purely cosmetic: it is the most likely cause of the small measured differences in 25.3 (cluster53 0.2427 vs 0.2426, cache-t-00 means 0.8816 vs 0.8813), rather than applier-timing jitter alone. The differences are 0.0001 to 0.0003, inside each trace's own noise band (24.3), so the change is **empirically miss-ratio-neutral**, which clears the "no miss-ratio risk" bar in practice. It is an empirical guarantee, not the analytic one the plan called for.

**Why the shipped (non-identical) version is kept: it is the faster of the two.** The choice of `norms` over `norm2` is a deliberate speed decision, not an oversight to clean up. The shipped code computes the `sqrt` at write time (once per applied event, in `update_embedding`/`init_embedding`, on the applier) and does a bare `denom = norm_a * norm_b` at eviction. The analytic-identity version (`norm2 = sq_norm`, `denom = std::sqrt(norm2_a * norm2_b)`) removes the write-time `sqrt` but moves a `sqrt` onto the read path, inside the K-loop over up to 16 recent entries, so it can do up to K*recent = 128 `sqrt`s per eviction versus the shipped version's K=8 per write. On high-eviction traces (cache-t-00 runs ~83% miss) that is a likely read-path throughput regression, so the norm-vs-norm2 choice is genuinely speed-vs-accuracy and was decided for speed. Restoring identity would be a three-line change (cache `sq_norm`, `sqrt` the product under a restored `< 1e-10` guard, drop the `+1e-10`) but is NOT free; it trades read-path throughput for the clean claim and the removed init/update asymmetry. It was not done. If read-path speed were the goal instead, the move is the opposite of identity: cache `1/||E||` at write time so cosine is a bare `dot * inv_a * inv_b` with no eviction-time `sqrt` or divide, which is the fastest of the three and the largest FP change, and would need its own validation.

The read-path `sqrt` per pair is retained either way. The win is eliminating the two D-length sum-of-squares per pair, which was the dominant per-pair arithmetic and, on the candidate side, was being redone up to 16 times per eviction, plus dropping the similarity vector and sort. Turning cosine into a bare dot product by caching `1/||E||` and dropping the read-path sqrt is a separate, larger FP change that was not done.

### 25.3 Fidelity validation: the tight traces reproduce to within noise

The patch was gated on the two near-deterministic operating points (Section 24.3), not on msr_proj_0 (whose 0.0075 spread makes it useless as a fidelity gate):
- **cluster53 @128MB single-thread: exactly 0.2426 across 3 runs**, matching the banked value to 0.0001.
- **cache-t-00 @512MB single-thread: 0.7761 / 0.7759 / 0.7758**, inside the banked 0.7757-0.7760 band.

Exact reproduction on the initial fidelity gate was close but not perfect once the code was inspected: the SIMD binary is not bitwise-identical (25.2), so these numbers are "within each trace's noise" rather than "identical." msr_proj_0's 3-run t=1 mean (0.4427) sat within its own scatter of the banked ~0.4442.

**Same-session pre-vs-post cross-check (July 28, via the `.stage1.bak` binary).** The pre-SIMD and patched binaries were compared directly in one session (25.5). Miss ratios differed by an amount inside each trace's known run-to-run noise: cluster53 @128MB t=1 was stage1 0.2426 vs simd 0.2427 (delta 0.0001), and cache-t-00 @512MB t=8 was stage1 0.8815 / 0.8811 vs simd 0.8818 / 0.8814 (means 0.8813 vs 0.8816). Given the confirmed FP departure (25.2), the honest reading is that the simd binary shifts a handful of near-threshold forgiveness decisions by a hair, and the net effect is below each trace's noise floor (24.3). So the result is empirical miss-ratio neutrality, not analytic identity. The applier also makes even t=1 non-deterministic at the 0.0001 level, which compounds the reason single-run pairs cannot resolve the difference either way.

### 25.4 Miss-ratio neutrality across the full thread grid

The optimization was re-run across the MT grid to confirm neutrality is not just a single-thread property. The LRU baseline reproduced its banked t=1 anchors exactly (msr_proj_0 0.5061, cache-t-00 0.8325, cluster53 0.2618), so the gap comparison is trustworthy. Gap = LRU minus mean LRUForgive, this session (banked Section 22.1 in parentheses):

| Threads | msr_proj_0 | cache-t-00 | cluster53 |
|---------|------------|------------|-----------|
| 1 | 6.34 (6.47) | 5.66 (5.65) | 1.92 (1.92) |
| 2 | 6.70 (6.91) | omit (5.66) | 1.93 (1.93) |
| 4 | 5.16 (5.30) | 4.93 (4.94) | 2.19 (2.33) |
| 8 | 4.87 (4.13) | 4.23 (3.89) | 3.35 (2.88) |
| 16 | 2.49 (2.00) | 3.04 (2.82) | 2.09 (2.06) |

Every trace reproduces its Section 22.1/24.1 tug-of-war shape: msr_proj_0 and cache-t-00 decay, cluster53 grows to a t=8 peak then falls. A few cells (msr_proj_0 t=8, cluster53 t=8) came in slightly above banked, but those are single-run LRU against 3-run LRUForgive, inside each trace's own scatter, and not systematic in direction. The optimization is miss-ratio-neutral at every thread count, not only at t=1. Cleared for the codebase.

### 25.5 Throughput: a real constant-factor win, largest single-thread, still positive under threading

The clean same-session A/B was run after all, using a `TARDISEmbeddingManager.h.stage1.bak` snapshot of the pre-SIMD header rather than reconstructing it from git. The current patched binary was preserved outside `_build` (`~/lruforgive.simd`), the `.bak` header was built once into `~/lruforgive.stage1`, and the two were compared on the same node in the same session. This is the isolated measurement the earlier cross-session comparison could not give, and it overturns the "neutral" reading that an earlier draft of this section recorded.

| Cell | stage1 (pre-SIMD) | simd (patched) | Speedup |
|------|-------------------|----------------|---------|
| cluster53 @128MB, t=1 (single-thread) | 3472.74s, 0.09 MQPS | 2548.72s, 0.12 MQPS | **1.36x** (27% less wall-clock) |
| cache-t-00 @512MB, t=8 (applier-bound) | 100.9s mean, 0.18 MQPS | 92.0s mean, 0.20 MQPS | **1.10x** (~10%) |

The optimization speeds up the eviction-time read path, so its effect is largest where that path is the biggest share of the critical path. At t=1 there is no applier contention diluting it, so cluster53 shows the full **1.36x**. At t=8 more of the time is spent contending with or waiting on the serial applier, so the read-path speedup is diluted to **1.10x** but stays clearly positive. So the correct statement is not "neutral on applier-bound cells"; it is that the gain **decays with thread count** as the bottleneck shifts onto the applier, without vanishing.

**Correction to the earlier draft.** A prior version of this section matched the t=8 result against a *banked* 0.20 MQPS and called it flat. The same-session pair is 0.18 (stage1) to 0.20 (simd), a genuine ~10% gain the cross-session comparison hid. The lesson reinforces the infra rule (footer): keep a labelled pre-change binary outside `_build` so isolation is a two-run comparison, not an archaeology exercise. The `.stage1.bak` header is what made this cheap here.

**This does not change the standing conclusion about scaling.** The single applier is still the bottleneck that limits *scaling*: adding threads does not help LRUForgive the way it helps LRU, which is why LRUForgive is 3 to 6.6x slower than LRU at t=16 (msr_proj_0 0.25 vs 0.82 MQPS, cache-t-00 0.32 vs 0.95, cluster53 0.37 vs 2.45, msr_prxy_1 0.22 vs 1.21). The norm-caching patch does not touch the applier, so it gives a constant-factor lift at each fixed thread count but does not change the scaling curve. The real scaling prize remains sharded appliers, which the windowed-ordering work (Section 15) is the precondition for.

### 25.6 Two session anomalies, neither caused by the patch

1. **cache-t-00 t=2 hp=26 hit LRU only this session.** The documented hashpower-26 anomaly (Section 14b) usually inflates both policies equally, but this session it inflated LRU alone (LRU 0.9852, LRUForgive normal at 0.7759), producing a spurious ~21pp gap. The t=2 cache-t-00 cell is omitted as before; the new wrinkle is that "inflates both equally" is not always true, so the t=2 asymmetry is worth a flag but not a chase.
2. **msr_prxy_1 t=1 bimodal again, LRU discrepancy still open.** The three t=1 LRUForgive runs were 0.1607 / 0.1964 / 0.1579 (two low ~0.159, one high ~0.196), reproducing the cliff bimodality of Sections 22.3 and 24.2. LRU t=1 landed on 0.2846, one of the three reproducible-across-sessions values (0.2846 / 0.2758 / 0.3574) that deterministic LRU should not produce. The msr_prxy_1 operating point remains diagnostic-only and blocked from publication until that LRU discrepancy is explained.

### 25.7 AVX2 is available but was not warranted

`objdump -d` of the rebuilt binary showed the D=8 dot-product loop compiled to scalar `mulsd`, not auto-vectorized, because GCC will not vectorize a floating-point reduction without `-ffast-math`. So hand-written AVX2 (`_mm256_fmadd_pd`) is genuinely available as a further speedup, unlike the case where the compiler already packs the loop. It was deferred: norm caching already removed the dominant per-pair waste, the system is applier-bound rather than read-bound, and hand intrinsics add maintenance and FP-ordering risk for a speedup on a path that is not the ceiling. AVX2 is banked as a future option if the read path ever becomes hot again, for example after sharded appliers move the bottleneck off the applier.

### 25.8 Status

Norm caching plus bounded top-3 is canonical on `tardis-implementation`, validated empirically miss-ratio-neutral across t=1..16 (not bitwise-identical; the FP departure of 25.2 shifts a few decisions within each trace's noise floor), with a measured constant-factor throughput win from the same-session A/B: 1.36x single-thread on cluster53, decaying to 1.10x at t=8 as the bottleneck shifts onto the serial applier. It clears Section 14 item 3 and the compute-speedup line of Section 15. The next lever is unchanged: non-strict windowed ordering as the precondition for sharded appliers, which attacks the single-applier bottleneck this optimization confirmed is the real ceiling.

---

## 26. Windowed ordering: single-applier hard-barrier is a dead end; the pivot to sharding (July 29)

The non-strict windowed-ordering idea (Section 15 item 2) was prototyped as an env-gated windowed applier: partition seq space into fixed windows W, hard barrier between windows, order relaxed within (object-grouped) or strict within (seq-ordered, as a machinery check). `TARDIS_WINDOW` set W, `TARDIS_WINGROUP` chose grouped vs strict. The result killed the single-applier version of the idea and, more usefully, said exactly what the sharded design must avoid.

### 26.1 Two confounds, both intrinsic to a single applier

**Staleness confound (strict path, `WINGROUP=0`).** Strict-within-window should equal baseline at every W. On cache-t-00 @512 t=1 it held at W=256 (0.7756) and W=4096 (0.7763) but broke at W=65536 (0.8100 vs baseline ~0.7758). Strict order was preserved, so this is not an ordering bug: a large window makes the applier collect W events before applying any, freezing embedding state while workers race ahead reading stale embeddings at eviction. Window size doubles as applier lag.

**Head-of-line confound (any W, t>1).** The hard barrier destroys the gap at multi-thread even at W=1 (strict, zero reorder). cache-t-00 @512 t=8 hp28: baseline (windowing off) miss 0.8818, gap vs LRU 0.9240 = 4.22pp; windowed W=1 miss 0.9124, gap 1.18pp; W=256/4096/65536 all ~0.912, gap ~1.18pp, flat across W. Losing ~3pp of gap at W=1 is not reorder, it is head-of-line staleness: to apply seq k in order the single applier blocks on the buffer owning k, so when one worker lags the others race ahead and read badly stale embeddings at eviction. The non-blocking batched baseline never blocks on a straggler, so it keeps embeddings fresh. The barrier is strictly worse.

So the 4b family is flat across W because barrier-staleness dominates and masks any reorder effect. The single-applier windowing proxy cannot answer the reorder-cap question: in one applier, reorder and staleness are coupled (both come from delaying application), and strict order requires a barrier that induces head-of-line staleness. Only parallel appliers decouple them, which is the whole point of sharding.

### 26.2 The one salvageable result: the t=1 reorder budget

At t=1 there are no stragglers, so `WINGROUP=0` is a clean staleness-only control and subtracting it isolates the pure object-grouped reorder cost on cache-t-00 @512:

| W | WG0 (staleness only) | WG1 (staleness+reorder) | reorder cost |
|------|------|------|------|
| 256 | 0.7756 | 0.7758 | ~0 |
| 4096 | 0.7763 | 0.7910 | +1.47pp |
| 65536 | 0.8100 | 0.8321 | +2.21pp |

Object-grouped reorder is ~free up to W around 1024 and only starts costing at 4096. There is a real fidelity budget; the barrier is just the wrong way to spend it.

### 26.3 Corrected model of what sharding does to recency

A second realization: full object-grouping within a window (the `WINGROUP=1` model) is an unrealistic, doubly-pessimistic model of sharding. It concatenates each object's events (reorder ~W), so a large window's recency ring ends up holding the highest-hash objects rather than the seq-latest ones. Real K parallel appliers INTERLEAVE by wall-clock, preserving approximate recency (reorder ~K, not ~W). And with faithful context (a serial sub-epoch pass) plus same-object order preserved by hash-sharding, embeddings are byte-faithful under sharding, so the ONLY thing sharding actually approximates is the recency ring's write order. That approximation is bounded and, under interleaving, small.

### 26.4 Phase 1b and Phase 2

Phase 1a windowing code was reverted. The plan is now:

- **Phase 1b (recency-reorder isolation, single applier, no barrier).** Exploit that `applyBatch` holds the write lock for the whole batch and eviction reads take the shared lock, so no read sees the ring mid-batch. Apply every event in seq order exactly as baseline (embeddings/context/counts byte-faithful), capture each recency write's (obj_id, embedding snapshot) at its true seq position, and flush the captured writes into the ring at batch end in shard order (bucketed by hash(obj_id) % K). Deferring the flush to batch end adds zero staleness (no read observes mid-batch), so this isolates recency reorder with none of the 1a confounds. K=1 reproduces baseline byte-for-byte. `TARDIS_RECENCY_SHARDS=K`, `TARDIS_RECENCY_MODE` = interleave (realistic, reorder ~K) or group (pessimistic, reorder ~batch). Reorder is capped at kBatchSize=256, a realistic skew bound. If interleave (and ideally group) is cheap at the operating points, sharding is greenlit on fidelity before any concurrent code is written.
- **Phase 2 (sharded appliers).** K appliers partitioned by object hash, faithful context via a serial sub-epoch pass, recency interleaved (not grouped) and incremental (never freeze-collect-then-apply), and no stop-the-world boundary barrier (double-buffer windows instead, so no shard blocks on the slowest). The 1a finding is the design constraint: keep per-apply staleness low and never block all shards on one. This is the throughput play and it structurally avoids both 1a confounds.

## 27. Phase 1b: recency reorder is free; sharding greenlit on fidelity (July 29)

Phase 1b isolated the one thing sharding actually approximates, the recency ring's write order, with no barrier and no staleness confound (the mechanism is in 26.4: capture each recency write at its true seq position during the byte-faithful seq-order apply, flush into the ring at batch end in shard order; because `applyBatch` holds the write lock for the whole batch and no eviction read sees the ring mid-batch, deferring the flush adds zero staleness). `TARDIS_RECENCY_SHARDS=K` sets the shard count; `TARDIS_RECENCY_MODE` picks interleave (round-robin across buckets, realistic, reorder ~K) or group (concatenate buckets, pessimistic, reorder ~batch=256). K=1 reproduces baseline byte-for-byte.

### 27.1 Results (single-thread, absolute miss ratios)

Gate: K=1 reproduced baseline exactly (cluster53 @128 0.2426 / 0.2427, straddling the banked 0.2426), confirming the capture/flush path is faithful.

Interleave (MODE=0):

| Trace | K=1 (baseline) | K=4 | K=8 | K=16 |
|-------|----------------|-----|-----|------|
| cluster53 @128MB | 0.2426 | 0.2426 | 0.2426 | 0.2426 |
| cache-t-00 @512MB | ~0.7758 | 0.7760 | 0.7758 | 0.7762 |

Group (MODE=1, pessimistic reorder ~256):

| Trace | K=8 | K=16 |
|-------|-----|------|
| cluster53 @128MB | 0.2426 | 0.2426 |
| cache-t-00 @512MB | 0.7760 | 0.7759 |

msr_proj_0 @256MB, 3-run means (the noisy trace, per 24.3): K=1 mean 0.4414 (0.4418 / 0.4411 / 0.4413), K=8 interleave mean 0.4422 (0.4443 / 0.4408 / 0.4415), inside its own 0.0075 scatter.

### 27.2 Verdict

Recency reorder costs nothing, even at the pessimistic bound. Interleave (the realistic model of balanced parallel shards) is neutral at every K on both tight traces and within scatter on msr_proj_0; group, which reorders recency writes by up to a full 256-event batch, is also neutral. This is the strong form of the result: even the worst-case recency reorder we could produce on a single applier does not move the miss ratio.

Combined with the design facts that faithful context (a serial sub-epoch pass) plus same-object order (guaranteed by hash-sharding, since one object's events all land on one shard) keep embeddings byte-faithful under sharding, the recency ring was the ONLY thing sharding approximates, and it is now shown free. Phase 2 sharded appliers are greenlit on fidelity before any concurrent code is written. Caveat: the reorder here is capped at the batch size (256), a realistic skew bound; throughput was not measured (single applier, Phase 2 territory).

## 28. Phase 2: sharded appliers (design; OUTCOME in Section 29)

**This section is the design as attempted. It was built and it failed: applier sharding is the wrong lever (Section 29), and the throughput win came instead from adaptive backoff (Section 30). Read 29 and 30 for what actually happened.**

The single applier is the throughput ceiling (Section 25.5: the t=16 LRUForgive-vs-LRU penalty is 3 to 6.6x, and the applier is compute-bound on embedding math, Section 14c). Phase 2 shards that compute across K appliers. Fidelity is cleared by 27.

### 28.1 What shards, and what does not

- **Embedding updates shard cleanly** by object hash: one object's events all land on one shard, so same-object order is preserved, and different objects are independent map entries. Access counts partition with the embeddings.
- **Recency is free** (Section 27), applied interleaved and incrementally, never freeze-collect-then-apply.
- **The RNG-ordered state does NOT shard.** The context advance (every 10 events) and `init_embedding`'s per-object seed both draw from the shared `rng_` in seq order; parallel unordered draws corrupt the walk. This is handled by a cheap serial sub-epoch pass that replays the ~26 advances per 256-batch in seq order and publishes read-only per-sub-epoch context snapshots the shards read, with inits reseeded deterministically per object. The per-object init reseed is a genuine semantic change (deterministic seed vs a draw from the shared `rng_`) and gets its own validation run, independent of the sharding.

### 28.2 Persistent shards, not per-batch fork-join

A 256-event batch split K ways is ~32 events per shard, far too little to amortize spawning threads per batch. So the appliers are K persistent threads, each owning a hash partition and draining continuously, with the cheap serial context handoff between windows. The batch/window still anchors the context sub-epochs and recency merge points.

### 28.3 Two window-handoff variants, build and ablate

- **(A) Strict discrete windows (barrier at the boundary).** Each shard runs its partition, all join at the boundary, context advances serially in the gap, next window. Easier to make correct (no shard sees another's half-applied state; context lives in a clean serial gap), so it is the correctness baseline and isolates "did sharding the compute help." Known cost: it reintroduces the Section 26 stop-the-world stall, so it will leave throughput on the table at the boundaries. Build first.
- **(B) Sliding / double-buffered windows.** A shard that finishes window w starts window w+1's partition instead of idling, with only the cheap serial context handoff ordered between windows. Removes the boundary idle. Harder (two windows in flight, so a shard may read context for w+1 while another finishes w). Build second, measure the delta over A; if it does not beat A by enough to justify the complexity, ship A and record B as a documented negative.

### 28.4 Design constraints inherited from the 1a dead end

Keep per-apply staleness low (shards apply incrementally, never freeze-collect), never block all shards on the slowest (variant B, or double-buffering, exists precisely to avoid this), and keep batch collection non-blocking (retain the defer-straggler merge, which is what keeps embeddings fresh). These are the exact failures Section 26 diagnosed, now design rules.

## 29. Phase 2 outcome: applier sharding is the wrong lever (negative result, Aug 1)

Phase 2 (Section 28) was built and measured. Fidelity held throughout, but every sharded variant regressed throughput, and the diagnosis chain located the real bottleneck, which is not the applier's compute at all. This is a clean negative result and it redirected the throughput work decisively.

### 29.1 Variant A (strict windows, barrier, one global write lock): regresses even at t=1

Built exactly as 28.3(A): K persistent hash-partitioned shards, serial sub-epoch context, interleaved recency, hard boundary barrier, coordinator holds one write lock per window. Fidelity was perfect (miss ratio flat across shard count K: cluster53 0.2426 at K=1/2/4/8; cache-t-00 t=1 0.7762 across K). But throughput fell monotonically with K, and, decisively, it regressed at **t=1** (cluster53 single-thread 2608s at K=1 to 3739s at K=8, with 39 idle cores). A t=1 regression cannot be lock or barrier contention (one worker, no stragglers), so the cost is the serial coordinator work sharding adds (partition pass, context-snapshot assignment, recency merge), all O(window). A window sweep (256 to 2048) confirmed lock-hold duration was not the cost: throughput was flat across window size. cache-t-00 t=8 went 0.20 MQPS (single applier) to 0.16 (K=8).

### 29.2 Variant "Option 2" (continuous, per-shard locks, no barrier): 10x worse

To rule out the barrier and the coarse global lock, we built the maximally-decoupled version: K independent mini-appliers sharing nothing (each its own state, lock, context, RNG, recency ring), fed by a lightweight seq-routing thread, no barrier. This is sharding's best case. It was catastrophic: cache-t-00 t=8 0.20 to 0.02 MQPS at K=8 (92s to 926s), and the same ~10x blowup at t=1. Fidelity was essentially held (coarse per-shard context/recency moved miss ratio only 0.2426 to 0.2474 on cluster53), confirming the fidelity model, but throughput collapsed the harder the more we decoupled.

### 29.3 Root cause: spin-thrash on a thread-oversubscribed process (measured)

`top -H` during the Option 2 K=8 run: load average 388, 427 of 430 threads in state R (running), **63.7% of CPU in system (scheduler) time**. `strace -f -c` on the *single applier* at t=8: **84% of syscall time in `sched_yield`** (696k calls in 10s). The mechanism: the applier's empty-buffer loop and the hot path's full-buffer loop both busy-wait with `std::this_thread::yield()`. When producer and consumer rates mismatch, one side spins at full rate, turning the imbalance into a `sched_yield` storm. Every sharded variant *adds* runnable, spinning threads to a process that (with CacheLib's own background pool) already has ~50 threads, so on the CloudLab node's core budget the scheduler thrashes, and it thrashes worse with more shards. This is exactly why lock-everything (Section 4) scaled well: it adds zero threads and its lock waiters sleep, they do not spin.

### 29.4 Synthesis: the applier is a serialization point, not a compute bottleneck

The corrected model: "the applier is compute-bound" (14c) was really "the applier is one thread every event funnels through, and its idle-wait spins." Parallelizing the funnel adds coordination and spinning that cost more than the serialization they remove. The lever is therefore **fewer runnable threads / less spinning**, not more parallelism. Applier sharding (variant A, Option 2, and by extension the sliding-window variant B, which only relieves the barrier and per-shard locks, neither of which is the t=1 cost) is abandoned.

Two intermediate ideas were also tested and rejected before landing the fix:

- **Update subsampling** (forward only 1-in-N objects into the embedding pipeline, deterministic per object): erodes the gap roughly one-for-one with N and never recovers it, because dropped objects can never be forgiven. cluster53 gap of 1.92pp is gone by N=4 (miss ratio 0.2653, worse than plain LRU 0.2618); cache-t-00 loses ~2.9pp at N=2. And throughput barely moved (t=8 0.20 to 0.23 at N=4), because subsampling cuts applier *work* but not the *spinning*, which fires on rate imbalance, not work volume. Not shippable.
- **Condition-variable blocking** (applier blocks on a CV when idle, workers signal): traded the `sched_yield` storm for a `futex` storm (95% of syscall time, 604k futex calls), and threw away the throughput. At high thread counts no thread is deeply idle, so CV signaling thrashes at every wait/wake boundary. Rejected. Brief spinning wins in this tight rate-matched regime.

## 30. The throughput fix: adaptive backoff (canonical champion, Aug 2)

The bottleneck is the busy-wait spinning (29.3), so the fix is to make idle threads sleep instead of spin, with no new threads.

### 30.1 The change

Both idle sites (the applier's empty-buffer loop, the hot path's full-buffer loop) replace bare `yield()` with an adaptive `idle_backoff(n)`: a cheap no-syscall spin for the first tier (absorbs short bursts), then a few `yield()`s, then a short `sleep_for` once genuinely idle. The idle counter resets on progress. Tuned thresholds are spin < 64, yield < 512, then sleep 20us; the tuned version beat the first-cut 64/4096/50us. No new threads, no lock changes; it touches only how idle threads wait, never what is applied, so it is miss-ratio-neutral by construction.

### 30.2 Result: ~2.6 to 3x throughput, zero fidelity cost, turbo-off

cache-t-00 @512MB, turbo-off, LRUForgive throughput:

| t | single applier (pre-fix) | adaptive backoff | speedup |
|---|--------------------------|------------------|---------|
| 1 | 0.05 | 0.15 | 3.0x |
| 8 | 0.20 | 0.51 | 2.6x |
| 16 | 0.34 | 0.79 | 2.3x |

The `sched_yield` share dropped from 84% toward the sleep tier; the win comes from reaching the sleep tier sooner (freeing cores for the workers), not from eliminating yields. This is near the backoff ceiling; tuning the sleep threshold lower stops helping.

### 30.3 Three-way verification: backoff is miss-ratio-identical to the pre-SIMD version

To prove the SIMD + backoff champion changed nothing about fidelity, a controlled 3-way run on cache-t-00 (LRU, the SIMD+backoff champion, and the reverted pre-SIMD Option-B binary), 5 reps, turbo-off, both 600MB and 512MB. At the deterministic t=1 the champion and the pre-SIMD version are identical to the fourth decimal (600MB 0.7622 vs 0.7623; 512MB 0.7758 vs 0.7759). At t>1 they agree within MT noise (the champion runs ~0.1 to 0.2pp higher miss ratio at t=8/16, an applier-timing-staleness effect that slightly shrinks the gap, worth a footnote, not a concern). Throughput: the champion is 3x the pre-SIMD binary at t=1 (0.15 vs 0.05) and ~2.6x at t=8/16 (0.51 vs 0.18, 0.79 vs 0.30). So the champion is a pure throughput win at zero fidelity cost. This is the canonical implementation to commit and build the paper around.

### 30.4 Throughput overhead vs LRU, and the open comparison to lock-everything

Throughput overhead vs LRU = (LRU MQPS minus LF MQPS) / LRU MQPS, turbo-off, from 22.5. It is strongly trace-dependent, so a single mean is misleading:

| trace | t=1 | t=4 | t=8 | t=16 | mean |
|-------|-----|-----|-----|------|------|
| cache-t-00 | 40% | 41% | 36% | 17% | 34% |
| msr_proj_0 | 27% | 36% | 45% | 42% | 38% |
| msr_prxy_1 | 51% | 64% | 68% | 70% | 63% |
| cluster53 | 70% | 79% | 82% | 82% | 78% |

The structure is explained by the single applier being a fixed-rate serial ceiling. Its max apply rate is set by embedding compute and is roughly constant per trace, so LRUForgive throughput is capped near that ceiling regardless of how fast LRU runs. The overhead is therefore whatever LRU's throughput is above that ceiling. On low-miss traces LRU is cheap (few evictions) and fast (cluster53 LRU 2.4 MQPS), so the applier caps LF far below it (78% overhead); on high-miss traces LRU is expensive and slow (cache-t-00 LRU ~1 MQPS), so LF sits close to it (34%). Overhead thus rises as miss ratio falls, which is the observed ordering (cluster53 lowest miss and highest overhead, cache-t-00 highest miss and lowest overhead).

**This is NOT a throughput win over lock-everything, and the doc should not claim one.** The Section 4 lock-everything numbers were measured in a different regime (their miss ratios *fall* with thread count, the signature of replicated mode with cache scaling, versus shared mode where they rise), so the columns are not comparable, and even at face value lock-everything's t=16 throughput (1.44 MQPS) exceeds backoff's (0.79). The reason lock-everything can be faster is structural and important: it applies embeddings inline on the worker threads (under one global lock), so the embedding compute is spread across all workers and has no single-applier ceiling, it only pays the lock's critical-section serialization. The log-structured design funnels all embedding work through one applier thread, which is what caps its throughput on fast-LRU traces. The tradeoff is fidelity: lock-everything applies out of order and its miss-ratio gap collapses at high threads (Section 4: 1.47% at t=16), while the log-structured applier preserves the gap (Section 22.5). So the honest claim is: **on the main operating-point traces (cache-t-00, msr_proj_0) the log-structured backoff version runs at lock-everything-class overhead (~34 to 38% vs LRU) while keeping the forgiveness gap that lock-everything erodes**; on fast-LRU traces (cluster53) the single-applier ceiling costs more and lock-everything would likely scale better on throughput but worse on fidelity. Quantifying that tradeoff exactly requires a same-config head-to-head (open task).

### 30.5 Correction to the old 14b gap curve (baseline was inflated)

The 3-way run also corrected a data-quality error in Section 14b. 14b reported 600MB gaps of 6.90/9.74/7.00/4.84 at t=1/4/8/16, with a puzzling "gap exceeds single-thread" bump. With LRU now sampled 5x, the true 600MB gaps are **5.66/4.88/4.16/2.94**, monotonically decreasing, no bump. The LRUForgive miss ratios reproduced exactly (0.7622 vs 14b's 0.7623 at t=1), so the design was never the issue; 14b's *LRU baseline* was single-sampled and high (its t=4 LRU 0.9182 vs the real 0.8777, t=16 0.9607 vs 0.9420), which manufactured the inflated gap and the false super-linear bump. 14b caveat 2 (the super-linear-gap explanation) is therefore void: there was no super-linear gap. The lesson for the paper: LRU is not deterministic at t>1 in shared mode, so gaps must use multi-sampled LRU. On cache-t-00 LRU is in fact near-deterministic across 5 reps (600MB t=16 spread 0.0002), so its gap is trustworthy; the noisy traces (msr_proj_0, msr_prxy_1) are where multi-rep LRU matters, and pinning them is an open task.

## 31. Harness bugs fixed; corrected shared-mode results (Aug 6)

Jane found two harness bugs (a third surfaced while fixing them). All three were in the shared-mode path; replicated mode was correct throughout. The bugs corrupted the shared-mode t>1 results, so Section 22 / 22.5's shared-mode multithreaded numbers are **superseded by this section**. The single-thread characterization (16-20, default mode), the three-condition model, the S3-FIFO defect, and the throughput/backoff conclusions are unaffected.

### 31.1 The three bugs

1. **Key offset fired in shared mode.** `read_oracleGeneral_trace` remapped `obj_id = obj_id % UINT32_MAX + reader_id * 1e9` unconditionally. That is correct for replicated mode (it makes each thread's key range disjoint) but wrong for shared mode, where all threads must see the same keys. Effect: our "shared" mode had threads in disjoint key ranges, so there was zero cross-thread reuse, it shared nothing. Fix: a `shared_mode` bool on the reader (defaults false), set per thread from `opts->mode == 1`, gating the offset (applied in replicated, dropped in shared).
2. **Double increment.** The replay loop did `record_idx++` in both the seq assignment and the round-robin test, advancing by 2 per iteration. At t=4 this made `(this_seq+1) % 4` always 1 or 3, so only threads 1 and 3 processed anything, on ~half the trace. Fix: one `record_idx++`, tested on `this_seq`, giving a clean 1-of-N partition covering every record once.
3. **Off-by-one record drop.** A pre-loop `read_trace` (for a readiness log) consumed record 0 and advanced the cursor, so the loop started at record 1 with `this_seq = 0`. Fix: `reader->offset = 0` rewind before the loop.

Verification: shared t=1 now reads the full trace and matches replicated t=1 exactly (cache-t-00 0.7758), and every re-run cell's request count is the full trace at every thread count.

### 31.2 How the two modes work

Replicated (mode 0, artifact default): every thread replays the whole trace into its own disjoint key range, one shared cache object holds N independent workloads, cache scaled by N (run.sh). Threads share no objects. Shared (mode 1): one trace partitioned by position (`i % N == thread`), all threads reference the same keys in one cache of fixed size, real cross-thread reuse. Threads share all objects. The concurrency/ordering problem exists only in shared mode.

### 31.3 Corrected shared-mode results (5-run means, turbo-off, full-coverage-verified)

**cache-t-00 @512** and **msr_proj_0 @256**: the gap HOLDS across threads (it does not degrade), which is a stronger result than the old corrupt curve. The old "degradation with threads" was largely the bug (disjoint key ranges thrashing N working sets in one cache).

| t | ct00 LRU | LF | gap | proj_0 LRU | LF | gap |
|---|------|------|------|------|------|------|
| 1 | 0.8325 | 0.7759 | 5.66 | 0.5089 | 0.4451 | 6.38 |
| 2 | 0.9708 anom | 0.7779 | n/m | 0.4979 | 0.4421 | 5.58 |
| 4 | 0.8367 | 0.7868 | 4.99 | 0.4972 | 0.4478 | 4.94 |
| 8 | 0.8553 | 0.8012 | 5.41 | 0.5073 | 0.4596 | 4.77 |
| 16 | 0.8640 | 0.8050 | 5.90 | 0.5193 | 0.4611 | 5.82 |

**cluster53 @128: forgiveness INVERTS at mid thread counts** (negative gap, reproducible across all 5 reps):

| t | LRU | LF | gap |
|---|------|------|------|
| 1 | 0.2618 | 0.2426 | +1.92 |
| 2 | 0.2653 | 0.2454 | +1.99 |
| 4 | 0.2677 | 0.2827 | **-1.50** |
| 8 | 0.2799 | 0.3064 | **-2.65** |
| 16 | 0.3358 | 0.3111 | +2.47 |

**msr_prxy_1 @256: positive gap but LRU non-deterministic (publishing-blocked).** Gap is +2 to +10pp across threads, so forgiveness helps, but LRU at t=1 returns two values across reps (0.3574 x4, 0.2846 x1), which deterministic LRU cannot do, and the absolute miss ratios swing erratically with hp/threads (0.36 at t=1 to 0.08 at t=4). Hypothesis: TTL-based eviction plus CacheLib's async Reaper (`req->ttl = 2000000`, time advanced by `util::setCurrentTimeSec`), which is wall-clock-nondeterministic even at t=1 and worse under MT. Testable by disabling TTL/reaper. Until resolved, prxy_1's absolutes and LRU baseline are unusable; the positive gap is qualitatively real.

### 31.4 The cluster53 inversion: recency-buffer pollution (hypothesis)

The likely mechanism is Section 14b's untested caveat 1, now biting: the shared 16-slot recency buffer is polluted by N threads' interleaved accesses, so the forgiveness comparison (candidate vs the 16 most-recent objects) gets noisy. On a low-gap trace like cluster53 (only 1.92pp of headroom), noisy forgiveness flips from helping to hurting; cache-t-00 and msr_proj_0 have 5 to 6pp of headroom and only degrade, they do not invert. This single mechanism (recency pollution scaling with threads) explains all three traces. It predicts a fix: per-thread recency buffers (or scaling the buffer with thread count) should remove the pollution and the inversion. This connects directly to the thread-local embedding design (31.5).

### 31.5 Implication: thread-local embeddings (Jane's insight)

In replicated mode threads share no objects, so there is no inter-thread embedding relationship to capture and each thread's stream is already in trace order. So per-thread embedding state (embeddings, context, recency, counts) updated inline on the worker thread needs no applier, no log, and no lock, and it would recover throughput and fidelity together, it is N independent single-thread LRUForgive runs. Caveat: this is replicated-only; in shared mode threads do share keys, so embeddings are genuinely shared state. One wrinkle even in replicated mode: the single shared cache object means eviction can pick a victim owned by another thread, so a correct forgiveness read at eviction must route to the victim's owner's embedding, unless replicated mode uses N independent caches (then it is trivially perfect and lock-free). Open question for the paper: is the multithreaded evaluation replicated or shared, and is the replicated cache one shared object or N independent ones? That decides whether thread-local embeddings is the headline architecture.

### 31.6 Applier pinning: tested and rejected (placement is not the bottleneck)

To test whether the single applier's throughput ceiling was a scheduling/locality problem, the applier was pinned to a dedicated CPU (chosen from the process's numactl-allowed set, the highest allowed CPU, which is 29 under `--cpunodebind=0`). Result on cache-t-00, turbo-off, LRUForgive MQPS:

| t | unpinned | pinned |
|---|----------|--------|
| 1 | 0.15 | 0.19 |
| 2 | 0.24 | 0.27 |
| 4 | 0.37 | 0.31 |
| 8 | 0.55 | 0.36 |
| 16 | 0.73 | 0.37 |

Pinning helps at t=1/t=2 (better locality, applier not floating) but hurts from t=4 up, badly at t=16 (0.73 to 0.37). Cause is the NUMA topology (node 0 = CPUs 0-9,20-29; node 1 = 10-19,30-39; 10 physical cores/socket, 2 HT each): CPU 29 is a hyperthread sibling on node 0, and at high thread counts the workers (pinned to CPUs 0,2,...,30) already occupy node 0's cores, so the pinned applier is jailed on one contended sibling core while the unpinned applier could float to whatever core was momentarily free. Pinning throttles the whole pipeline to that one core's rate (~0.37). Rejected; the unpinned champion (float the applier) is retained.

The experiment was still decisive as a diagnosis. During the pinned t=8 run the workers sat at ~48% CPU (not ~99%), i.e. half-idle waiting on the applier, and throughput tracked the applier's core quality (up on a good core at t=1, down when jailed at t=16). That is the signature of a single-thread bottleneck: throughput is gated entirely by the one applier's serial compute rate, not by placement. This triangulates with the object-count hypothesis (heavy per-access/per-eviction embedding compute) and with Jane's finding that removing the applier via per-thread embeddings still left throughput poor. Conclusion: the ceiling is the embedding compute itself (per-access update on the write side, cosine on the eviction/read side), so the levers are removing the funnel (per-thread embeddings, replicated mode) and taking forgiveness compute off the eviction path (precomputed candidates), not applier placement.

## 32. Replicated mode on Jane's per-shard branch: the ceiling is the shared cache, not our code (Aug 8)

We are optimizing replicated mode (the decided direction). Tested Jane's `jane-test-main` per-shard design (stable commit `4e1c65d`): embedding state sharded per key-owner (`ShardState` per shard, per-shard recency, per-shard LRU), sharded by key owner (correct for replicated), one applier thread per shard. Reviewed correct: key-owner sharding, per-shard recency, and the eviction read routes to the victim's owner shard (`shardOfKey`), so the cross-thread-eviction wrinkle is handled.

### 32.1 Removing the per-shard appliers changed nothing (appliers are not the bottleneck)

Hypothesis (from Section 29): the per-shard appliers add spinning threads and cost throughput. Test: made `on_access` apply inline on the worker under the shard's `unique_lock`, calling the same `applyEventLocked` the applier used, which stops any applier thread from spawning. Result: **identical** throughput and miss ratio (cache-t-00 replicated t=8: 1.33 MQPS, miss 0.8133, both before and after). So the appliers were not the tax. Reason Section 29 does not transfer: with key-owner sharding each shard is one worker + one applier in lockstep, so at t=8 that is 16 threads on 40 cores, not oversubscribed; the spin was free because there were spare cores. Inline is still the cleaner design (no dead applier threads, no queues), and it is byte-identical on fidelity, so it is the version to keep, but it is a simplification, not a speedup.

### 32.2 The scaling wall is CacheLib's shared cache (LRU walls out too)

cache-t-00, replicated, cache scaled with threads, turbo-off, membind=0:

| t | size (MB) | LRU MQPS | LRU miss | LF MQPS | LF miss | gap (pp) | LF/LRU tp |
|---|-----------|----------|----------|---------|---------|----------|-----------|
| 1 | 512 | 0.25 | 0.8325 | 0.22 | 0.8213 | 1.12 | 0.88 |
| 2 | 1024 | 0.41 | 0.9851 anom | 0.41 | 0.8156 | n/m | 1.00 |
| 4 | 2048 | 0.90 | 0.8256 | 0.74 | 0.8139 | 1.17 | 0.82 |
| 8 | 4096 | 1.74 | 0.8248 | 1.33 | 0.8133 | 1.15 | 0.76 |
| 16 | 8192 | 2.25 | 0.8227 | 1.79 | 0.8117 | 1.10 | 0.80 |

Two reads. First, **LRU scales sublinearly and walls out around t=8 to 16** (2.25 MQPS at t=16 is only ~9x the t=1 rate, and t=8 to t=16 is just 1.3x for 2x the threads). LRU has none of our machinery (no applier, embeddings, or forgiveness), so this wall is CacheLib's single shared cache object (its internal lock domain under N-way concurrent access), not our code. Second, **LF tracks LRU at a roughly flat ~76 to 88% throughput ratio** at every thread count; the ratio does not fall with threads, so the forgiveness compute (eviction-path cosine) is a constant ~20% tax, not a scaling problem. The scaling ceiling is external to LRUForgive.

### 32.3 NUMA is a minor contributor

LRU t=16 under three memory policies: membind=0 2.25, `--interleave=all` 2.50, no numactl 2.43 MQPS. Interleaving recovers ~11%, so cross-NUMA memory (all memory on node 0 while workers spill to node 1 cores) is a real but minor part of the wall; the dominant ceiling is the shared-cache lock. Consistent across policies.

### 32.4 Implications

The embedding-side design (applier vs inline, backoff, log structure) does not move replicated-mode throughput, because the ceiling is CacheLib's shared cache, which caps LRU and LF equally. Backoff specifically is a no-op here (no applier to back off), and the log structure is unnecessary (no cross-thread order to preserve in replicated mode). The lever that would raise the ceiling is **N independent cache objects (one per thread)**, removing the shared lock domain so replicated mode can scale near-linearly; that also makes per-thread embeddings trivially lock-free (no cross-thread eviction reads). This is the next experiment, a hypothesis the data supports, not yet proven.

On miss ratio: in replicated mode the multithreaded gap equals the single-thread gap (~1.1pp for cache-t-00 at 512MB), because each thread is an independent single-thread run. So the gap is smaller than shared mode's ~5 to 6pp, and improving it is single-thread algorithm work, not threading work. The threading/pipeline design only affects throughput; the gap is set by the forgiveness algorithm and hyperparameters. The lever for a bigger replicated gap is hyperparameter tuning (Bayesian; paper 9.93% vs our 7.32% single-thread suggests headroom), not per-thread recency (already inherent per-shard) or precomputed candidates (throughput-only, gap-neutral). This surfaces a mode trade-off for the paper: replicated scales better but shows the single-thread gap; shared shows a larger gap but walls out on the shared cache.

## 33. Two-mode comparison across four traces (replicated per-thread vs shared log+backoff, Aug 8)

Per Jane's request, compared her replicated per-thread design against the shared log+backoff champion on all four traces, three base sizes each (operating point, 600, 1200MB), replicated cache scaled with threads, 4 reps. Key results and two findings that correct earlier hypotheses.

### 33.1 Replicated miss-ratio gap: small, positive, cache-size-sensitive

Per-thread minus LRU, pp (positive = forgiveness helps):

| trace / size | t=1 | t=4 | t=8 | t=16 |
|---|---|---|---|---|
| cache-t-00 512 | 1.13 | 1.20 | 1.16 | 1.05 |
| cache-t-00 600 | 1.17 | 1.19 | 1.19 | 1.10 |
| cache-t-00 1200 | 1.02 | 0.99 | 1.00 | 0.69 |
| msr_proj_0 256 | 0.54 | 0.90 | 0.14 | 0.86 |
| msr_proj_0 600 | 0.10 | 0.30 | 0.00 | 0.40 |
| cluster53 128 | 0.10 | -0.28 | -0.42 | -0.48 |

The replicated gap equals the single-thread gap (each thread is an independent run). cache-t-00 is the only trace with a clean, stable positive gap (~1pp), and it shrinks as the cache grows (1200MB drops to 0.69pp at t=16, less eviction pressure, less to forgive). msr_proj_0's gap mostly evaporates at larger caches. msr_prxy_1 is uninformative in replicated mode: at 600/1200MB the cache holds essentially the whole working set (miss ~0.003), and at 256MB the LRU non-determinism recurs (0.3574 x3 then ~0.25 on perthread reps).

### 33.2 Finding: the cluster53 inversion is NOT recency pollution (corrects Section 31.4)

cluster53 inverts (forgiveness worse than LRU) at t=4/8/16 at its 128MB operating point **in replicated mode**. This rules out the Section 31.4 hypothesis that the shared-mode inversion was recency-buffer pollution from thread interleaving: replicated mode has per-thread recency (no pollution possible), and it still inverts. So the inversion is intrinsic: **forgiveness is a genuinely poor fit for cluster53 under eviction pressure**, not a threading artifact. This is a real characterization result and it supersedes the pollution hypothesis. cluster53 has a small positive single-thread gap only when eviction pressure is low (large cache); under pressure (128MB, thread scaling raising effective pressure) forgiveness makes wrong rescues and loses to LRU.

### 33.3 Throughput: fixed per-eviction tax, brutal on low-miss traces

Per-thread throughput as a fraction of LRU: cache-t-00 (high miss) holds ~0.76 to 0.83 across threads. But low-miss traces where LRU is fast pay heavily: cluster53 t=1 is 0.44 vs LRU 0.92 (48%), msr_prxy_1 t=1 is 0.68 vs 2.92 (23%). Same mechanism as Section 30.4: forgiveness is a fixed per-eviction cost, so when LRU is fast (low miss), the relative tax is large.

### 33.4 Reconciling Jane's 34x eviction-function measurement

Jane traced `getEvictionIterator` in the replicated per-thread implementation at **34x slower than LRU**. That is the isolated eviction-function cost (the `avg_top_k` cosine walk), and it applies to both implementations, the inline change touched only the write path (`on_access`), not the eviction/read path, so the forgiveness read is identical. The reconciliation: a 34x-slower eviction function is only a ~20 to 80% end-to-end tax, because the eviction path is a small fraction of total request work (the cosine is a bounded 8x8 computation firing only on eviction candidates). The 34x is the per-eviction microbenchmark; the amortized system cost is 0.76 to 0.83 of LRU on cache-t-00, worse on low-miss traces. This is the direct motivation for moving forgiveness off the eviction path (precompute verdicts in the background): it would turn the 34x eviction into a cheap flag-read and recover most of the amortized tax. This is the highest-value throughput optimization for both modes.

## 34. S3-FIFOForgive: generalizing forgiveness beyond LRU (Aug 16)

Built S3-FIFOForgive: forgiveness applied to S3-FIFO instead of LRU, to test whether the embedding-rescue technique generalizes to a modern, already-strong policy. It does, with a small positive gap.

### 34.1 Design: where forgiveness hooks into S3-FIFO

S3-FIFO evicts differently from LRU. It has a small (probationary) FIFO and a main FIFO. On eviction from the small FIFO, an object that was re-accessed (freq > 0) is promoted to main; an object that was not (freq 0, a one-hit-wonder) is dropped and its key goes to a history/ghost structure. The evict-vs-promote decision lives in `S3FIFOList::getEvictionCandidate` (the datastruct layer), NOT in the MM policy wrapper (`MMS3FIFO` is thin).

The forgiveness hook is the freq-0 drop decision. In the small-FIFO branch of `getEvictionCandidate`, the code becomes a three-way decision: if `isAccessed` promote to main (S3-FIFO's own second chance); else if `shouldForgive` promote to main (our addition, embedding similarity to recent); else drop (S3-FIFO's one-hit-wonder eviction). This is the S3-FIFO analog of LRUForgive's tail rescue: it only fires on objects S3-FIFO would drop, so it is strictly additive.

### 34.2 Implementation

Four new files (parallel to the LRUForgive set, but the forgiveness decision is one layer lower, in the datastruct):
- `datastruct/S3FIFOForgiveList.h/-inl.h`: a copy of `S3FIFOList` with a non-owning `TARDISEmbeddingManager*` (set via `setEmbeddingManager`), forgive params, `shouldForgive` (the 3-part gate: access_count >= minAccess, has_embedding, avg_top_k_similarity >= threshold), and the three-way decision in `getEvictionCandidate`.
- `MMS3FIFOForgive.h/-inl.h`: mirrors `MMS3FIFO`, owns the embedding manager, passes it into the list, and calls `embMgr_->on_access(hashKey(node))` on every access in `recordAccess`.

KEY TRAP (resolved): the embedding manager must see the SAME object id on the access path and the eviction path. `MMS3FIFO`'s existing `hashNode` uses `folly::hasher`, but `MMLruForgive` uses `SpookyHashV2::Hash64`. S3-FIFOForgive uses `SpookyHashV2::Hash64` consistently in both the MM policy's `hashKey` and the list's `embKey`. If they had differed, forgiveness would silently never fire (nForgive_ stuck at 0).

Wiring (all done): `MMS3FIFOForgive::kId = 9` (unique; existing ids 1-6,8, with 7 commented), `S3FIFOForgiveCacheTrait` in CacheTraits.h (mirrors `S3FIFOCacheTrait`, only MMType differs), extern template + `S3FIFOForgiveAllocator` alias in CacheAllocator.h/.cpp, `s3fifoforgive` target in mybench/CMakeLists.txt with `USE_S3FIFOFORGIVE`, cache.h alias branch, and cache.cpp MCACHE branch (bounds the embedding store at 5x est items, identical to LRUForgive, so the two forgiveness policies share the same embedding budget).

### 34.3 First result and the overlap caveat

cache-t-00, 512MB, t=1: s3fifo miss 0.7750 (deterministic), s3fifoforgive miss ~0.768 (small positive gap ~0.7pp). Two observations. First, S3-FIFO's own baseline (0.7750) already beats LRU (0.8325) by ~6pp, so forgiveness is adding value on top of a strong policy, which is the "forgiveness generalizes beyond LRU" contribution. Second, the gap is smaller than LRUForgive's ~1.1pp single-thread gap on the same trace, as expected: S3-FIFO already forgives one-hit-wonders via its `isAccessed` check, so embedding-forgiveness only rescues the freq-0 objects S3-FIFO drops that have semantic similarity, a narrower target than LRU's blind tail. Throughput tax ~37% (0.27 vs 0.43 MQPS), same eviction-path cosine cost as LRUForgive.

CAVEAT (t=1 non-determinism): both LRUForgive (0.7758-0.7760) and S3-FIFOForgive (0.7678-0.7774) vary slightly run-to-run at t=1, traced to the shared embedding manager (not S3-FIFO-specific). S3-FIFOForgive's variance band (~1pp) currently spans its gap, so the single-run gap is not yet separable from noise. Deprioritized for now, but it is a known caveat: t=1 forgiveness numbers are not bit-exact, so gaps should be reported as multi-run means, and any headline S3-FIFOForgive gap needs the variance pinned down first.

### 34.4 Next

Test single-thread across traces (msr_proj_0, cluster53) to see if the gap generalizes or is cache-t-00-specific (cluster53 may invert, as it does for LRUForgive). MT is expected to hit the Section 23 concurrency defect in `getEvictionCandidate` (non-atomic check-then-remove), so the precompute path (`getEvictionCandidate0`, currently disabled) is where S3-FIFOForgive MT and the eviction-latency mitigation converge: precomputing forgiveness verdicts off the eviction path would address the defect and the ~37% cosine tax together.

## 35. Subsampling table: per-access update sampling (Aug 19)

**CORRECTION (Aug 19, later): Sections 35.4-35.7 below are INVALIDATED by an encoding bug found afterward. The jane-perthread numbers in those sections (the "1.1pp gap", the "sharding shrinks the gap ~4.5pp", the "dominance finding", the "nature of the two designs" fragmented-recency explanation, the convergence-at-t16 story) were all artifacts of a shard-decode bug, NOT real properties of sharding. See Section 36 for the corrected result. Kept below for provenance, but do not cite them.**

**The bug:** jane-perthread's `shardOfKey` decoded shard = (value / 1e9) % kMaxThreads, but the key encodes obj_id % UINT32_MAX + reader_id*1e9, and obj_id % UINT32_MAX (up to 4.29e9) bled into the /1e9 division. The ACCESS path used cachedShardOfKey (thread-local cached, so consistent per worker) but the EVICTION path used uncached shardOfKey(candidate), which computed a DIFFERENT scattered shard for the same object. So forgiveness looked up embeddings in the wrong shard, found nothing, and rescued almost nothing. The "1.1pp per-thread gap" was forgiveness silently half-broken, not the cost of sharding. Fixed with the bit-shift encoding ((obj_id % UINT32_MAX) | (reader_id << 32), decode value >> 32). After the fix, jane-perthread LRUForgive gives the FULL ~5.6pp gap, same as single-manager, and holds it across thread counts (Section 36).

## 35 (original, subsampling shape, still valid). Subsampling table: per-access update sampling

Jane's first-priority request: a sampling-rate vs miss-ratio table in synchronous mode, to measure the accuracy/throughput tradeoff of the embedding compute (her ablation showed the per-access embedding math, context advance + embedding update, is the applier bottleneck). Built two subsample variants on tardis-implementation (single-manager, synchronous), env-driven (TARDIS_SAMPLE_RATE, TARDIS_SAMPLE_MODE), all in TARDISEmbeddingManager.h, no harness changes. Measured on cache-t-00 512MB t=1, where the full single-thread gap is ~5.65pp (LRU 0.8325, LRUForgive 0.7760). 3 reps/cell, means below.

IMPORTANT distinction from the earlier subsampling experiment (Section documented at doc line ~1257): that one was per-OBJECT (forward 1-in-N objects, drop all accesses to the rest), which erodes the gap one-for-one because dropped objects can never be forgiven. This is per-ACCESS: every object stays eligible, its embedding is just updated on a fraction of its accesses. Per-access degrades far more gracefully than per-object. They are different experiments.

Two modes:
- **Mode 0 (skip whole event):** on a non-sampled access, skip everything (no access_count, no recency, no embedding update).
- **Mode 1 (skip only the math):** on a non-sampled access, still increment access_count, but skip context advance / embedding update/init / recency.

### 35.1 Mode 0 (skip whole event), the viable lever

| rate | miss ratio | gap kept | throughput (MQPS) | tp gain |
|---|---|---|---|---|
| 1.0  | 0.7760 | 100% | 0.17 | 0% |
| 0.5  | 0.7807 | 92%  | 0.19 | +10% |
| 0.25 | 0.7878 | 79%  | 0.20 | +16% |
| 0.1  | 0.8009 | 56%  | 0.21 | +23% |
| 0.05 | 0.8110 | 38%  | 0.22 | +25% |
| 0.01 | 0.8272 | 9%   | 0.23 | +33% |

(gap kept = (LRU - miss) / 5.65pp; tp gain = throughput vs rate-1.0 baseline)

### 35.2 Mode 1 (skip only the math), the cautionary result

| rate | miss ratio | gap kept | throughput (MQPS) | tp gain |
|---|---|---|---|---|
| 1.0  | 0.7761 | 100% | 0.17 | 0% |
| 0.5  | 0.8075 | 44%  | 0.19 | +14% |
| 0.25 | 0.8222 | 18%  | 0.21 | +24% |
| 0.1  | 0.8291 | 6%   | 0.22 | +29% |
| 0.05 | 0.8311 | 2%   | 0.22 | +29% |
| 0.01 | 0.8325 | 0%   | 0.22 | +29% |

### 35.25 Mode 2 (deterministic global every-Nth access, skip whole event)

Same as mode 0 but deterministic: update on every Nth access (N = round(1/rate)) instead of random probability. Tests whether the sampling pattern matters or only the rate.

| rate | miss ratio | gap kept | throughput (MQPS) |
|---|---|---|---|
| 1.0  | 0.7759 | 100% | 0.17 |
| 0.5  | 0.7807 | 92%  | 0.19 |
| 0.25 | 0.7881 | 79%  | 0.19 |
| 0.1  | 0.8008 | 56%  | 0.20 |
| 0.05 | 0.8112 | 38%  | 0.21 |
| 0.01 | 0.8270 | 10%  | 0.22 |

**Mode 2 is statistically identical to mode 0** (every pair within ~0.0003, inside the t=1 noise band). So the sampling pattern (random vs evenly-spaced) does not matter; only the rate does. The graceful degradation is a property of the sampling rate, not of randomness. Practical upshot: use whichever is convenient; deterministic every-Nth is available for free if reproducibility is wanted (though at rate 1.0 the gate is a no-op so the shared-manager's own t=1 nondeterminism still shows; the sampling itself adds none).

### 35.4 [INVALIDATED, see Section 36] Same sweep on jane-perthread (sharded, 1.14pp gap), and the dominance finding

Ran all three modes on jane-perthread (cache-t-00 512 t=1, LRU 0.8325, LRUForgive 0.8211, gap 1.14pp). Both findings replicate: mode 0 ≈ mode 2 (skip-whole, random ≈ deterministic), mode 1 (skip-math) degrades sharply.

| rate | m0 miss | m0 gapKept | m1 miss | m1 gapKept | m2 miss | tp (all) |
|---|---|---|---|---|---|---|
| 1.0  | 0.8211 | 100% | 0.8211 | 100% | 0.8211 | 0.21 |
| 0.5  | 0.8217 | 95%  | 0.8275 | 44%  | 0.8217 | 0.22 |
| 0.25 | 0.8230 | 83%  | 0.8302 | 20%  | 0.8229 | 0.22 |
| 0.1  | 0.8256 | 61%  | 0.8319 | 5%   | 0.8256 | 0.23 |
| 0.05 | 0.8277 | 42%  | 0.8322 | 3%   | 0.8276 | 0.23 |
| 0.01 | 0.8313 | 11%  | 0.8325 | 0%   | 0.8313 | 0.23 |

Throughput gain from subsampling is smaller here in relative terms (+5-11%) than on single-manager (+10-33%), because this branch starts less lock-bound (0.21 vs 0.17 MQPS at full rate).

**THE DOMINANCE FINDING (t=1): subsampling the single-manager beats the per-thread sharding on the throughput/miss tradeoff.** Cross-branch, cache-t-00 512 t=1:
- single-manager, rate 1.0: miss 0.7760 @ 0.17 MQPS
- single-manager, rate 0.5: miss 0.7807 @ 0.19 MQPS  (keeps 92% of a 5.65pp gap)
- per-thread,     rate 1.0: miss 0.8211 @ 0.21 MQPS  (keeps ~20% equivalent; 1.14pp gap)

Single-manager subsampled to 0.5 gets miss 0.7807 at 0.19 MQPS, nearly the same throughput as full per-thread (0.21) but a far better miss ratio (0.7807 vs 0.8211). So sharding trades ~4.5pp of gap for ~24% throughput, while subsampling buys comparable throughput for only ~0.5pp. Subsampling is a much more efficient way to spend miss-ratio for throughput than sharding is.

CAVEAT: this is all t=1, where sharding shows only its cost (fragmented recency) and none of its benefit (parallel throughput under load). The per-thread appliers were built to scale at high thread counts where a single applier saturates. The fair test of whether sharding earns its gap loss is a single-manager-vs-per-thread THROUGHPUT comparison at t=8/16, RUN BELOW in 35.5. Subsampling itself does NOT need a full per-thread-count sweep: the shape (skip-whole > skip-math, mid-range sweet spot, pattern-independent) is a per-access property that won't change qualitatively with thread count.

### 35.5 [INVALIDATED, see Section 36] High-thread comparison: the t=1 dominance finding does NOT generalize (Aug 19)

Ran single-manager vs per-thread throughput AND miss ratio at t=4/8/16 (cache-t-00, replicated, size/hp scaled per run.sh: t4=2048/27, t8=4096/28, t16=8192/29, 2 reps). This is the fair test of whether sharding earns its gap loss.

| t | SM tp | PT tp | PT/SM | SM miss | PT miss | SM gap | PT gap |
|---|---|---|---|---|---|---|---|
| 1 | 0.17 | 0.21 | 1.24 | 0.776 | 0.821 | 5.6pp | 1.1pp |
| 4 | 0.68 | 0.74 | 1.09 | 0.787 | 0.814 | 4.5pp | 1.9pp |
| 8 | 1.08 | 1.33 | 1.23 | 0.797 | 0.813 | 3.5pp | 2.0pp |
| 16 | 1.56 | 1.77 | 1.13 | 0.810 | 0.811 | 2.3pp | 2.1pp |

THE CROSSOVER: single-manager's miss ratio DEGRADES with threads (gap 5.6pp -> 2.3pp) while per-thread's stays FLAT (~2.0pp). By t=16 they converge (SM 2.3pp, PT 2.1pp, essentially equal), and per-thread is faster throughout (1.1-1.2x). Reason: single-manager has one applier; at high thread counts it cannot keep up, so embedding updates lag evictions and forgiveness works with stale state, eroding the gap. Per-thread's per-shard appliers each keep pace with their worker, so its gap is thread-count-stable.

CORRECTED SYNTHESIS (supersedes the t=1 "dominance finding" in 35.4): the t=1 result that "subsampling single-manager dominates sharding" is a single-thread artifact. Single-manager's 5.6pp gap collapses to 2.3pp under threading regardless of subsampling, because the applier saturates. Per-thread sacrifices single-thread gap that would not have survived at scale anyway. At the realistic multi-threaded operating point (t=16), per-thread gives EQUAL miss ratio AND better throughput. So the sharded architecture is justified: its cost (fragmented recency) only matters single-threaded, and its benefit (thread-stable gap + higher throughput) is exactly what shows up under load. This is the architecture argument for the paper.

### 35.6 [INVALIDATED, see Section 36] The nature of the two designs (why the gaps behave oppositely)

The entire single-manager-vs-per-thread contrast reduces to one shared ordered embedding state with one applier vs. many sharded single-writer states each with its own applier.

**Single-manager: high gap, degrades with load.** One global embedding space and one global recency buffer. Forgiveness compares a candidate against the GLOBAL recent set, the complete cross-thread picture, which is why the gap is large at t=1 (5.6pp): best possible information. But one applier does all embedding compute. Embedding updates are order-sensitive (they accumulate state; out-of-order application corrupts learned relationships), so a single applier enforces global sequence order via an N-way merge. As worker threads scale, they produce update events faster than the one applier can apply them; the applier lags, so forgiveness reads STALE embeddings (updates queued but not applied), and the gap erodes (5.6 -> 2.3pp by t=16). The single applier is a serial bottleneck and its lag degrades the gap under load.

**Per-thread: low gap, but load- and subsample-stable.** State is sharded by key-owner: each shard has its own embedding space, recency buffer, and applier. Two opposite consequences:
- Smaller gap: forgiveness compares a candidate only against ITS OWN shard's recent set, a fragmented/partial view of recency, not the global one. Less information, weaker rescues, smaller gap (1.1-2pp). This is the cost of sharding.
- Stable gap: each shard is single-writer (thread i produces only shard i's events, applier i applies only shard i), so per-shard order is preserved trivially and adding threads adds shards AND appliers one-to-one. No applier ever falls behind, no serial bottleneck, embeddings stay fresh at every thread count. So the gap does not degrade with load (2.0pp at t=1, 2.0pp at t=16), and subsampling stays consistent for the same reason.

**Why multiple appliers are safe in per-thread but not single-manager:** the single-manager needs one applier because there is one global ordered state, and concurrent appliers would reapply out of order (the corruption the merge prevents). Sharding makes each shard an independent single-writer domain, so multiple appliers never touch the same embedding, no cross-shard ordering or contention. The thing that forced one applier (one global ordered state) is exactly what sharding removes. So the multiple-applier freedom and the gap loss are two sides of one coin: sharding buys per-shard parallelism (many appliers, no bottleneck) at the cost of the global recency signal (smaller gap).

**The convergence** at t=16 (SM 2.3pp, PT 2.1pp) is single-manager's applier-lag eroding its gap down to per-thread's level, while per-thread held that gap all along at higher throughput. That is why per-thread wins at scale: same gap, no bottleneck.

### 35.7 [INVALIDATED, see Section 36] Subsampling under load: nearly free at high thread counts

Ran mode 0 (skip-whole) at rates 0.5/0.25, t=4/8/16, both branches (cache-t-00, 2 reps). The t=1 penalty (subsampling to 0.5 cost ~8% of the single-manager gap) SHRINKS to almost nothing under load:

| t | SM gap 1.0->0.5 | SM tp 1.0->0.5 | PT gap 1.0->0.5 | PT tp 1.0->0.5 |
|---|---|---|---|---|
| 4  | 4.51->4.38pp | 0.66->0.67 | 1.85->1.79pp | 0.74->0.81 |
| 8  | 3.49->3.46pp | 1.09->1.19 | 1.95->1.87pp | 1.32->1.44 |
| 16 | 2.32->2.33pp | 1.55->1.72 | 1.95->1.87pp | 1.81->1.89 |

At t=16, subsampling to 0.5 leaves the gap essentially unchanged (2.32->2.33pp single-manager) while raising throughput +11%. Reason: at t=1 the applier keeps up so embeddings are fresh and skipping half the updates measurably degrades them; at t=16 the applier is already lagging so embeddings are already stale, and skipping half barely adds staleness while cutting applier load. So subsampling is a BETTER deal under load than at t=1, the opposite of the naive fear. Caveat: subsampling does NOT recover single-manager's applier-saturation gap loss (stuck ~2.3pp at t=16 regardless); it just stops costing gap. It is free throughput at scale, not a gap-rescue.

### 35.3 Findings

**Skip-whole (mode 0) beats skip-only-math (mode 1) decisively.** At rate 0.5, mode 0 keeps 92% of the gap vs mode 1's 44%; at rate 0.25, 79% vs 18%. This is the opposite of the initial prediction (that keeping the cheap bookkeeping would help). The reason: mode 1 desynchronizes the state. access_count races ahead (counting every access), so objects cross min_access_count and get embeddings created, but those embeddings are then barely updated and recency is barely recorded, so forgiveness compares against many stale embeddings and a sparse recency buffer. Mode 0's surviving accesses are complete, coherent updates on a representative random subset, so the (sparser) state it maintains is accurate. Clean skipping beats half-updating. Practical guidance: subsample the whole event, never just the math.

**The tradeoff is not linearly inverse; it worsens as sampling deepens.** Throughput gain per pp of miss-ratio loss falls from ~21 (at rate 0.5) to ~6 (at rate 0.01). Early cuts (rate 0.5-0.25) buy most of the available throughput (+10-16%) for little gap loss (keep 92-79%); deep cuts pay full miss-ratio price for shrinking throughput return. Throughput saturates around +33% because the embedding compute is not the only cost (the cache itself, the state lock, and trace replay remain), while miss ratio has no floor until it reaches LRU. So the two diverge at the low-rate extreme rather than tracking inversely. The usable operating region is the mid-range (rate 0.5-0.25): near-full gap, meaningful compute reduction.

**Caveat on this branch's throughput:** the single-manager architecture tops out at ~0.17-0.23 MQPS here (one lock, one applier serializing). The per-thread sharded branch (jane-perthread) runs ~0.24+ and is more compute-bound, so subsampling should show a larger relative throughput gain there. The sharded-branch subsample table is the next run.

**Cross-branch context (per-thread sharding shrinks the gap):** single-manager (tardis-implementation) gives the full 5.65pp gap at 0.17 MQPS; per-thread sharded (jane-perthread) gives ~1.1pp at 0.24 MQPS. Confirmed same trace/size/mode/threads (cache-t-00 512 t=1, replicated), and replicated vs shared are identical at t=1 (0.7757 vs 0.7758), so the gap difference is the sharding, not the mode. Sharding fragments the recency buffer and embedding space (a candidate is compared only against its own shard's recent set, a partial signal), trading ~4.5pp of gap for throughput. This is a separate finding worth raising with Jane independent of subsampling.

## 36. CORRECTED: per-thread holds the full gap across thread counts (Aug 19)

After fixing the shardOfKey encoding bug (see the correction banner at Section 35), jane-perthread LRUForgive gives the FULL ~5.6pp gap, not the bug-suppressed 1.1pp. And it HOLDS that gap across thread counts, where single-manager's gap degraded under applier lag.

cache-t-00, replicated, per-thread LRUForgive with the fix (gap vs matching-thread LRU):

| t | PT LRUForgive | LRU (matching t) | gap | PT throughput |
|---|---|---|---|---|
| 1  | 0.7757 | 0.8325 | 5.68pp | 0.19 |
| 4  | 0.7721 | 0.8257 | 5.36pp | 0.62 |
| 8  | 0.7727 | 0.8248 | 5.21pp | 1.05 |
| 16 | 0.7747 | 0.8227 | 4.80pp | 1.46 |

(t=2 LRU shows a 0.985 anomaly, the known t=2 non-determinism on this trace; ignored. LRU baseline drifts slightly with thread count because replicated cache-size scaling shifts the effective working set; gaps use matching-thread LRU.)

CORRECTED SYNTHESIS (supersedes 35.4-35.7): the sharded per-thread design does NOT cost gap. Its per-shard single-writer appliers keep embeddings fresh at every thread count, so it holds the full ~5.6pp gap under load while scaling throughput (0.19 -> 1.46 MQPS). Single-manager, by contrast, has one applier that saturates under load, so ITS gap degrades (5.6 -> 2.3pp at t=16). So per-thread is strictly better at scale: same (full) gap as single-manager at t=1, but unlike single-manager it KEEPS that gap as threads grow, and it has higher throughput. The earlier "sharding shrinks the gap" conclusion was entirely the encoding bug corrupting the eviction-path shard lookup. The sharded architecture is vindicated: full gap, thread-stable, parallel appliers, no bottleneck.

This also means all jane-perthread numbers measured before the fix (the 1.1pp baseline, the perthread subsample table in 35.4) must be re-measured with the fix. Re-measurement in progress.

### 36.1 Corrected per-thread subsampling under load (mode 0, skip-whole, with fix)

cache-t-00, per-thread, mode 0, rates 1.0/0.5/0.25 across thread counts (2 reps):

| t | rate 1.0 miss | rate 0.5 miss | rate 0.25 miss | tp 1.0->0.5->0.25 |
|---|---|---|---|---|
| 1  | 0.7760 | 0.7805 | 0.7878 | 0.18 / 0.20 / 0.21 |
| 4  | 0.7721 | 0.7760 | 0.7824 | 0.62 / 0.67 / 0.73 |
| 8  | 0.7727 | 0.7764 | 0.7825 | 1.05 / 1.17 / 1.29 |
| 16 | 0.7748 | 0.7776 | 0.7829 | 1.47 / 1.61 / 1.73 |

Unlike single-manager (where subsampling became nearly free under load BECAUSE the applier was already saturated and embeddings already stale), per-thread shows a CONSISTENT gap-for-throughput trade at every thread count: rate 0.5 costs ~0.4pp of miss ratio for ~+10% throughput whether at t=1 or t=16. Reason: per-thread's appliers never saturate, so embeddings are always fresh, so subsampling always genuinely removes fresh updates. This is the honest, load-independent version of the subsampling tradeoff, and it's the one to report since per-thread is the architecture that holds the gap.

### 36.2 S3FIFOForgive ported to jane-perthread, works

Ported S3FIFOForgive to jane-perthread (adapted on_access to (hashKey, cachedShardOfKey), shouldForgive to hashKey + uncached shardOfKey + 2-arg manager reads, with the fixed >> 32 shard decode). cache-t-00 512 t=1: s3fifoforgive 0.7679 vs s3fifo 0.7750 = +0.71pp, identical to the tardis-implementation result. Forgiveness fires, MCACHE bounding active. The port is clean; the branch-wide encoding fix carried through so the eviction-path shard lookup matches the access-path shard.

## 37. S3FIFOForgive design decision: promote-to-main beats keep-in-small (Aug 23)

Tested two rescue actions for a forgiven freq-0 small-FIFO candidate, via env var TARDIS_S3FF_KEEP_IN_SMALL. cache-t-00 512 t=1:

| variant | miss ratio | gap vs s3fifo | throughput |
|---|---|---|---|
| s3fifo (baseline) | 0.7750 | - | 0.43 |
| s3fifoforgive, promote-to-main (default) | 0.7679 | +0.71pp | 0.34 |
| s3fifoforgive, keep-in-small | 0.7723 | +0.27pp | 0.27 |

Promote-to-main wins decisively: it recovers +0.71pp vs keep-in-small's +0.27pp (less than half), and is faster (0.34 vs 0.27 MQPS; keep-in-small re-processes forgiven objects through the small FIFO repeatedly). The more committal rescue captures more reuse. DECISION: S3FIFOForgive uses promote-to-main; the final sweep runs with TARDIS_S3FF_KEEP_IN_SMALL unset.

IMPLEMENTATION NOTE: keep-in-small can infinite-loop getEvictionCandidate if every small-FIFO tail candidate is forgivable (forgiven objects re-enter the small FIFO and never leave, so nothing is ever evicted). Fixed with a per-call forgive cap (maxForgivesPerCall) that forces eviction after N forgives in one call, guaranteeing progress. Promote-to-main does not hit the cap (forgiven objects leave the small FIFO for main), so the cap is inert for the default. A third variant (mark-as-accessed = let the forgiven object earn normal S3-FIFO promotion one lap later) was considered but not tested: it is a less-committal version of promotion, and since promote-to-main already beat the less-committal keep-in-small, mark-as-accessed is expected to land between them, not to win.

S3FIFOForgive baseline (both branches, t=1, 3 reps): s3fifoforgive ~0.768 vs s3fifo 0.7750 = +0.7pp on both single-manager and jane-perthread. Multi-thread not run: base S3-FIFO has a pre-existing concurrency defect in getEvictionCandidate (non-atomic check-then-remove) that hangs at t>=4, orthogonal to forgiveness. S3FIFOForgive is reported single-threaded; the final sweep runs S3FIFO/S3FIFOForgive at t=1 only.

## 38. FINAL SWEEP results (Aug 23-25): headline solid, three anomalies flagged

Full sweep, both branches, all 4 traces x 3 base sizes, LRU+LruForgive at t=1/2/4/8/16, S3FIFO+S3FIFOForgive at t=1, promote-to-main, full fidelity, defaults, 3 reps. Files: results/final_sweep_perthread.csv (432 rows), results/final_sweep_single-manager.csv (216 rows). Analysis: setup-scripts/analyze_sweep.py (keys on base size = size_mb/threads).

### 38.1 THE HEADLINE (solid, publication-ready): per-thread holds the gap, single-manager degrades

LruForgive gap vs LRU across threads, operating-point size, t=2 excluded (see 38.3):

cache-t-00 (base 512):   PT 5.64/5.32/5.24/4.76pp   SM 5.69/3.81/2.78/1.31pp   (t=1/4/8/16)
msr_proj_0 (base 256):   PT 6.22/4.13/3.87/2.85pp   SM 6.62/2.65/1.74/0.25pp

Two independent traces, identical pattern: per-thread holds ~5pp (cache-t-00) / ~3-6pp (msr_proj_0) roughly flat across thread counts; single-manager collapses toward 0 as its single applier saturates. At t=16 per-thread is 3-4x better than single-manager on the same policy. Both branches identical at t=1 (as they must be), then diverge under load. This is the paper.

Throughput (Table 3) reinforces it: single-manager throughput goes BACKWARD under load on msr_proj_0 (0.42->0.39->0.35 at t=4/8/16) as the saturated applier adds contention, while per-thread climbs. msr_prxy_1: per-thread 4.0 MQPS at t=16 vs single-manager 0.55. Per-thread wins on both axes (gap + throughput).

Per-trace verdict (t=1, operating size): cache-t-00 +6.77% rel [WINS], msr_proj_0 +12.25% [WINS], cluster53 +7.33% [WINS at t=1 but see 38.4], msr_prxy_1 +33.80% [WINS but see 38.4].

S3FIFOForgive (t=1): cache-t-00 +0.72pp both branches, holds across sizes (0.72/0.66/0.35 shrinking with size). Clean on cache-t-00.

### 38.2 ANOMALY 1 [PARTIALLY RESOLVED - median is fine, but real intermittent instability found]: msr_proj_0 S3FIFOForgive

The sweep's -4.26pp "regression" was a corrupted rep (0.5547 among 0.4166/0.4211). Median is fine: across 20 fresh runs, s3fifoforgive median ~0.4207 vs s3fifo 0.4216 = +0.09pp (neutral-to-slightly-positive, NO systematic regression).

BUT: S3FIFOForgive has an INTERMITTENT instability that s3fifo does NOT. In 10 runs, s3fifo values were tight (0.4215-0.4257); s3fifoforgive had 9 tight (~0.4205) plus one at 0.4981, and the sweep produced a 0.5547. Two high-side ~8-13pp blowups across ~13 s3fifoforgive runs, zero for s3fifo. Not environmental (would hit s3fifo too); it is forgiveness-specific and nondeterministic. Hypothesis: a rare timing/phase alignment where forgiveness promotes a burst of low-value objects to main (transiently-similar embeddings during a trace phase transition), polluting main and spiking miss ratio for the rest of the run. maxForgivesPerCall bounds it per-call but not across calls.

DISPOSITION: median-of-5 gives a correct headline and unblocks the MT work (do not block on this). But before publication, characterize (20+ reps/trace, outlier frequency) and likely fix, probably a stricter forgive gate (higher threshold / lower maxForgives), ties into hyperparameter tuning. A reviewer running the code and hitting 0.4981 when the table says 0.42 is a credibility risk.

LESSON stands: all-traces runs use MEDIAN of >=5 reps. NOTE on binary output: ~8 progressive miss ratios per run (checkpoints through the 24h replay), last line is converged final; tail -1 is correct.

### 38.3 ANOMALY 2 (likely same bug): msr_prxy_1 branch discrepancy at t=1

msr_prxy_1 base 256 t=1: S3FF PT +0.98pp vs SM +4.76pp; LruForgive PT +11.16pp vs SM +13.36pp. Branches disagree ~2pp at t=1 where they should match. Same signature as Anomaly 1 (non-cache-t-00 trace, branch mismatch, key encoding). Probably one fix for both. Note msr_prxy_1 t=1 sits on top of the cliff anomaly (38.5) so its absolute numbers are already suspect.

### 38.4 ANOMALY 3 (exclude or root-cause): t=2 universally broken

Every trace: LRU at t=2 gives impossible miss ratios > the t=1 value (cache-t-00 0.9545, base-600 0.9834). All t=2 rows contaminated (the +18/+22pp "gaps" at t=2 are artifacts of a broken LRU baseline, not real). Only t=2, every trace, so a 2-thread replay/warmup artifact (trace-split across 2 readers likely mis-warms or double-counts). DECISION for paper: exclude t=2, report t=1/4/8/16. Root-cause optional.

### 38.5 Known non-blockers

cluster53 t>=4 mostly NA (OOM/timeout on scaled sizes >= ~10GB on NUMA node 0); cluster53 is the inverting/flat trace (forgiveness ~0 or negative at larger sizes), t=1/t=2 cells suffice to show it. msr_prxy_1 t=1 cliff (0.33 at t=1 collapses to ~0.017 at t=2, 20x) is the TTL/Reaper pathology, publication blocker IF prxy_1 is in the paper (open question).

### 38.6 Bottom line

Headline (per-thread holds gap + scales throughput; single-manager degrades) is fully analyzed and solid on cache-t-00 and msr_proj_0. NOT publication-clean until Anomalies 1+2 (the non-cache-t-00 branch-discrepancy bug, likely one key-encoding fix) are diagnosed. t=2 excluded. cluster53/msr_prxy_1 reporting depends on whether they're in the paper.

## 39. S3FIFO MT: segfault (not hang) on real traces, explained by trace characterization (Aug)

### 39.1 It is a SEGFAULT, not a hang

S3FIFO on cache-t-00 t=4 runs ~25M requests then SEGFAULTs (exit 139), it does not livelock. The non-atomic check-then-remove race in getEvictionCandidate (concurrent removeTail on shared FIFO nodes) causes use-after-free / double-free, i.e. memory corruption -> crash, not a spin. (Careful: piping the run through `| tail -1` masks this as exit 0, that is tail's exit code; run without a pipe to see 139.) So reporting S3FIFO/S3FIFOForgive at t=1 only on real traces is correct, the MT runs crash. Confirmed on jane-perthread.

MT S3FIFO/S3FIFOForgive numbers ARE obtainable on the zipf generated trace (run.sh, via libCacheSim data_gen.py, alpha 1.0), which does not trigger the crash. That is how the TARDIS paper got MT S3FIFO numbers. Full zipf 4-policy matrix (t=1..16, jane-perthread): s3fifo scales 2.2->12.3 MQPS, s3fifoforgive 0.5->4.49, lru 1.6->2.2 (LRU barely scales), lruforgive 0.46->2.0. Miss ratio ~neutral for forgiveness on zipf (0.0457-0.051), see 39.2 for why. Forgiveness throughput overhead: S3FIFOForgive stays ~23-37% of S3FIFO (S3FIFO keeps scaling so overhead stays exposed); LRUForgive climbs 29%->91% of LRU (LRU stalls so overhead hides).

FULL ZIPF MT TABLE (jane-perthread, zipf1.0_1_100, single-run values, cache size scaled 4000MB*t, hp 21+log2 t). NOTE: single runs, not median-of-5; re-run with median before final reporting (S3FIFOForgive has intermittent outliers, see 38.2).

Throughput (MQPS):
| t | lru | lruforgive | s3fifo | s3fifoforgive |
|---|---|---|---|---|
| 1  | 1.60 | 0.46 | 2.20 | 0.50 |
| 2  | 1.74 | 0.75 | 2.61 | 0.87 |
| 4  | 2.61 | 1.31 | 5.10 | 1.72 |
| 8  | 2.30 | 1.81 | 9.45 | 2.88 |
| 16 | 2.19 | 2.00 | 12.30 | 4.49 |

Miss ratio:
| t | lru | lruforgive | s3fifo | s3fifoforgive |
|---|---|---|---|---|
| 1  | 0.0457 | 0.0457 | 0.0457 | 0.0457 |
| 2  | 0.0457 | 0.0457 | 0.0457 | 0.0457 |
| 4  | 0.0457 | 0.0457 | 0.0467 | 0.0457 |
| 8  | 0.0460 | 0.0469 | 0.0497 | 0.0472 |
| 16 | 0.0459 | 0.0474 | 0.0507 | 0.0506 |

Reading: S3FIFO scales 5.6x (2.2->12.3), LRU only 1.4x (plateaus ~t=4). Both forgiveness variants scale (s3fifoforgive 0.5->4.49 = 9x; lruforgive 0.46->2.0 = 4.3x). Miss ratio flat/neutral for forgiveness (zipf has nothing to rescue). This is the MT throughput-scaling result; the miss-ratio gap comes from the real traces at t=1 (Section 38).

### 39.2 Trace characterization: concrete evidence for both the crash and the gap (setup-scripts/characterize_trace.py, 5M reqs each)

| metric | zipf | cache-t-00 |
|---|---|---|
| unique objects | 571K | 1,675K (~3x) |
| accesses/object | 8.75 | 2.99 |
| one-hit-wonders | 48.5% | 66.2% |
| top-1% capture | 64.2% | 28.6% |
| hot-set churn (Jaccard, 200k win) | 0.829 | 0.879 |

cache-t-00 pushes ~3x more unique objects through the cache, 2/3 one-hit-wonders, with a FLAT popularity distribution (top-1% capture only 29% vs zipf's 64%). zipf has a small, concentrated, stable hot set. This ONE characterization explains both phenomena:
- THE CRASH: the segfault is a use-after-free in getEvictionCandidate's concurrent removeTail, exposed by EVICTION RATE (how often threads concurrently evict). NOTE: churn was similar (0.83 vs 0.88), so churn is NOT the discriminator; the discriminator is eviction pressure. zipf concentrates 64% of traffic on the top 1% (resident hot set, rarely evicted) so it evicts rarely and the race seldom fires. cache-t-00 spreads traffic (top-1% only 29%) with 66% one-hit-wonders, a flood of distinct low-reuse objects each forcing an eviction, so it evicts constantly and the race fires -> crash. This is strong circumstantial evidence (eviction pressure differs sharply, and evicts are where the race lives), NOT proof. Clean proof would be a low-alpha zipf (near-uniform, high eviction) crashing while high-alpha (concentrated) does not; data_gen.py supports the alpha sweep.
- THE GAP: cache-t-00's 66% one-hit-wonders + fat low-reuse tail is exactly the population forgiveness can rescue (objects that look cold but will be reused) -> ~5.6pp gap. zipf's reusable objects are already obviously hot (top-1% = 64% of traffic), nothing to rescue -> neutral. So zipf is a throughput benchmark, real traces are the miss-ratio benchmark.

NOTE on zipf: it is SKEWED but STATIONARY, not uniform. data_gen.py draws every request i.i.d. from one fixed Zipf(alpha=1.0) distribution, no phases, no working-set shift, no new objects over time. Skew = concentrated hot set; stationarity = distribution time-invariant. Record size = 24 bytes (480MB/20M zipf; 235.8MB/9.83M cache-t-00), obj_id at offset 4 (per reader.cpp).

## 40. libCacheSim large-scale evaluation (September-October)

Jane asked for results across every available trace rather than four hand-picked operating points. CacheLib cannot do that at this scale (one trace at a time, real time), so the evaluation moved to the libCacheSim simulator, using Aryan's fork with the two forgive policies (`LRUForgiveEmbCache`, `S3FIFOForgiveEmbCache`; github.com/Aryan470/libCacheSim, branch `embeddings`). Everything (scripts, results, how to rerun) is in the tardis repo (`libcachesim/`, `doc/reproduce.md`).

### 40.1 Setup

| | |
|---|---|
| Traces | 6,352: the whole CMU cache-trace mirror (CloudPhysics, MSR, FIU, SYSTOR, Alibaba Block, Tencent Block, Tencent Photo, Twitter in full and 10%/1% samples, Meta KV / CDN / Storage, Wikipedia) plus Wikimedia 2019 text and upload (42 days) |
| Cache sizes | 10%, 1%, 0.1% of each trace's working set in bytes (object sizes kept) |
| Policies | LRUForgive vs LRU, S3FIFOForgive vs S3-FIFO (each against its own base; LRUForgive vs S3-FIFO dropped per Jane) |
| Sketch table | 5x the number of objects the cache holds (same budget as MCACHE in CacheLib) |
| Skip rule | forgive policies not run when the cache holds fewer than 1,000 objects (S3-FIFO paper rule) |
| Metric | relative miss-ratio reduction per trace, (base - forgive) / base, averaged; this matches the paper's Table 5 |
| Machines | 8 CloudLab c220g2 nodes, one trace at a time per node (download, simulate, delete) |

### 40.2 Default parameters: almost nothing happens

With the defaults (lr 0.2, ctx-speed 0.001, threshold 0.4), pooled over all traces LRUForgive gains +0.18% at 10%, +0.74% at 1% and +1.20% at 0.1%, and most datasets sit at about 0%: forgiveness almost never fires. S3FIFOForgive is about 0% everywhere.

### 40.3 Hyperparameter tuning

- Optuna (TPE, Bayesian) over lr (0.01-0.5), ctx-speed (1e-7 to 1e-3) and threshold (0.3-0.95), defaults always tried first.
- One study per (dataset, cache size, policy): 84 studies, 100 trials each, up to 8 training traces (one per family of near-identical traces, at most 150M requests each). About 18 hours on 8 nodes.
- Tuned ctx-speed lands between 1e-7 and 1e-5 for nearly every LRUForgive study, 100 to 10,000 times slower than the default, and in the same range as the paper's Table 8.
- Convergence: by trial 50 the typical study had 98.5% of its final gain; trials 51-100 added a median 0.01 points (max 0.18). More trials would change little.
- Judged on held-out traces only (traces the tuner never saw), as in the paper.

### 40.4 Results with tuned parameters (held-out traces; the extended Table 5)

Relative miss-ratio reduction (%):

| Workload | LRUF 10% | LRUF 1% | LRUF 0.1% | S3FF 10% | S3FF 1% | S3FF 0.1% |
|---|---|---|---|---|---|---|
| Wiki Upload | +17.22 | +11.12 | +2.25 | +0.36 | +0.32 | +0.00 |
| Wiki Text | +8.88 | +6.35 | +2.44 | +0.24 | +0.45 | +0.08 |
| Meta KV † | +0.15 | +3.42 | +2.65 | +0.00 | +0.28 | -3.45 |
| Tencent Photo † | - | +8.04 | - | - | -0.83 | - |
| Tencent Block | +2.80 | +3.04 | +1.90 | -0.02 | -0.15 | +0.01 |
| Meta CDN † | +0.07 | +0.69 | +0.53 | -0.55 | -0.51 | -1.05 |
| Alibaba Block | +2.69 | +4.20 | +3.22 | -0.79 | +0.49 | +0.39 |
| Twitter | -2.71 | +1.21 | +0.56 | -5.61 | -3.46 | -0.12 |
| **Mean** | **+4.16** | **+4.76** | **+1.94** | **-0.91** | **-0.43** | **-0.59** |

† Not tunable (every trace over the request limit, or too few traces); run with Wiki Upload's tuned LRUForgive parameters for both policies (40.5). Paper Table 5 at 1% for comparison (LRUF / S3FF): Wiki Upload 12.00 / 0.65, Wiki Text 9.93 / 0.87, Meta KV 7.77 / 0.87, Tencent Photo 0.60 / 0.00, Tencent Block 3.53 / 1.01, Meta CDN 2.94 / 0.63, Alibaba Block 2.71 / 0.81, Twitter 2.70 / 0.00.

What this supports:
1. Tuned LRUForgive lowers the miss ratio on every paper workload at 1% (+0.7% to +11.1%, mean +4.8%), with the same workloads leading as in the paper (Wiki first, block storage next). Close to the paper on Wiki Upload, Tencent Block and Alibaba Block.
2. The gain is broad, not a few outliers: 100% of held-out Wiki traces improve at every size; 76-94% of block traces improve.
3. Tuning carries over to unseen traces: held-out gains match training gains on Wiki (+17.2 vs +17.6 at 10%); block datasets keep about half.
4. S3FIFOForgive stays near zero (means -0.4% to -0.9%), below every paper number. Consistent with Section 34.3: S3-FIFO already rescues what LRU throws away, so there is less left to forgive.

What it does not support, or needs care:
- Twitter: tuning made held-out traces worse (-2.7% LRUF, -5.6% S3FF at 10%). Tuned on 8 sampled clusters, most of which showed no change.
- At 10%, 21% of Tencent Block and 15% of Alibaba Block traces get worse with tuned LRUForgive even though the averages rise.
- The † rows rest on 2-4 traces and borrowed parameters.

### 40.5 Borrowed parameters

Meta KV and Tencent Photo (every trace over 1B requests) and Meta CDN (3 traces) could not be tuned; with defaults they show exactly 0%. Rerun with Wiki Upload's LRUForgive parameters (lr 0.117, ctx-speed 1.2e-7, threshold 0.483): Meta KV +3.4%, Tencent Photo +8.0%, Meta CDN +0.7% at 1%. So parameters partly transfer across workloads. Tencent Photo only finished at 1% (12-hour limit and memory; see 40.8).

### 40.6 Gain versus cache size depends on the workload

Wiki gains are largest at the largest cache (Wiki Upload +17.2% at 10%, +2.3% at 0.1%); block storage peaks at 1%. This looks like the opposite of the CacheLib finding (Section 16) that the gain collapses as the cache grows. 40.7 shows why the two are not yet comparable.

### 40.7 Cross-check against CacheLib on the same trace: the two implementations disagree

cache-t-00 is the same trace in both (Wikimedia 2019 text, 9,826,681 requests). Relative reduction of LRUForgive vs LRU:

| Simulator | Cache | Parameters | LRU miss | LRUForgive miss | Reduction |
|---|---|---|---|---|---|
| CacheLib | 600 MB | default | 0.8188 | 0.7624 | 6.89% |
| CacheLib | 4800 MB | default | 0.5805 | 0.5682 | 2.12% |
| CacheLib | 9600 MB | default | 0.4805 | 0.4784 | 0.44% |
| libCacheSim | 65 MB (0.1%) | default | 0.9242 | 0.9175 | 0.72% |
| libCacheSim | 652 MB (1%) | default | 0.7982 | 0.7980 | 0.03% |
| libCacheSim | 6.5 GB (10%) | default | 0.5249 | 0.5249 | 0.00% |
| libCacheSim | 652 MB (1%) | tuned | 0.7982 | 0.7470 | 6.41% |
| libCacheSim | 6.5 GB (10%) | tuned | 0.5249 | 0.4795 | 8.65% |

Two things stand out:
1. **Same defaults, very different results.** At about 600 MB, CacheLib's LRUForgive gains 6.9% with the defaults; the libCacheSim fork gains 0.03%. The LRU baselines are close (0.82 vs 0.80), and both code paths use the same update rule (context advanced every 10 accesses with 10x speed, the same thresholds and minimum access count). Something differs in how forgiveness fires. Until this is explained, "the defaults do nothing" is a statement about the simulator, not about TARDIS.
2. **The size trend flips with tuning.** CacheLib with defaults collapses as the cache grows; libCacheSim with tuned parameters (ctx-speed 2.4e-6 at 10%, 400x slower context) grows. A plausible reading: a slow-moving context relates objects over longer stretches of time, which is what a large cache needs (its useful reuse distances are long). That would make the Section 16 collapse a property of the default parameters, not of forgiveness.

**The gap is systematic, not just this trace.** The CacheLib all-traces sweep (`cachelib/result/all_traces_t1.csv`: every CMU-mirror trace at 1 thread with defaults, 10% and 0.1% of the footprint in bytes) can be matched trace by trace with the libCacheSim default sweep at the same sizes. On 5,481 traces at 10%, LRUForgive gains +1.01% in CacheLib and +0.22% in libCacheSim; at 0.1% (675 traces) +1.92% vs +1.47%. CacheLib's LRU baseline is slightly worse (median +0.008 miss ratio, slab overhead), which explains a little of it, not a 4.6x difference. So the simulator's forgiveness fires less than ours with the same parameters, most of all at large caches.

Cheap experiments that settle both: (a) run the libCacheSim fork on cache-t-00 at exactly 600 MB with defaults and compare its forgive counts (the `forgives=` line it prints) with CacheLib's nForgive at 600 MB; (b) run CacheLib on cache-t-00 at 6.5 GB with the libCacheSim-tuned Wiki Text 10% parameters (lr 0.157, ctx-speed 2.41e-6, threshold 0.487). If (b) gives a large gain, the collapse is a parameter effect.

### 40.8 Simulator issues found along the way (Aryan has graduated; these go to Jane)

- The per-object access counter (`access_count_`) is never freed, so memory grows with the number of unique objects, not the cache size. This is why the largest Twitter clusters run out of memory.
- `max-emb-entries=Nx` multiplies the cache size in bytes, not objects. We always pass an explicit object count.
- The sketch table costs about 512 bytes per entry. A 10% table for Tencent Photo (about 520M unique objects) is about 135 GB, near the node's 160 GB.

### 40.9 Paper-style rerun (ready; waiting on nodes, 14-day reservation)

The first run differs from the paper's method in three ways (cache sized in bytes with object sizes kept; tuning per dataset with at most 8 traces under 150M requests; 100 trials). The rerun matches the paper where it matters and keeps Jane's all-traces requirement:

- Object size ignored, so the cache is 10%, 1% and 0.1% of unique objects (paper: 1% of unique objects).
- One tuning study per workload type (as in the paper), training on a random third of the traces, at most 16, no request cap; 125 trials at every size.
- All traces, with Twitter represented by one sampled copy (`twitter_sample10`) used in full.
- Held-out results reported with and without near copies of training traces (the family check).
- Expected to fail at 10%: both Tencent Photo traces (sketch table too large) and Meta KV `202312` (8.7B requests). Those cells are recorded as failed and their workload is tuned without that size.
- Rough timing: tuning about 7.5x the first tuning run's work (Twitter and Wiki Upload are 60% of it), so about 5-6 days, run alongside the default sweep; then about 2 days for the tuned sweep.

## 41. Where things stand (October 18) and open questions

**Solid (can go in the talk as results):**
- CacheLib: the per-thread design holds the ~5-6pp LRUForgive gap from 1 to 16 threads on cache-t-00 and msr_proj_0, while the single-manager log design's gap collapses (38.1).
- CacheLib: forgiveness needs both eviction pressure and temporal correlation; the IRM zipf trace shows exactly zero gap (16b), and a high one-hit-wonder share rules a trace out (20.2).
- CacheLib: S3-FIFO's multithreaded eviction path is broken in the SOSP'23 artifact (non-atomic check-then-remove), so S3-FIFO numbers are single-threaded on real traces (23, 39).
- libCacheSim: tuned LRUForgive helps on every paper workload at 1% on held-out traces; S3FIFOForgive does not (40.4).

**Gaps to close (roughly in priority order):**
1. The two implementations disagree on cache-t-00 with the same defaults (40.7). Run the two cross-check experiments before the talk if possible, and before any paper number.
2. The cache-size trend: CacheLib collapse vs libCacheSim growth (40.6, 40.7 b).
3. The libCacheSim numbers do not yet follow the paper's sizing (object size ignored, % of unique objects); the rerun (40.9) fixes this.
4. Twitter tuning does not carry over; S3FIFOForgive is below the paper everywhere.
5. CacheLib S3FIFOForgive has rare large outliers on msr_proj_0 (38.2) and a branch discrepancy on msr_prxy_1 (38.3); msr_prxy_1's LRU is nondeterministic (TTL/Reaper hypothesis, 31.3). Exclude prxy_1 or root-cause it.
6. CacheLib t=2 rows are broken on every trace (38.4); the paper reports t=1/4/8/16.
7. The enqueue-order vs true-trace-order comparison (14b), the clearest evidence that order matters, was measured in shared mode before the harness fixes (31). Rerun both orderings on the fixed harness before it goes on a slide or in the paper.
8. Sections 32-33 (replicated mode: the ~1.1-point per-thread gap and "cluster53 inverts, so it is not recency pollution") were measured on per-thread code from before the shard-lookup fix (35-36), which made forgiveness read the wrong shard. Those conclusions need a rerun; the final sweep (38) is after the fix.
9. The recency-window result (24.1: a 64-entry buffer does not flatten the decay) ran in shared mode before the harness fixes (31), and its cluster53 runs all failed. Treat it as unverified.
10. The earlier run scripts recorded the last line of `mybench` output without checking the exit code. `mybench` prints a miss ratio at every checkpoint, so a run that crashed part way was recorded as an early (too high) miss ratio. Check whether the S3FIFOForgive outliers in 38.2 (0.4981, 0.5547) were crashes. The new `cachelib/scripts/run_matrix.sh` records them as `FAIL_exitN`.

**Decisions to confirm with Jane:**
- Which simulator's numbers the paper's Table 5 uses, and whether the CacheLib section uses the per-thread (replicated) mode as the headline.
- How to present the borrowed-parameter rows (†) and Twitter.

## Reference: paths, config, and build

- Source: `/mydata/tardis/sosp23-s3fifo/cachelib-sosp23/cachelib/allocator/`
- Implementation files: `MMLruForgive.h`, `MMLruForgive-inl.h`, `TARDISEmbeddingManager.h`
- Benchmark: `mybench/` (`benchMT.cpp`, `cmd.h`, `bench.h`, `cache.cpp`)
- Config params: learningRate=0.2, contextSpeed=0.001, recentWindow=16, minAccessCount=2, forgiveThreshold=0.4, maxForgives=5, max_K=3, maxEmbeddingEntries=5x cache items (MCACHE). K=8 subvectors of D=8. Note: **recentWindow is a RUNTIME constructor argument** (`int recent_window_`, set from the constructor, `recent_embeddings_.resize(recent_window_)`), not a compile-time constant like K and D. A future recentWindow 8/4 sweep therefore needs only a CLI knob wired to the constructor arg, not a per-value rebuild.
- Embedding storage: the `EmbeddingData` struct co-locates the K x D vectors with a cached `norms[K]` (L2 norms, `sqrt(sum of squares)`), computed inline in `init_embedding` and `update_embedding` (Section 25; note the shipped version caches the norm and uses `denom = norm_a*norm_b`, so it is empirically neutral but not bitwise-identical to the original cosine). Any new embedding mutation site (for example a revived Stage 2 merge, Section 14c) must recompute the cached norm inline or it goes stale silently.
- GitHub: `Djninja926/cachelib-sosp23`, branch `tardis-implementation` (remote is `origin` on the CloudLab node). Scripts and results: `github.com/Djninja926/tempdata-repo`.
- Build: `mybench/turboboost.sh disable`, then `mybench/build.sh` (full rebuild, required after any header change because of the explicit `CacheAllocator<LruForgiveCacheTrait>` template instantiations). **`build.sh` clears `_build/` on every full rebuild**, so a reference binary saved inside `_build` does not survive a header rebuild. Preserve reference binaries OUTSIDE `_build`, e.g. `cp _build/lruforgive ~/lruforgive.rw16`, before rebuilding a header. (This is the lesson from the Section 25.5 lost-A/B-binary case.)
- Node infra: CloudLab C220G2 (Wisconsin). `/mydata` is ephemeral scratch, wiped on reprovision and not captured by disk snapshots; rebuild from GitHub. On a fresh node: `sudo chown -R $USER:$(id -gn) /mydata`, then `sudo apt-get install -y numactl` (not installed by default; the harness pins with `numactl --cpunodebind=0 --membind=0`). Never run two sweeps concurrently (interleaved writes corrupt `sweep_results.csv`; dedup with `awk 'NR==1||!seen[$1","$2","$3]++'`).
- S3-FIFO reference files: `MMS3FIFO.h/-inl.h`, `datastruct/S3FIFOList.h/-inl.h` (concurrency defect diagnosed in Section 23).
- Sweep infrastructure: `/mydata/tardis/sysml_sweep/` (misnamed, it is LDOS work): `download_traces.sh`, `cache_size_sweep.sh` (resumable), `plot_sweep.py` (knee detection), `ohw_fast.py` (one-hit-wonder counting); results to `/mydata/tardis/sweep_results.csv`.
- libCacheSim: fork github.com/Aryan470/libCacheSim branch `embeddings` (`libCacheSim/cache/eviction/LRUForgiveEmbCache.cpp`, `S3FIFOForgiveEmbCache.cpp`). Scripts, results and instructions: tardis repo, `libcachesim/scripts/`, `libcachesim/result/`, `doc/reproduce.md`.
- Generated trace: `zipf1.0_1_100.oracleGeneral.bin` in `mybench/`, from `libCacheSim/scripts/data_gen.py -m 1000000 -n 20000000 --alpha 1.0`. Pure IRM negative control: gap exactly 0.00 at every cache size (Section 16b).
