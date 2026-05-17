# bptreedb — Production-Readiness Gap Analysis

**Date:** 2026-04-07
**Companion to:** `2026-04-07-bptreedb-design.md`

---

## How to read this document

`bptreedb` is an educational B+ tree key-value store. Its design (in the companion spec) was deliberately simplified at every step to keep the implementation comprehensible — small enough to hold in your head, simple enough to teach, narrow enough to finish. This document is the catalogue of those simplifications.

For each one it answers four questions:

1. **What `bptreedb` does.**
2. **What a production-grade engine does** (Postgres, SQLite, InnoDB, LMDB, etc., with concrete references).
3. **Why production engines do it that way** — i.e., what real-world constraint forces the additional complexity.
4. **What you would need to do** to close the gap.

Every term of art is defined inline at first use. The glossary at the end is a quick reference. The intended reader is a curious developer who has not opened a database textbook before.

A guiding observation throughout: each gap is a *reasoned trade-off*, not an oversight. Production engines are complicated because they have to satisfy constraints that `bptreedb` deliberately punts on — multi-user concurrency, oversized values, torn meta pages, hardware that lies about `fsync`, transactions, schemas, secondary indexes. Once you know the constraint, the complexity becomes legible.

---

## 1. Buffer-manager policies (STEAL, FORCE, and what they cost)

### What we do

`bptreedb` uses what databases call a **NO-STEAL / FORCE-AT-CHECKPOINT** buffer-manager policy.

Two terms to introduce:

- A **buffer manager** (or **buffer pool**) is the in-memory cache of pages that sits between the tree code and the data file. Every page read passes through it; every page write originates from it. In `bptreedb` the relevant module is `cache.py`.
- A **dirty page** is a page that has been modified in the buffer pool but not yet written to the data file.

The two policy axes are about when dirty pages are allowed to be written back:

- **STEAL** vs **NO-STEAL.** Can the buffer manager write a dirty page back to disk *before* the transaction that modified it commits? STEAL means "yes, freely;" NO-STEAL means "no, dirty pages are pinned in memory until commit."
- **FORCE** vs **NO-FORCE.** When a transaction commits, must all of its dirty pages be flushed to disk before commit returns? FORCE means "yes, every commit fsyncs every dirty page;" NO-FORCE means "no, commit just fsyncs the WAL and the dirty pages are flushed lazily."

`bptreedb` doesn't have transactions per se; the closest analogue is "between checkpoints." So our policy is precisely: **dirty pages are never written to the data file outside of a checkpoint.** Eviction is forbidden on dirty pages — the LRU cache only evicts clean pages. At checkpoint time, every dirty page is force-flushed; the data file is fsynced; the WAL is then truncated. This is NO-STEAL with a lazy form of FORCE.

### What production does

Postgres, MySQL InnoDB, Oracle, SQL Server, and basically every "real" relational engine use **STEAL + NO-FORCE**. This is the most flexible combination: dirty pages can be written back at any time (good for big working sets), and commits are fast because they don't have to wait for random I/O on the data file (good for throughput).

LMDB is closer to our policy: it's NO-STEAL because of its append-only-snapshot design. SQLite in default rollback-journal mode is even more conservative, being roughly FORCE.

### Why production does it that way

STEAL is about working-set size. A NO-STEAL buffer pool can hold no more dirty data than its capacity. If your write workload touches more pages than fit in the buffer pool between checkpoints, you have to checkpoint constantly — which is itself a source of write amplification. STEAL lets the buffer pool spill dirty pages to disk freely, keeping write rates smooth even when the working set vastly exceeds RAM.

NO-FORCE is about commit latency. Under FORCE, a transaction that touched a thousand pages does a thousand random writes at commit time. Under NO-FORCE, it does one append to the WAL and one `fsync`. For workloads with many small commits, this is the difference between thousands of transactions per second and dozens.

The combination has a price, though. If you allow dirty pages to be written before commit (STEAL), you must be able to **undo** those writes if the transaction aborts or the system crashes during it. That requires an **UNDO log** (sometimes called an "undo record" or, in Postgres, the equivalent role is played by the heap's MVCC machinery). And if you don't force pages on commit (NO-FORCE), you must be able to **redo** any committed change whose page didn't make it to disk before a crash. That requires a **REDO log** — which is what the WAL is.

So STEAL + NO-FORCE means "we need both REDO and UNDO." Postgres implements this with its WAL (which is the REDO log) plus its heap-tuple visibility / MVCC machinery (which serves the role UNDO would in other systems). InnoDB has both an explicit redo log and an explicit undo log.

### What it costs `bptreedb`

Two things, both bounded:

1. **Working set is bounded by buffer pool capacity.** Between checkpoints, the cache must hold every dirty page. If a workload would dirty more pages than `cache_capacity_pages`, we trigger an early checkpoint. In practice this is fine: each `put` or `delete` dirties at most `O(log N)` pages, and the checkpoint thresholds (4 MiB WAL, 50% dirty ratio) trigger long before exhaustion. Two implementation details enforce this:
   - **`_maybe_checkpoint` is called both before and after every public mutation.** The post-mutation call is the obvious one — "did we just push the WAL or the dirty ratio past its threshold?" The pre-mutation call is the subtle one: a single `put` whose cascading split touches several new pages can push the cache from "almost full of dirty pages" to "completely full of dirty pages," at which point eviction has nowhere to go and the operation aborts mid-rebalance — leaving the tree in a half-mutated in-memory state with no rollback. Pre-checkpointing flushes the pool down to "all clean" first, guaranteeing headroom for the upcoming rebalance.
   - **If the soft thresholds underestimate, eviction raises `DBBufferPoolOverflowError`.** The LRU evictor only considers clean pages; when every cached page is dirty and a new page would need a slot, we fail hard rather than silently violating NO-STEAL by writing back a dirty page. The error is the safety net for "we accidentally got into a state the threshold heuristics didn't catch."
2. **Checkpoint latency.** Each checkpoint flushes every dirty page in one shot. For a 256-page cache with half dirty, that's 128 page writes plus two fsyncs. This is fine for an educational workload but would be a latency spike under load.

### What it would take to bridge the gap

1. Implement an UNDO log: every WAL record that modifies a page must also describe how to reverse the modification.
2. Allow the buffer pool to evict dirty pages, with a new ordering rule: a dirty page may be written to the data file only if the WAL record at its `last_modified_lsn` has been fsynced (which is the WAL ordering rule we already prepared for in the spec — see §6.2).
3. Implement transaction abort: walk the UNDO log backward and reverse each operation.
4. Adapt recovery to do a three-pass ARIES procedure (see §8 below).

This is a significant undertaking — easily a doubling of the codebase — and is the most natural "Phase 2" if you ever want to take the project further.

---

## 2. WAL record format (logical vs physical vs physiological)

### What we do

`bptreedb` writes **logical** WAL records: each record describes a high-level operation like `PUT(key, value)` or `DELETE(key)`. Recovery replays the operations against the tree by re-executing them — `tree.insert(key, value)`, `tree.delete(key)`. The tree itself decides which pages to read, which to split, where to walk; the WAL doesn't know.

This is the most compact possible format (one record per public-API call regardless of how many pages were touched) and the simplest to write. It is also the *most* fragile, for reasons we'll get into.

### What production does

Almost no production engine uses pure logical logging.

- **Postgres** uses **physical** WAL records: each record describes a specific change to a specific page at a specific byte offset. Recovery applies these byte-level deltas directly to the data file pages. Postgres also writes **full-page images (FPI)** the first time a page is modified after a checkpoint, to defend against torn writes (see §3 below).
- **MySQL InnoDB** uses **physiological** records: each record describes a high-level operation ("insert this slot at this position on this page") that targets a specific page but uses logical semantics within the page. This is a hybrid that gets most of the benefits of physical (no torn-page worries, simple recovery) with smaller record sizes than full physical.
- **SQLite** in default rollback-journal mode actually uses page images for *both* the journal (which holds pre-images for rollback) and the data file (which holds post-images for forward writes). In WAL mode it uses full pages too. SQLite is comparatively profligate with bytes because it values simplicity.

The three styles, in one table:

| Style          | Record describes                                | Recovery needs to                  | Pros                                              | Cons                                          |
|----------------|-------------------------------------------------|------------------------------------|---------------------------------------------------|-----------------------------------------------|
| Logical (us)   | "User did `PUT key value`"                      | Re-execute the user-level op       | Tiny records; simple to write                     | Determinism required; can't tolerate concurrency or torn pages |
| Physical       | "On page 17, bytes 234..256 are now `<bytes>`"  | Memcpy bytes into the page         | No determinism required; survives concurrency     | Records can be page-sized; needs FPIs for torn pages |
| Physiological  | "On page 17, insert slot 3 with payload `<...>`"| Apply the slot operation to the page | Smaller than physical; no torn-page worries     | More record types to design and test           |

### Why production avoids pure logical

Two related reasons.

**Determinism.** Logical replay only works if the tree's behaviour on `put` / `delete` is *deterministic given its current state*. The tree must produce the same results — same splits, same page allocations, same internal-node separators — every time it processes the same input from the same starting state. Any source of nondeterminism breaks recovery:

- Wall-clock time leaking into a decision.
- Random numbers (e.g., a randomized data structure).
- Iteration order over a Python `dict` or `set` (CPython's order is now insertion-order, which helps, but it would have been a footgun pre-3.7).
- Hash randomization.
- *Concurrency.* Any concurrent writer means the order of operations seen by the tree depends on thread scheduling. There is no logical replay that can reproduce this.

Single-threaded `bptreedb` is small enough to enforce determinism by discipline. Production engines are multi-threaded, so they cannot.

**Torn pages.** A logical replay rebuilds the tree from a starting state on the data file. If that starting state is *itself* corrupt — e.g., a page was half-written to disk during the crash, leaving an inconsistent slot directory — the replay can't proceed because the tree it's walking is malformed before it even starts. Physical and physiological logging handle this by recording (or being able to reconstruct) the *exact bytes* of every page that's been modified, so torn pages get overwritten with known-good content during redo.

Postgres's solution: write a **full-page image (FPI)** to the WAL the *first* time each page is modified after a checkpoint. When recovery sees a FPI, it overwrites the entire page with the FPI's contents, regardless of whatever torn state exists on disk. Subsequent modifications to the same page can use small delta records. This bloats the WAL right after each checkpoint and then tapers off — and the bloat is the price Postgres pays for being able to handle torn pages without giving up any of the speed benefits of NO-FORCE.

### Why `bptreedb` can get away with pure logical

We have two structural protections that production engines don't:

1. **Single-threaded.** No interleavings, no nondeterminism from scheduling.
2. **NO-STEAL.** Dirty pages are never written outside of a checkpoint, so the data file at recovery time is always either the state at the previous checkpoint or the state at the current one — never a torn intermediate. Spec §6.5 spells out the crash-window analysis that proves this.

These two together let us replay logical records and trust the tree to redo the same work it did the first time.

### What it costs `bptreedb`

- **Concurrency is impossible without redesigning the WAL.** If you ever want multiple writers, you have to switch the WAL to physiological or physical.
- **Determinism is fragile by construction.** A future contributor who innocently introduces a `time.time()` call into a tree-decision path will silently break crash recovery. The crash property test from spec §8.5 catches this in practice, but the only enforcement mechanism is "the test suite is paranoid."

### What it would take to bridge the gap

The natural next step is **physiological records**, which give you the most bang for the buck:

1. Change the WAL record types from `PUT` / `DELETE` to per-page operations: `LEAF_INSERT(page_id, slot_index, payload)`, `LEAF_DELETE(page_id, slot_index)`, `LEAF_SPLIT(page_id, new_page_id, ...)`, `INTERNAL_INSERT(page_id, ...)`, etc.
2. The tree now writes one WAL record *per page modification* (so a single `put` that splits up to the root produces O(log N) records) instead of one per public-API call.
3. Recovery applies records page-by-page instead of re-walking the tree.

This is a meaningful rewrite of `tree.py` and `wal.py` — easily a third of the codebase.

To go further to **physical with full-page images**, you'd add a "first modification after checkpoint" flag to each page, and write a FPI to the WAL the first time you mark such a page dirty. The flag resets at every checkpoint.

---

## 3. Meta page durability (and the ping-pong dance)

### What we do

`bptreedb` has a single meta page at offset 0 of the data file, with a CRC over its contents. On open, the meta page is read and its CRC verified. If the CRC fails, we raise `DBCorruptedError` and the database is unrecoverable from the data file alone.

Two nuances that the upfront spec didn't quite anticipate:

1. **The meta page is a cache, not the source of truth, for the `last_checkpoint_lsn`.** Every `CHECKPOINT` WAL record carries a snapshot of `(root_page_id, freelist_head, next_page_id)` alongside its LSN, and recovery does a pre-pass scanning the WAL for `CHECKPOINT` records before any replay starts. If the highest-LSN `CHECKPOINT` exceeds the on-disk meta's `last_checkpoint_lsn`, the in-memory meta is rolled forward from the `CHECKPOINT` record. The next checkpoint then rewrites the meta to match.
2. **This buys us a partial fallback for *stale* meta pages, but not for torn ones.** If a crash lands between "append `CHECKPOINT` to the WAL" and "rewrite the on-disk meta," the next open repairs the meta from the WAL. But if the crash tears the meta itself — the actual byte-level concern that drives ping-pong in LMDB — the CRC check fails and we abort. The roll-forward only saves us when the on-disk meta is intact-but-stale.

### What production does

- **LMDB** uses **two meta pages** at the start of the file (page 0 and page 1) and writes them alternately at each commit. On open, LMDB picks the meta page with the higher transaction id whose checksum validates. This is called **ping-pong** meta pages or sometimes **double-buffering** the meta.
- **Postgres** has a separate `pg_control` file outside of any data file, with its own checksum. On top of that, each table is a separate file with its own per-page metadata, and the cluster's recovery state lives in `pg_control` plus the WAL.
- **SQLite** in WAL mode keeps the database header (which contains the equivalent of our meta page) at the start of the main database file, and uses the journal/WAL plus its checksum to detect corruption.

### Why ping-pong

A torn write to the meta page is catastrophic — it's the entry point to the entire on-disk structure. If you lose the meta page, you lose every page's reachability, the root of the tree, the freelist, everything. Even on modern hardware (where page-aligned writes are usually atomic at the sector level), torn writes can still happen on:

- Older spinning disks where a page write spans multiple sectors.
- Cheap SSDs where firmware bugs leak partial writes on power loss.
- Network filesystems with unsynchronized clients.

Ping-pong gives you a fallback: if writing meta-page-A is interrupted, meta-page-B from the previous checkpoint is still intact and the database falls back to that. You lose the most recent checkpoint's metadata-update, but you can rebuild it from the WAL.

### What it costs `bptreedb`

A single torn write to page 0 destroys the database. The `CHECKPOINT`-record fallback above covers the *stale* case (intact CRC, missed the most recent rewrite) but not the *torn* case (write interrupted mid-page, CRC fails). The probability of a torn page-0 write is low on modern hardware but the cost is total loss.

### What it would take to bridge the gap

Reserve pages 0 and 1 as ping-pong meta pages.

1. Each checkpoint alternates which meta page it writes (`current_meta_index = checkpoint_number % 2`).
2. The chosen meta page is fully encoded, CRC'd, written, and fsynced.
3. On open, read both meta pages. Pick the valid one (CRC passes) with the higher `last_checkpoint_lsn`.
4. If neither validates, raise `DBCorruptedError`.
5. If only one validates, use it and immediately schedule the other to be rewritten on the next checkpoint.

This is maybe 30 lines of additional code in `pager.py`. The trickiest part is making sure the "which meta is current?" state is itself recovered correctly — you can't store it in the meta page without circularity. The standard solution is "the one with the higher LSN wins," with ties broken by index.

---

## 4. Concurrency (latches, locks, and MVCC)

### What we do

Nothing. `bptreedb` is single-threaded. Every public method is a blocking call. There are no locks, no latches, no atomic counters, no concurrency-safe data structures.

### What production does

Production database engines support hundreds or thousands of concurrent clients, often with mixed read and write workloads. They achieve this through some combination of three mechanisms, each with its own vocabulary worth defining.

A **latch** is a short-duration mutex protecting a single page or buffer-pool slot. It is held for microseconds — only as long as a page is being read or modified in memory. Latches are *not* user-visible; they are an internal implementation detail of the buffer pool and the index code. They come in **shared** (multiple readers OK) and **exclusive** (one writer, no readers) flavours, and they typically nest carefully to avoid deadlocks. ("Latch coupling" is a tree-traversal technique that holds a latch on a child before releasing the latch on its parent, so concurrent splits don't lose the walker.)

A **lock** is a transaction-scoped abstraction protecting a logical entity (a row, a range of rows, a table). Locks are held for the duration of a transaction — possibly seconds. They are managed by a **lock manager**, a complex subsystem that detects deadlocks (typically via wait-for graphs) and handles lock escalation. Locks come in many modes: shared, exclusive, intent-shared, intent-exclusive, update, etc.

**MVCC** stands for **Multi-Version Concurrency Control**. Each row in the database has multiple versions, each tagged with the transaction id that created it. A transaction reads a "snapshot" of the database — the set of row versions visible at the moment the transaction began. Readers and writers don't block each other: readers see old versions, writers create new versions. MVCC eliminates the need for read locks at the cost of needing garbage collection of old versions ("vacuum" in Postgres, "purge" in InnoDB).

Production engines combine these:

- **Postgres** uses **page latches** for short access (held for microseconds while reading or modifying a page in the buffer pool), plus a **lock manager** for transaction-level locks (table locks, row locks for `SELECT ... FOR UPDATE`), plus **MVCC** via heap tuple visibility (regular reads use snapshots, no read locks).
- **MySQL InnoDB** uses **row locks** + **MVCC** + a separate **redo log** (REDO) and **undo tablespace** (which stores both UNDO records for rollback and old row versions for MVCC reads).
- **SQLite** in default rollback-journal mode uses **a single global file lock** (writers exclude readers and writers exclude writers). In WAL mode it has **snapshot isolation**: readers see a consistent snapshot from when their transaction began, writers append to the WAL, and a single writer at a time is allowed.

### Why concurrency is hard

Each mechanism solves a problem the previous one doesn't:

- **Latches alone** would force every reader and writer to block on the same page, even briefly. This is OK for the buffer pool (where pages are touched for nanoseconds) but not for transactions that might hold a "lock" on a row for seconds.
- **Locks alone** would require every reader to acquire a lock on every row it touches, which is a contention nightmare and often unnecessary (most reads don't conflict with most writes).
- **MVCC** lets reads happen without locking at all, by giving each reader its own snapshot. This is the dominant approach in modern systems.

### What it costs `bptreedb`

We can serve exactly one client at a time from one process. Multi-threaded code wraps the DB in a single Python `threading.Lock`, which serializes everything and gives you no benefit. Multi-process clients can't share a `bptreedb` at all; you need an external process to broker access.

Even with the external mutex, multi-process access is actively unsafe — `bptreedb` takes no `flock`/POSIX advisory lock on either the data file or the WAL, so two processes opening the same directory will silently corrupt each other's state. SQLite defends against this by acquiring file locks on open; we don't. Treat the data directory as owned by one Python process at a time.

For an educational, single-user, embedded use case this is fine. For anything else it's a deal-breaker.

### What it would take to bridge the gap

The road has many forks; here's the standard one:

1. **Page latches.** Add shared/exclusive latches to each buffer-pool page. Tree walks acquire shared latches on the way down, upgrade to exclusive only at the leaf about to be modified. Splits use latch coupling. (Very invasive — every page access in the codebase has to be examined.)
2. **A reader-writer lock at the DB level.** Coarse but cheap; gives you "many readers OR one writer." Insufficient for serious concurrency but simple to implement and a useful intermediate step.
3. **Snapshot reads.** Each reader records the meta page's LSN when it begins; reads see only pages reachable from that LSN. Requires either MVCC (versioned records) or copy-on-write tree updates (LMDB's approach: an entire root-to-leaf path is rewritten on every write, and old paths are GC'd).
4. **Full MVCC.** Each leaf record carries `(creating_txn_id, deleting_txn_id, prev_version_pointer)`. Visibility checks at read time. A vacuum process to remove versions no transaction can still see.

LMDB took a fascinating shortcut around all this: it uses **copy-on-write** tree updates, which makes every committed snapshot a complete, immutable tree rooted at one of the two ping-pong meta pages. Readers just pin a meta page; writers write a fresh tree without touching readers' pages. There is exactly one writer at a time (a single global mutex), but readers are completely lock-free. This is much simpler than full MVCC and gives you most of the benefits for read-heavy workloads.

---

## 5. Free space management (freelists, FSMs, and bitmaps)

### What we do

`bptreedb` uses a **singly-linked freelist** of freed page ids. When the tree merges two leaves and frees a page, the page id is pushed onto the head freelist page. When the tree needs a new page, it pops from the head freelist page (or bump-allocates if the freelist is empty).

This is the simplest possible scheme. It is **LIFO** (last-in-first-out): the most recently freed page is the next to be reused.

### What production does

- **Postgres** uses a **Free Space Map** (FSM) per relation. The FSM is a small B-tree indicating, for each page in the relation, approximately how much free space it has. When inserting a tuple, the executor queries the FSM for "give me a page with at least N bytes free" and gets back a candidate page id. The FSM is updated probabilistically (not on every insert) to keep its overhead low.
- **MySQL InnoDB** uses **bitmap pages**: special pages within each tablespace whose bytes describe the allocation status of nearby data pages. A few bits per page indicate "allocated", "fragment", "free", etc. Allocation walks the bitmap.
- **SQLite** uses **trunk pages** in a linked list, similar to ours but with one important difference: the freelist is reused only when a transaction explicitly enables it (otherwise pages are leaked, intentionally, to reduce write amplification on cheap storage).

### Why FSMs and bitmaps exist

A LIFO freelist gives you *any* free page. For variable-length record insertion, you usually want a page with **enough** free space for the record you're inserting — and for a workload of mixed-size records, the freelist's "any free page" answer often forces you to allocate a new page even though half-empty pages exist.

Concretely:

- You insert many small records into a leaf, then delete most of them. The leaf is now half-empty but not below the merge threshold. It is *not* on the freelist.
- You then insert a large record that doesn't fit on any existing leaf. With a freelist, you bump-allocate a new page, leaving the half-empty leaf wasting space.
- With an FSM, the inserter would find the half-empty leaf and use it.

For workloads that have stable patterns of insertion and deletion, FSMs significantly reduce the on-disk footprint. For our educational use case where the typical workload is "insert many, delete few," the freelist is fine.

### What it costs `bptreedb`

Half-empty pages between the merge threshold (40%) and full are never reclaimed. A long-running database with churning small writes can develop significant internal fragmentation. The on-disk file size will be larger than the theoretical minimum for the working set.

### What it would take to bridge the gap

Replace the linked-list freelist with a small B-tree (or sorted array, or bitmap) keyed by free-space buckets. For each page, record its current free-space bucket (e.g., 0-25%, 25-50%, 50-75%, 75-100%). On insertion, query the FSM for "give me a page with at least N bytes free" and update the bucket if the new free-space estimate falls in a different bucket.

Postgres deliberately makes the FSM probabilistic and lazy — the executor doesn't update the FSM on every insert, just occasionally — to keep the overhead low. This is a useful design pattern: an *advisory* index rather than a strictly correct one.

---

## 6. Variable-length records and oversized values (overflow pages)

### What we do

`bptreedb` uses slotted pages with variable-length records — same as most production engines — and **rejects records larger than `(page_size - header_size) // 5 - slot_size`** with a `DBRecordTooLargeError`. For a 4 KiB page, this is about 800 bytes; for a 256-byte page, it's about 36 bytes.

The `/5`, rather than the more intuitive `/4` (which would also guarantee "four records fit in an empty page"), is load-bearing. It keeps the maximum slot below roughly `0.2 * page_size + slot_overhead`, which is what makes the half-full invariant always achievable: with that tighter cap, an underpopulated page next to a sibling can always be redistributed-or-merged without producing a fresh under-threshold page. At `/4`, extreme slot-size distributions can force an unbalanced split that leaves one half below the 40% merge threshold — and the cascading rebalance has no good answer. This came out of implementing redistribute-or-merge: the spec called for `/5` from the start, but only when the corner cases of the rebalance code stopped breaking did the precise reason for the choice land.

### What production does

Every production engine handles oversized records via **overflow pages** (sometimes called **TOAST** in Postgres, **off-page storage** in InnoDB, or **continuation chains** in older systems).

- **Postgres TOAST** (The Oversized-Attribute Storage Technique): when a row's total size exceeds about 2 KiB, large attributes are *compressed* and then *moved out of line* into a separate "TOAST table" associated with the main table. The main heap row stores a pointer (a "TOAST pointer") to the TOAST chunks. Reading a large attribute requires following the pointer and reassembling chunks.
- **MySQL InnoDB** stores oversized attributes on **off-page storage**: linked overflow pages. The clustered index stores either the entire attribute (if it's small enough) or a 20-byte pointer to the overflow chain.
- **SQLite** uses **payload overflow pages** in a linked list: the cell on the leaf page stores as much of the payload as fits, then a pointer to the first overflow page; subsequent pages chain via a header field.

### Why overflow pages

For general-purpose use, "your value is too large" is unacceptable. Documents, blobs, JSON, images, and serialized objects routinely exceed any reasonable page size. A database that can't store them is not a general-purpose database.

### What it costs `bptreedb`

A user wanting to store a 10 KiB value in a 4 KiB-page DB is told "no." For an educational KV store this is acceptable; the user can chunk their data themselves if they really want to.

There is also a more subtle cost: by rejecting large records, we avoid the entire complexity of tree algorithms that have to handle records spanning multiple pages. Splits, merges, and scans all stay simple. Overflow pages would force every leaf-touching operation to also be aware of overflow chains.

### What it would take to bridge the gap

Add a third page type, `TYPE_OVERFLOW = 0x04`, parallel to internal and leaf nodes. An overflow page has a header (with type tag and a `next_overflow_page_id` field) and a body of raw payload bytes. A chain of overflow pages stores a single oversized value.

The leaf record format gains a flag bit (or a sentinel `value_length`) indicating "this value is stored off-page." When the flag is set, the leaf stores a small "overflow descriptor": `(first_overflow_page_id, total_value_length)` instead of the value itself.

Operations to update:

- `put` with an oversized value: split the value into chunks, allocate overflow pages, write the chunks, store the descriptor in the leaf.
- `get` of an oversized record: follow the overflow chain and reassemble the value.
- `delete` of an oversized record: walk the overflow chain and free every page back to the freelist.
- `scan`: same as `get`, just over a range.
- Splits and merges: when copying a record from one leaf to another, also copy (or share) the overflow chain.

This is a meaningful addition — easily 100-200 lines of new code spread across `page.py`, `tree.py`, and the pager — but it is *additive*, not invasive. Existing tests don't change; new tests cover the overflow paths.

---

## 7. Recovery (and the ARIES algorithm)

### What we do

`bptreedb` recovers via **single-pass logical replay** from the last checkpoint. The procedure is:

1. Read the meta page. Then do a pre-pass over the WAL looking for `CHECKPOINT` records; if any has an LSN higher than the meta's `last_checkpoint_lsn`, roll the in-memory meta forward from that record (see §3 for why). The effective `last_checkpoint_lsn` for the rest of recovery is `max(disk_meta, highest_WAL_CHECKPOINT)`.
2. Walk the WAL, validating each record.
3. Skip records with LSN ≤ effective `last_checkpoint_lsn`.
4. For each later record, re-execute the operation against the tree.
5. Take an immediate checkpoint to persist the recovered state.

Eviction is disabled during recovery so the buffer pool can grow unbounded; it's bounded in practice by the WAL size since the last checkpoint, which is itself bounded by `checkpoint_wal_size_bytes`. Without this guard, mid-recovery eviction could flush half-applied tree state to disk before the next WAL record completes — and a second crash at that moment would leave the data file inconsistent.

### What production does

Most production engines use **ARIES** or a variant. ARIES (Algorithm for Recovery and Isolation Exploiting Semantics) was published in 1992 by C. Mohan and others at IBM, and is the dominant recovery algorithm in commercial databases.

ARIES recovery is a **three-pass** procedure:

1. **Analysis pass.** Scan the WAL forward from the last checkpoint, building two pieces of state: a **Transaction Table** (which transactions were active at the time of the crash, and what was the LSN of their most recent change) and a **Dirty Page Table** (which pages were dirty at the time of the crash, and what was the LSN of the earliest change that dirtied them — the "recovery LSN" or "rec_lsn"). The lowest rec_lsn in the dirty page table is where the redo pass will start.
2. **Redo pass.** Scan the WAL forward from the lowest rec_lsn. For each record, re-apply the change to the appropriate page — *unconditionally*, regardless of whether the transaction committed or aborted. The reason: under STEAL, dirty pages from uncommitted transactions may have been written to disk before the crash, and those changes have to be re-applied to bring the page to a consistent state before they can be undone in the next pass. ARIES uses physical or physiological logging precisely because this kind of "redo even uncommitted things" requires byte-level certainty about what each change did.
3. **Undo pass.** For each transaction in the Transaction Table that was *not* committed at the time of the crash, walk its WAL chain backward and undo each change using the UNDO records in the WAL. As undo proceeds, ARIES writes **compensation log records (CLRs)** that describe each undo step — so that if the undo itself is interrupted by a crash, recovery can resume from the right point.

The full ARIES procedure includes additional refinements: **fuzzy checkpoints** (a checkpoint records the dirty page table without forcing the dirty pages to disk, so checkpointing doesn't block writers), **savepoints** (partial transaction rollback), and various optimizations to skip redo work that's already on disk.

### Why ARIES exists

Three reasons that all stem from the STEAL + NO-FORCE buffer pool policy:

1. **Dirty pages from uncommitted transactions can be on disk.** Recovery must redo their changes before deciding to undo them — otherwise the undo records refer to states that aren't there yet.
2. **Committed transactions' dirty pages may *not* be on disk.** Recovery must redo those changes to honor durability.
3. **Undo itself is restartable.** A crash during undo must not leave the database in a half-undone state.

A NO-STEAL system doesn't have problem 1 (because uncommitted dirty pages never reach disk). A FORCE system doesn't have problem 2 (because committed dirty pages are always on disk). `bptreedb` is NO-STEAL with effective FORCE-at-checkpoint, so neither problem applies — and our recovery is correspondingly trivial: replay the WAL since the last checkpoint and you're done.

### What it costs `bptreedb`

Recovery time is bounded by `checkpoint_wal_size_bytes`. With the default 4 MiB and tiny PUT records, that's at most a few hundred thousand operations. On educational hardware this replays in a fraction of a second. There is no penalty for being simple.

The constraint we accept is the *flip side*: we get easy recovery in exchange for forgoing STEAL and forgoing transactions. ARIES exists because production engines refuse to make those compromises.

### What it would take to bridge the gap

Implementing ARIES is a significant project — easily larger than the rest of `bptreedb`. The minimum steps:

1. Add transactions (see §8 below).
2. Add an UNDO log: every WAL record gains an "undo image" describing how to reverse it.
3. Switch the buffer pool to STEAL.
4. Implement the analysis pass: build the Transaction Table and Dirty Page Table from the WAL.
5. Implement the redo pass with byte-level page application (which requires switching from logical to physiological WAL records — see §2 above).
6. Implement the undo pass with compensation log records.
7. Implement fuzzy checkpoints.

ARIES is what real databases do, and there is a reason it has not been substantially superseded in 30+ years. It is also complex enough that most "from scratch" educational databases (including `bptreedb`) deliberately skip it.

---

## 8. Transactions and isolation levels

### What we do

`bptreedb` has **no transactions**. Each public-API call is its own atomic, durable unit. There is no `begin`, no `commit`, no `abort`, no notion of grouping multiple operations into a single logical action.

### What production does

Every production relational database supports transactions. The standard properties of a transaction are summarized by the acronym **ACID**:

- **Atomicity.** A transaction is all-or-nothing: either every operation in it takes effect, or none of them do. If the transaction aborts (or the system crashes), partial effects are rolled back.
- **Consistency.** A transaction transforms the database from one consistent state to another. Any invariants the application enforces must hold at commit time. (This is the part of ACID that is mostly the application's responsibility, not the database's.)
- **Isolation.** Concurrent transactions don't see each other's intermediate state. The level of isolation determines how strictly this is enforced.
- **Durability.** Once a transaction commits, its effects survive any subsequent crash.

The standard SQL isolation levels, in order of increasing strictness:

- **Read uncommitted.** A transaction can see changes made by other transactions before they commit. Almost no real database actually offers this; it's more of a theoretical floor. Even when nominally selected (e.g., in some MySQL configurations), implementations usually round it up to read committed.
- **Read committed.** A transaction only sees changes from other transactions that have committed. Each individual `SELECT` sees a fresh snapshot. Postgres's default. Allows non-repeatable reads (running the same query twice in one transaction may return different results).
- **Repeatable read.** A transaction sees a consistent snapshot for its entire duration. Multiple `SELECT`s of the same row return the same result. Allows phantom reads (a query returning a range may return more rows the second time if another transaction inserted matching rows). MySQL InnoDB's default.
- **Snapshot isolation.** A transaction sees the database as it was when the transaction began. Closely related to repeatable read but achieved via MVCC rather than locking. Postgres's "repeatable read" mode is actually snapshot isolation.
- **Serializable.** Concurrent transactions appear to execute in some serial order. The strictest level, and the only one that completely eliminates concurrency anomalies. Implemented either via two-phase locking (slow) or via SSI (Serializable Snapshot Isolation), a clever extension to snapshot isolation that aborts transactions whose execution would have produced a non-serializable result.

### Why transactions exist

Two reasons.

**Application correctness.** Many real-world operations involve multiple writes that must succeed or fail together. Transferring money between accounts, for example, requires deducting from one account and crediting another. If the system crashes between the two, you have lost (or duplicated) money. A transaction guarantees both happen or neither.

**Concurrency.** When multiple users hit the database simultaneously, transactions provide the abstraction that lets each user reason about the database as if they were the only one. Without transactions, every application would have to write its own concurrency control on top of every read and write — a recipe for subtle, irreproducible bugs.

### What it costs `bptreedb`

Users cannot group multiple operations atomically. If an application needs to insert several related records, it has to insert them one at a time, accepting that a crash in between leaves a partially-applied state. In an educational single-user setting, this is acceptable. For real applications it's a non-starter.

### What it would take to bridge the gap

A meaningful undertaking, but the path is well-known:

1. Add `db.begin()` returning a `Transaction` object. The transaction holds a write set (a list of pending PUT and DELETE records).
2. Inside a transaction, `put` and `delete` append to the write set and modify a local view (or just the write set itself, with reads checking the write set first).
3. `commit` writes a single composite WAL record (with `BEGIN_TXN(txn_id)`, the operations, and `COMMIT_TXN(txn_id)` markers — or one giant atomic record), fsyncs, and applies the changes to the tree.
4. `abort` discards the write set without applying anything to the tree.
5. Recovery treats `BEGIN_TXN ... COMMIT_TXN` brackets atomically: if a `BEGIN_TXN` is found without a matching `COMMIT_TXN`, the transaction's operations are discarded.

That gives you atomicity and durability for single-writer transactions, no isolation (because we're still single-threaded), and no concurrent transactions. To add isolation you need concurrency, and to add concurrency you need either page latching or MVCC (see §4). Each step compounds.

---

## 9. Schemas, tables, and secondary indexes

### What we do

`bptreedb` is a flat key-value store. There is one index — the primary key — and the value is opaque bytes. There are no tables, no schemas, no secondary indexes, no joins, no query language.

### What production does

Almost every production database supports multiple indexes per table, schemas with typed columns, joins, and a query language (usually SQL).

Two terms to define for clarity:

- A **clustered index** is an index whose leaf pages contain the actual rows of the table. The table is *stored* in primary-key order. MySQL InnoDB always uses a clustered index on the primary key. SQLite uses one for `WITHOUT ROWID` tables; its default is a hidden integer ROWID.
- A **secondary index** is an index on a non-primary-key column. Its leaf pages contain *not* the rows themselves but pointers to the rows.

The pointer in a secondary index can be one of two kinds:

- A **physical pointer**: a (page id, slot index) tuple identifying where the row lives on disk. Postgres calls this a **TID** (tuple identifier). Cheap to follow, but needs special handling when a row moves (e.g., when it's updated to a larger size and migrates to another page) — Postgres handles this via "HOT updates" and "TID chains."
- A **logical pointer**: the primary-key value of the row. To go from a secondary index to the actual row, you do a *second* lookup in the primary index. Slower (one extra B-tree walk) but stable across row movements. This is what InnoDB does.

A **schema** is the metadata describing what tables exist, what columns each has, what types those columns are, what constraints apply, and what indexes exist. Schemas live in **system catalogs** — special tables managed by the database itself, queryable via the same SQL machinery as user tables.

### Why these layers exist

A flat KV store is not enough for most applications. Real applications query data in many ways: "all users created last week," "all orders for this customer," "the top-grossing product in each category." Each of these queries wants a different access path, and a single primary-key index can only answer one of them efficiently. Secondary indexes give you multiple access paths over the same data without duplicating the data. Schemas let the database understand and validate the shape of the data, and let the query planner make intelligent decisions about how to execute queries.

### What it costs `bptreedb`

Every application built on `bptreedb` has to do its own indexing in user code. Want a "users by email" lookup in addition to "users by id"? You have to maintain two separate `bptreedb` instances (or use composite keys) and keep them consistent yourself. Want to query by a non-key field at all? You can't — you have to scan the entire database.

This is, again, fine for an educational KV store. It is the line beyond which you stop being a KV store and start being a database engine.

### What it would take to bridge the gap

Each layer is its own substantial project:

1. **A schema layer.** Define a `Schema` class with named, typed columns. Serialize schema metadata to a system catalog (a special record under a reserved key, e.g., `__schema__`).
2. **A tuple encoding.** Define how a row of typed values becomes a `bytes` value.
3. **A table abstraction.** A `Table` is a logical view over the underlying KV store, with its primary index using composite keys like `(table_name, primary_key)`.
4. **Secondary indexes.** Each index is its own B-tree. Updates to the table must update every index atomically — which is now a multi-write operation, which requires transactions (§8 above).
5. **A query planner and executor.** Even a minimal one (e.g., "given a set of indexes, pick the best one for this WHERE clause") is a meaningful component.
6. **A SQL parser** if you want a SQL interface.

Each step here is an entire project. Realistically, a `bptreedb`-based "real" database would borrow heavily from existing query planners and parsers — say, by hosting a frontend like Calcite or DataFusion on top.

---

## 10. Could `bptreedb` be the storage backend for a SQL database?

### Short answer

Yes — almost. The KV API (`put` / `get` / `delete` / `scan`) is the right shape for a relational storage backend, and most of the SQL machinery can live in user code on top of it without changing the backend. The single critical missing piece is **transactions** (§8 above): without them, you cannot atomically update a row and its secondary indexes, which is a non-negotiable requirement for SQL.

### How SQL maps onto an ordered KV store

This pattern is well-established in industry. **CockroachDB** uses Pebble (a RocksDB descendant) as its KV backend; **TiDB** uses TiKV; **FoundationDB** ships a "Record Layer" that puts a relational interface on top of an ordered KV store. The mapping is similar across all of them.

A relational table with primary key columns `(p1, p2, ...)` and value columns `(v1, v2, ...)` is stored as:

```
key   = encode(table_id) || encode(p1) || encode(p2) || ...
value = encode(v1, v2, ...)
```

The key encoding has to be **order-preserving** — if `(p1_a, p2_a)` should sort before `(p1_b, p2_b)` in primary-key order, the encoded byte string for the first must be lexicographically less than the encoded byte string for the second. Several order-preserving encodings exist for integers, floats, strings, and composite tuples; FoundationDB and CockroachDB both publish their schemes. The `bytes`-with-lexicographic-ordering API of `bptreedb` is exactly the substrate they require.

With this encoding, SQL operations map onto the KV API directly:

- `SELECT * FROM table WHERE p1 = X` becomes `scan(start_key_inclusive=encode(table_id, X), end_key_exclusive=encode(table_id, X+1))`.
- A primary-key point lookup becomes a `get`.
- An `INSERT` or `UPDATE` becomes a `put`.
- A `DELETE` becomes a `delete`.

Crucially, *all of these operations are bounded by key prefixes*, which is exactly what the leaf sibling chain in a B+ tree is good at. The `bptreedb` scan primitive is the load-bearing API for SQL workloads — `get` and `put` would be useful but not sufficient on their own.

A **secondary index** on column `c` of the same table is stored as a separate key range:

```
key   = encode(index_id) || encode(c) || encode(p1) || encode(p2) || ...
value = (empty, or the encoded primary key for InnoDB-style secondary indexes)
```

To find all rows where `c = Y`, you `scan` the index range, get back the primary keys, and then do point `get`s into the table. This is two B-tree walks per row (one in the index, one in the table) — exactly the cost InnoDB pays for its secondary index design.

A **schema** lives in a reserved key range that user code treats as the system catalog. For example, all keys with prefix `\x00` could be metadata records describing tables, columns, indexes, and constraints. The KV backend doesn't need to know they're special; user code reads and writes them like any other records.

### What lives in user space, no backend changes needed

Almost the entire SQL layer:

- **The SQL parser.** Takes a query string, returns an abstract syntax tree.
- **The query planner.** Picks an execution strategy (which index to use, what order to join tables, whether to use a hash join or a merge join).
- **The executor.** Walks the plan, calling `get` / `scan` / `put` / `delete` on the KV backend.
- **The schema / catalog layer.** Stores table definitions in a reserved key range.
- **Constraint checking.** Foreign keys, `NOT NULL`, `UNIQUE` — all enforceable in user code that wraps the KV backend.
- **The type system.** The KV store deals only in `bytes`; the type system above it interprets those bytes as ints, strings, dates, etc.

None of this requires changes to `bptreedb`'s public API. A SQL frontend could be built as a separate package that imports `bptreedb` and uses it like any other client.

### What `bptreedb` is missing for the role

**Transactions are the critical missing piece.** A SQL `INSERT INTO orders ...` that maintains a foreign-key relationship and several secondary indexes typically does:

1. Read the referenced row from the parent table to validate the foreign key.
2. Insert the new row into the orders table (one `put`).
3. Insert entries into every secondary index on `orders` (one `put` per index).

If the system crashes between step 2 and any of step 3, the database is left with an orders row whose index entries are missing. Subsequent reads via the affected index won't find the row; subsequent reads via the primary key will find a row whose index says it doesn't exist. This is database corruption from the application's point of view, even though every individual `put` was durable.

The fix is transactions: wrap all the `put`s in a single atomic unit so they either all happen or none happen. As §8 describes, adding transactions to `bptreedb` is a substantial undertaking, but it is the standard next step for any KV store that wants to be a relational backend.

**Concurrency is the second missing piece.** A SQL database typically serves many clients in parallel. `bptreedb` is single-threaded, so even with transactions added, only one client at a time could use it. This is fine for an embedded application (think SQLite) but not for a server (think Postgres). §4 describes the path: latches, locks, MVCC.

**A few smaller things** would also be valuable but aren't blockers:

- **Range delete.** Dropping a table with a million rows currently requires a million individual `delete` calls, each of which writes a WAL record and walks the tree. A `delete_range(start, end)` primitive that lets the backend free entire subtrees in one shot is much more efficient. Real KV stores like RocksDB, Pebble, and FoundationDB have it; CockroachDB uses it heavily for `TRUNCATE TABLE`.
- **Conditional put / compare-and-swap.** "Put this value only if the current value is X" lets the SQL layer implement optimistic concurrency control above the KV layer without holding a lock for the duration of a transaction. Real KV stores expose this primitive.
- **Snapshot reads.** For MVCC isolation, readers want to see the database as of some past LSN. As §4 mentions, LMDB gets this for free from its copy-on-write tree design; `bptreedb` would need either MVCC or a separate snapshot mechanism added.

### Verdict

The KV API is the right shape; the gap is in the runtime semantics, not the surface area. Add transactions and you have a viable storage backend for a single-user embedded SQL database (SQLite class). Add concurrency on top of that and you have a viable backend for a multi-user SQL server (Postgres class). The SQL parser, planner, executor, and schema catalog can all be built on top of the existing API without touching the backend at all.

The educational implication is worth noting: the architecture you've built here — paged file, B+ tree, WAL, buffer pool — is the same architecture that sits underneath every major SQL database. The differences are in scale, concurrency, and the number of layers above the KV interface, not in the fundamental ideas.

---

## 11. Crash testing in the wild

### What we do

`bptreedb`'s crash testing has two pieces:

1. A `FaultyFile` test fixture that wraps a real file and remembers the on-disk state at the last `fsync`. Calling `crash()` reverts the file to that state, simulating a crash that loses all writes since the last fsync. A pytest fixture monkeypatches `open` inside both `bptreedb.wal` and `bptreedb.pager`, plus the global `os.fsync`, so every write-side file the DB opens is a tracked `FaultyFile` and a single `crash_all()` call rolls every file back together — important because a realistic crash drops unfsynced writes from the WAL and the data file at the same instant, not one before the other. Read-only opens are passed through unwrapped; otherwise their initial snapshot would later overwrite legitimate writes made through a parallel writable handle.
2. A Hypothesis property test that generates random sequences of operations, applies them with randomly-chosen crash points, reopens the DB, and verifies that exactly the acknowledged operations are present afterward.

This is a strong test infrastructure for an educational project — Hypothesis is genuinely effective at finding edge cases — but it makes several simplifying assumptions about how real-world crashes manifest.

### What production does

There is a small industry of database crash and correctness testing. A non-exhaustive tour:

- **Jepsen** (https://jepsen.io) is the most famous: a framework for testing distributed databases under network partitions, clock skew, process kills, and various failure modes. Kyle Kingsbury has used Jepsen to find serious correctness bugs in essentially every popular distributed database. Jepsen is not directly applicable to a single-process embedded engine, but the methodology — generate workloads, inject faults, check the resulting history against a model — is universal.
- **ALICE** (Application-Level Intelligent Crash Explorer), from the OSDI '14 paper "All File Systems Are Not Created Equal," is a tool that injects realistic file system fault patterns into application I/O traces. ALICE found dozens of crash-consistency bugs in mature databases by simulating the kinds of partial writes and reorderings that real filesystems actually allow but that most applications don't test for.
- **Formal verification.** Some database projects (e.g., FoundationDB, Verdi) use TLA+ specifications and model checking to verify the correctness of critical algorithms. This is very effective but limited in scope (you can verify the algorithm but not its implementation).
- **Real-world torn-write testing.** Hardware rigs that physically cut power to a running database and verify the on-disk state afterward. This catches firmware bugs and lying-fsync issues that no software-only test can detect.
- **Chaos engineering tools** like Chaos Monkey (Netflix) and various open-source successors that inject failures at the infrastructure level.

### What our test misses

Several real-world failure modes that `FaultyFile` does not simulate:

**Partial sector writes.** A page-aligned write of 4 KiB is *not* atomic on most hardware. A typical hard disk sector is 512 bytes; an SSD logical block may be 512 or 4 KiB. A write that spans multiple sectors can be torn at any sector boundary. `FaultyFile` treats every write as either completely-applied or completely-lost; in reality, a page write can land partially.

**Reordered writes.** The OS may reorder writes that were issued in one order to disk in a different order. `fsync` provides a barrier — everything before the `fsync` is on disk before anything after it — but writes *between* fsyncs may be reordered freely. Our `FaultyFile` simulates the boundary correctly (anything before the last fsync is preserved, anything after is lost) but does not simulate the reordering of writes within a single fsync window.

**Lying fsyncs.** Some hardware (notably consumer SSDs and certain RAID controllers) reports `fsync` success before the data has actually reached non-volatile storage. The data is still in a volatile write cache. A power loss at this moment loses data that the application was told was durable. `bptreedb` cannot defend against this; it has to trust the OS, which has to trust the hardware.

**Filesystem-level bugs.** ext4, XFS, btrfs, and ZFS each have had periods where their crash-consistency guarantees were weaker than advertised — particularly around `rename(2)` semantics. The "atomic rename" we use for WAL truncation assumes the filesystem actually implements it atomically.

**Multi-process race conditions.** We're single-threaded, so we don't have these. Real databases that allow multiple processes to share a file (e.g., SQLite) have to use file locks and have a much harder time guaranteeing consistency.

### What it would take to bridge the gap

For a more rigorous fault model in `FaultyFile`:

1. **Track every write as a separate, independently-losable event** rather than a single all-or-nothing buffer since the last fsync. On `crash()`, choose a random subset of unfsynced writes to "apply" and the rest to "lose."
2. **Apply unfsynced writes in arbitrary order** to simulate reordering.
3. **Allow torn writes**: a write of N bytes may be applied as the first M bytes (for some random M < N) and the remaining bytes may be lost or contain stale data.
4. **Sector-level torn writes**: model writes as a sequence of 512-byte sector writes, each independently losable.

Each refinement makes the test more realistic and more painful. Postgres's test suite contains a recovery-correctness mode that does some of this; SQLite's test suite is famously thorough and includes tests for many filesystem-specific edge cases.

For real-hardware testing, a project would need a power-cut rig: a microcontroller-controlled power switch, a workload that runs continuously, an external observer that records what was acknowledged, and an automated reboot-and-check loop. This is impractical for an educational project but is how real databases earn their durability claims.

---

## 12. What I learned

A short post-build appendix listing the things that only became sharp once the code existed.

**The `CHECKPOINT` WAL record earns a second role beyond "recovery cutoff."** The spec introduced it as the marker that lets recovery skip the prefix of the WAL it doesn't need to replay. But once `_repair_pager_meta_from_wal_checkpoint` existed, the same record was also the meta-page snapshot used to roll a stale-but-valid meta forward — closing the crash window between "WAL has the new checkpoint" and "meta has the new checkpoint." Two unrelated guarantees, one record, no extra bytes.

**The 20%-versus-25% record cap is structural, not stylistic.** The spec said `/5` from the start without making a fuss about it. Then the redistribute-or-merge code refused to terminate on certain skewed inputs until the cap was tightened from a tentative `/4` back to `/5`. The reason — that `/5` makes "two underpopulated pages plus the pulled-down separator always fit together" provable as an invariant — only crystallized as a consequence of `_redistribute_or_merge_page`'s "this should not happen" assertion never firing in practice.

**Split-point selection is not "the byte midpoint."** A naive midpoint split breaks on pages where one giant record sits next to many tiny ones: the giant half lands above the threshold, the small half lands below it, and the rebalance code immediately wants to merge the freshly-created page back. The implementation walks outward from the midpoint (+1, -1, +2, -2, ...) looking for a split that leaves both halves above the underpopulation threshold, falling back to the midpoint only when no balanced index exists. None of this was spec'd; it surfaced as Hypothesis counterexamples to "split never creates an underpopulated page."

**`_maybe_checkpoint` has to run *before* the mutation, not just after.** The post-mutation call ("did we just cross a threshold?") is the obvious one. The pre-mutation call ("do we have headroom for what we're about to do?") looks redundant until you trace what happens when a `put` lands on a 90%-full cache and the resulting split needs three new dirty slots: the post-call would fire, but only after the cache had already overflowed and `DBBufferPoolOverflowError` had aborted the operation. The pre-call exists because rebalances are not transactional and there's no clean way to roll one back.

**`DBBufferPoolOverflowError` exists because heuristics aren't proofs.** The 50% dirty-ratio threshold is a soft target; it doesn't *guarantee* the cache won't fill with dirty pages. The exception is the hard rail beneath it — when eviction has no clean victim, the only NO-STEAL-compatible answer is "fail." Knowing the safety net is there changes how you reason about the soft thresholds: they're heuristics to keep the rail far away, not invariants that prove the rail is unreachable.

**`FaultyFile` had to wrap the pager too, not just the WAL.** The first version only patched WAL `open`s, because the WAL was where durability happened. But the pager writes the meta page on every checkpoint, and a crash between "data file fsynced" and "meta updated" needs both files rolled back together — otherwise the test sees a state that no real crash could produce. Adding the pager to the patch list was the difference between "tests pass" and "tests find bugs."

---

## 13. Glossary

The terms used in this document, in alphabetical order.

**ACID.** The four properties of a transaction: Atomicity, Consistency, Isolation, Durability.

**ARIES.** Algorithm for Recovery and Isolation Exploiting Semantics. The dominant database recovery algorithm in commercial systems, originally published by IBM in 1992. Three-pass: analysis, redo, undo.

**Atomic write.** A write whose contents are either entirely applied or entirely not applied — no partial state visible after a crash. Single sector writes are usually atomic; larger writes typically are not.

**Buffer manager / buffer pool.** The in-memory cache of pages that sits between application code and the data file. Owns dirty-page tracking and the eviction policy.

**Bump allocation.** Allocating new resources by incrementing a counter. The simplest possible allocator.

**Checkpoint.** A point in time at which the buffer pool's dirty pages are flushed to the data file and the WAL can be truncated.

**Clustered index.** An index whose leaf pages contain the actual rows of the table. The table is stored in primary-key order.

**Compensation log record (CLR).** In ARIES, a log record written during the undo pass that describes an undo step. Used to make undo itself crash-recoverable.

**CRC (Cyclic Redundancy Check).** A checksum used to detect (not correct) data corruption. CRC32 is the variant typically used in databases.

**Dirty page.** A page that has been modified in the buffer pool but not yet written to the data file.

**FORCE.** A buffer-manager policy in which all of a transaction's dirty pages are flushed to disk at commit time. The opposite of NO-FORCE.

**FPI (Full-Page Image).** A WAL record containing the entire contents of a page. Used by Postgres to defend against torn writes: the first modification to a page after a checkpoint writes a FPI, so recovery can rebuild the page from scratch.

**Free Space Map (FSM).** An index that tracks how much free space each page has. Used by Postgres to find a page with enough room for an insert.

**Fsync.** A system call (`fsync(2)`) that forces buffered file writes to durable storage. Until `fsync` returns successfully, the OS may have your data only in volatile memory.

**Heap.** An unsorted file of rows, indexed by other structures. Postgres tables are heaps; rows live in arrival order.

**Latch.** A short-duration mutex protecting a single page or buffer-pool slot. Held for microseconds. Distinct from a lock.

**Latch coupling.** A tree-traversal technique that holds a latch on a child before releasing the latch on its parent, so concurrent splits do not lose the walker.

**Lock.** A transaction-scoped abstraction protecting a logical entity (row, range, table). Held for the duration of a transaction. Managed by a lock manager.

**Lock manager.** The subsystem responsible for granting locks, detecting deadlocks (typically via wait-for graphs), and handling lock escalation.

**Log Sequence Number (LSN).** A monotonically increasing integer identifying a position in the WAL.

**Logical logging.** A WAL style in which records describe high-level operations like "PUT(key, value)." Recovery re-executes the operations.

**MVCC (Multi-Version Concurrency Control).** A concurrency mechanism in which each row has multiple versions, each tagged with the transaction id that created it. Readers see snapshots; writers create new versions; readers and writers do not block each other.

**NO-FORCE.** A buffer-manager policy in which dirty pages are not forced to disk at commit time. Commit just fsyncs the WAL. Requires REDO logging.

**NO-STEAL.** A buffer-manager policy in which dirty pages are not allowed to be written to disk before the transaction that modified them commits. Avoids the need for UNDO logging.

**Off-page storage.** A scheme for storing oversized values outside the main page, in linked overflow pages, with the main row holding a pointer.

**Order-preserving encoding.** A serialization scheme for typed values (ints, strings, tuples) such that the lexicographic byte ordering of the encoded form matches the natural ordering of the values. Required for storing relational tables in an ordered KV store, so that range scans on a column return rows in the right order.

**Overflow page.** A page used to store the continuation of a value that didn't fit in a single page.

**Physical logging.** A WAL style in which records describe byte-level changes to specific pages. Recovery applies the bytes directly.

**Physiological logging.** A hybrid WAL style in which records describe page-level operations ("insert this slot at this position on this page") that are logical within a single page.

**Ping-pong meta pages.** A scheme of two meta pages alternated each checkpoint, so a torn write to one leaves the other intact.

**REDO.** Re-applying a change during recovery, typically because the change was committed but the dirty page didn't make it to disk before the crash.

**REDO log.** A log of forward changes used for redo during recovery. Synonym for WAL in this context.

**Repeatable read.** An isolation level in which a transaction sees a consistent snapshot for its entire duration.

**Sector.** The smallest unit of read or write on a disk. Typically 512 bytes on hard disks; 4 KiB on modern SSDs.

**Serializable.** The strictest isolation level: concurrent transactions appear to execute in some serial order.

**Snapshot isolation.** An isolation level in which a transaction sees the database as it was when the transaction began. Implemented via MVCC.

**System catalog.** A set of database-managed tables (or, in a KV store, a reserved key range) holding metadata about the schema: tables, columns, types, indexes, constraints. Queryable via the same interface as user tables.

**STEAL.** A buffer-manager policy in which dirty pages may be written to disk before the transaction that modified them commits. Requires UNDO logging.

**TID (Tuple Identifier).** Postgres's name for a (page id, slot index) physical pointer to a heap row.

**TOAST (The Oversized-Attribute Storage Technique).** Postgres's mechanism for storing oversized column values out of line.

**Torn write.** A write that was partially applied to disk before a crash. Common at sector boundaries on multi-sector writes.

**Transaction.** A logical unit of work that is atomic, consistent, isolated, and durable.

**UNDO.** Reversing a change during recovery or transaction abort. The opposite of REDO.

**UNDO log.** A log of pre-images or reverse operations used for undo during transaction rollback or recovery.

**WAL (Write-Ahead Log).** A separate, append-only file that records every change before it is applied to the data file. The foundation of crash recovery in most databases. Synonym (in this document's usage) for REDO log.

**Write-ahead.** The discipline of writing the log record describing a change *before* writing the change itself.
