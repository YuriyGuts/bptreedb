# bptreedb — Design Specification

---

## 1. Purpose and goals

`bptreedb` is an educational, single-process, single-threaded key-value database written in Python. Its purpose is to teach — through implementation — how real database engines achieve three properties simultaneously:

1. **Indexed lookup and ordered iteration** via an on-disk B+ tree.
2. **Bounded memory and disk-friendly access patterns** via a fixed-size paged file format and a buffer pool.
3. **Crash safety** via a write-ahead log, a deterministic recovery procedure, and periodic checkpoints.

This is not a production database. It is a teaching artifact: every design choice favours conceptual clarity and testability over throughput, scale, or feature completeness. Where this document deviates from what a production engine like PostgreSQL, SQLite, or LMDB would do, the deviation is deliberate, and a companion document (`production-readiness-gap-analysis.md`) will catalogue the differences after v0 ships.

### Success criteria

A successful v0 implementation:

- Can `put`, `get`, `delete`, and `scan` arbitrary `bytes` keys and values.
- Persists across process restarts.
- Survives a process kill at any moment with no data loss for acknowledged operations and no spurious data for unacknowledged ones.
- Passes a property-based test suite that compares its behaviour against a Python `dict`, including under simulated crashes.
- Is composed of small, well-bounded modules that can each be understood and tested in isolation.

---

## 2. Scope and non-goals

### In scope (v0)

- Bytes keys, bytes values, lexicographic key ordering.
- Configurable page size (default 4 KB), stored in the database file.
- Slotted-page layout with variable-length records.
- B+ tree with split, merge, and redistribution.
- Singly-linked freelist for page reuse.
- LRU buffer pool with NO-STEAL / FORCE-AT-CHECKPOINT policy.
- Logical write-ahead log (one record per public-API operation), fsynced on every write.
- Recovery on open by replay from the last checkpoint.
- Range scans via leaf sibling pointers, with version-counter invalidation on concurrent mutation.
- Crash-safety property tests using a fault-injection file wrapper.

### Out of scope (v0)

- Concurrency. Single-threaded only. The API is blocking.
- Transactions (`begin` / `commit` / `abort`). Each `put` and `delete` is its own atomic, durable unit.
- Secondary indexes, schemas, types beyond raw bytes.
- Compression, encryption, replication.
- Records larger than `page_size / 5`. Oversized records are rejected with `DBRecordTooLargeError`.
- Online schema or page-size changes.
- Mutation-tolerant range cursors (deferred to v1; see Section 10).
- Dual / ping-pong meta pages for additional torn-write protection.

---

## 3. Architecture

The system is a stack of stateful services built on top of two pure, stateless foundations: a **data model** (plain dataclasses) and a **wire-format codec** (pure functions). Each service talks only to the service immediately below it and to the data model / codec. Inter-layer boundaries are the most important property of the design — they make the system comprehensible, replaceable, and testable.

```
┌──────────────────────────────────────────┐
│ 6. Public API (DB class)                 │  put / get / delete / scan / close
├──────────────────────────────────────────┤
│ 5. B+ Tree                               │  search, insert, delete, range scan
│    (operates on Page dataclasses)        │  splits, merges, redistribution
├──────────────────────────────────────────┤
│ 4. Buffer Pool (LRU cache)               │  page_id → in-memory Page object
│    + dirty tracking                      │  pins dirty pages between checkpoints
├──────────────────────────────────────────┤
│ 3. Pager / File I/O                      │  read_page(id), write_page(id, bytes)
│    + freelist + meta page                │  allocate_page(), free_page(id)
├──────────────────────────────────────────┤
│ 2. WAL                                   │  append, fsync, replay, truncate
└──────────────────────────────────────────┘
              ▲                  ▲
              │                  │
      ┌───────┴────────┐  ┌──────┴──────────┐
      │ entities.py    │  │ codec.py        │
      │ (data model:   │  │ (wire format:   │
      │  dataclasses)  │  │  pure encode /  │
      │                │  │  decode funcs)  │
      └────────────────┘  └─────────────────┘
```

**Two transverse modules, used by every layer:**

- **`entities.py` — the data model.** Plain, mutable Python dataclasses describing everything that has a persistent on-disk form or a durable in-memory form: `WALPutRecord`, `WALDeleteRecord`, `WALCheckpointRecord`, `MetaPage`, `LeafPage`, `InternalPage`, `FreelistPage`, `LeafSlot`, `InternalSlot`. These classes contain **no I/O, no serialisation, and no validation of wire-format bytes**. They are the shared vocabulary all layers use to pass structured data to each other.
- **`codec.py` — the wire-format codec.** Pure functions: `encode_wal_record(record) -> bytes`, `decode_wal_record(buf) -> WALRecord`, `encode_page(page) -> bytes`, `decode_page(buf) -> Page`, `encode_meta_page(meta) -> bytes`, `decode_meta_page(buf) -> MetaPage`. Plus small buffer helpers (`BufferReader`, `BufferWriter`) and a CRC verifier. The codec **never** touches a file descriptor; it operates only on `bytes` / `bytearray`. This keeps wire-format tests trivial (round-trip `encode(decode(x)) == x`) and makes every stateful service above replaceable without rewriting the serializer.

Keeping the data model separate from both the wire format and the stateful services is the single most important structural decision in the design. It means:

- The tree algorithms manipulate `LeafPage` / `InternalPage` dataclasses without knowing a byte offset table exists.
- The codec describes the on-disk format in one place and does nothing else.
- The WAL, Pager, and BufferPool are small wrappers around "apply the codec to bytes we read/write to a file."

If you ever catch yourself wanting a `SlottedPage` class that wraps a mutable `bytearray` and provides `insert_slot` / `find_slot_for_key` / `compact`, stop — that design conflates the three roles above. Use a `LeafPage` dataclass with a `list[LeafSlot]` instead, mutate the list directly, and let `encode_page` handle byte-level layout at flush time.

### Layer responsibilities (detailed APIs are in Section 7)

**Layer 2 — WAL.** Owns the log file. Append-only. Knows the `entities.WALRecord` family but nothing about page contents. Delegates all byte-level work to `codec.encode_wal_record` / `codec.decode_wal_record`. Its job is the I/O discipline: append, fsync, replay-with-torn-tail, truncate.

**Layer 3 — Pager.** Owns the data file. Knows the page size and the file offset of each page id. Owns the meta page (page 0) and the freelist. Reads and writes raw page-sized `bytes` buffers at page offsets; decoding/encoding those buffers via `codec.py` is done by the Pager itself for the meta page (which it manages directly) and by the Buffer Pool for all other pages. The Pager has no knowledge of the WAL, the buffer pool, or page contents above the type tag. It is "a typed file."

**Layer 4 — Buffer Pool.** Sits on top of the Pager. Holds at most `cache_capacity_pages` decoded `Page` objects in memory. On a miss, it asks the Pager for the raw bytes and calls `codec.decode_page` to produce a dataclass. On flush, it calls `codec.encode_page` and asks the Pager to write the bytes. Implements the **NO-STEAL / FORCE-AT-CHECKPOINT** policy: dirty pages are pinned (cannot be evicted). LRU eviction operates on clean pages only. If a `get` requires loading a page and there are no clean pages to evict, the buffer pool triggers a checkpoint to reclaim space.

**Layer 5 — B+ Tree.** The algorithms. Walks the tree by asking the buffer pool for `LeafPage` / `InternalPage` dataclasses by id, mutating their slot lists in place, and marking them dirty with the current LSN. Writes one WAL record per public-API operation, **before** mutating any page. Increments a version counter on every successful mutation.

**Layer 6 — DB.** The public API. Opens both files, runs recovery, owns all the services below, runs checkpoints, closes cleanly. Translates user-level errors into the `DBError` hierarchy.

### Why this layering matters

- Each layer can be unit-tested with a fake or in-memory version of the layer below.
- A bug in any one layer cannot silently leak across the boundary.
- Re-implementing one layer (e.g., switching the buffer pool from LRU to clock) does not require touching the others.
- A reader can understand the system one layer at a time.

---

## 4. On-disk format

> **How to read this section.** Every table below describes a **wire format** — the byte-level layout that `codec.py` reads and writes. For each wire format there is a corresponding **in-memory dataclass** in `entities.py`. Layers above the codec (tree, buffer pool, WAL, DB) work with the dataclass exclusively and never touch bytes. The codec is the only module that knows about offsets, padding, or CRCs. When the table lists `free_space_start`, `free_space_end`, `num_slots`, etc., those fields are **computed on encode** from the dataclass's `slots` list; they do not need to be stored in the dataclass itself. The only header field the in-memory dataclass must explicitly carry is `last_modified_lsn`, because the tree sets it directly.

### 4.1 Files

A database is a **directory** containing exactly two files:

1. `bptreedb.data` — the data file. A sequence of fixed-size pages managed by the Pager.
2. `bptreedb.wal` — the write-ahead log. An append-only sequence of records managed by the WAL.

The directory layout (rather than a base-path with two suffixed files) avoids the risk of typos creating orphans and makes the database trivially backed up or removed as a unit. There is no separate journal, no separate index, no separate manifest. Opening a non-existent directory creates it; opening an existing directory that also contains unrelated files is allowed — the DB owns only the files it created, and foreign files are ignored rather than treated as corruption.

### 4.2 The data file

The data file is exactly `N × page_size` bytes for some `N ≥ 1`. Page 0 is the meta page; pages `1..N-1` are tree nodes or freelist nodes. New pages are appended at the end (bumping `next_page_id`) when the freelist is empty.

#### 4.2.1 Meta page (page 0)

The meta page has a fixed layout (not slotted). All multi-byte integers are little-endian unsigned.

| offset | size | field                 | description                                          |
|-------:|-----:|-----------------------|------------------------------------------------------|
|      0 |    8 | `magic`               | `b"BPTREEDB"`                                        |
|      8 |    4 | `version`             | format version, currently `1`                        |
|     12 |    4 | `page_size`           | page size in bytes                                   |
|     16 |    8 | `root_page_id`        | page id of the B+ tree root                          |
|     24 |    8 | `freelist_head`       | page id of the head freelist page; `0` if empty      |
|     32 |    8 | `next_page_id`        | next page id to bump-allocate                        |
|     40 |    8 | `last_checkpoint_lsn` | LSN of the last successful checkpoint                |
|     48 |    4 | `crc32`               | CRC32 over bytes `0..48`                             |
|     52 |  ... | (zero padding)        | up to `page_size`                                    |

The CRC is verified on open. A failed CRC at this layer means the database is unrecoverable from the data file alone, and `DBCorruptedError` is raised. (Adding a second meta page for ping-pong protection is a v1 task; see Section 10.)

#### 4.2.2 Slotted page layout (internal & leaf nodes)

Internal and leaf nodes share the same on-disk layout: a header at the front, a slot array growing downward from the end of the header, and variable-length records growing upward from the end of the page. The free space lives between the slot array and the records.

```
┌──────────────┬──────────┬──────────┬──┄┄──┬──────────┬──────────┐
│ page header  │ slot[0]  │ slot[1]  │ ...  │ record N │ record 0 │
└──────────────┴──────────┴──────────┴──┄┄──┴──────────┴──────────┘
0 ────────────────────────────────────────────────────────► page_size
                slot array grows →           ← records grow
```

**Page header (32 bytes):**

| offset | size | field                | description                                            |
|-------:|-----:|----------------------|--------------------------------------------------------|
|      0 |    1 | `type_tag`           | `0x01` internal, `0x02` leaf, `0x03` freelist          |
|      1 |    3 | reserved             | zero, alignment padding                                |
|      4 |    4 | `num_slots`          | number of records on this page                         |
|      8 |    4 | `free_space_start`   | offset of the byte just past the last slot             |
|     12 |    4 | `free_space_end`     | offset of the first byte of the lowest record          |
|     16 |    8 | `last_modified_lsn`  | LSN of the last WAL record that touched this page      |
|     24 |    8 | `right_sibling_page_id`      | leaves: page id of the next leaf in key order, else `0`; internal nodes: leftmost-child page id |

The `right_sibling_page_id` field is overloaded by node type. In a leaf, it is the next-leaf pointer used by `scan`. In an internal node, it stores the leftmost child pointer (the one without a separator key in front of it). This overload keeps the header a single fixed shape and avoids a special "slot −1" case.

**Slot entry (8 bytes):**

| offset | size | field            |
|-------:|-----:|------------------|
|      0 |    4 | `record_offset`  |
|      4 |    4 | `record_length`  |

**Leaf record:**

```
4 bytes  key_length     uint32
N bytes  key
4 bytes  value_length   uint32
M bytes  value
```

**Internal record:**

```
4 bytes  key_length     uint32
N bytes  key
8 bytes  child_page_id  uint64
```

The keys in slot order are sorted ascending. For an internal node with `k` slots, there are `k + 1` children: the leftmost child (in the header) and one child per slot.

**In memory**, a page is a `LeafPage` or `InternalPage` dataclass whose `slots` attribute is a plain Python list. Insertion is `slots.insert(i, slot)`, deletion is `slots.pop(i)`, and split-by-byte-size is a loop that sums encoded sizes until past the midpoint. There is no free-space bookkeeping in memory — the list is the truth.

**On encode** (in `codec.encode_page`), the codec lays out the slot array growing downward from the header, packs the records growing upward from `page_size_bytes`, and populates `num_slots`, `free_space_start`, and `free_space_end` in the header accordingly. Because the codec re-packs from scratch on every encode, there are no "deletion holes" to compact — compaction is implicit.

This is a deliberate simplification versus a production engine, which would mutate a byte buffer in place to avoid the re-pack cost. For a single-threaded teaching database, re-packing on encode is fine: flushes happen at checkpoint frequency, not per operation, and the code that results is dramatically simpler than in-place slot arithmetic.

#### 4.2.3 Freelist page

A freelist page is a singly-linked list element holding freed page ids:

```
page header (type_tag = 0x03, num_slots = count of stored ids)
8 bytes  next_freelist_page_id   uint64
8 bytes × count  freed page ids
```

The Pager's freelist head is `meta.freelist_head`. When a page is freed it is pushed onto the head page; if the head is full, a new freelist page is allocated and linked. When a page is needed and the freelist is non-empty, the Pager pops from the head; if the head becomes empty, the freelist head advances to `next_freelist_page_id`.

### 4.3 Size limits

- The maximum usable record size is approximately `page_size / 5`. The exact threshold, accounting for the 32-byte header and per-slot overhead, is computed by the implementation as `(page_size - header_size) / 5 - slot_size`. For `page_size = 4096` this is 806 bytes; for `page_size = 256` it is 38 bytes. Records larger than this threshold are rejected with `DBRecordTooLargeError`. The `/5` (rather than the more intuitive `/4`, which would also guarantee "four records fit in an empty page") keeps the maximum slot size below `0.2 * page_size + 24`. That bound is what makes the half-full invariant always achievable: with a tighter cap, split and merge-then-split can always find a partition where both halves land above the 40% threshold; at `/4`, extreme slot-size distributions can force an unbalanced split that leaves one half underfull.
- The minimum supported `page_size` is 256 bytes. Below that, the threshold above degenerates and split decisions become awkward. The default `page_size` is 4096.

### 4.4 The WAL file

The WAL is an append-only sequence of length-prefixed, CRC-protected records.

**Record framing:**

```
4  bytes  record_length   uint32   (length of everything after this field)
8  bytes  lsn             uint64
1  byte   op_type         uint8
... op-specific payload ...
4  bytes  crc32           uint32   (over record_length, lsn, op_type, payload)
```

**Op types:**

- `0x01` PUT
- `0x02` DELETE
- `0x03` CHECKPOINT

**PUT payload:** `key_length(4) | key | value_length(4) | value`
**DELETE payload:** `key_length(4) | key`
**CHECKPOINT payload:** `root_page_id(8) | freelist_head(8) | next_page_id(8)`

The CRC is essential. Without it, recovery cannot distinguish a half-written torn record at the end of the log from a complete one. Recovery's torn-tail detection: walk records from the start, validating each CRC and verifying that `record_length` does not run past EOF and that LSNs are strictly increasing. Stop at the first record that fails any check; truncate the WAL to the end of the previous good record.

LSNs are assigned monotonically by the WAL writer starting from `last_checkpoint_lsn + 1` on a fresh database (1 on the first ever record).

---

## 5. B+ tree algorithms

This section describes the tree's behaviour. It performs no I/O directly: it requests pages from the buffer pool and marks them dirty. The buffer pool and the WAL guarantee durability.

### 5.1 Invariants

Let `B` denote the (variable) number of records that fit in a page. The tree maintains:

1. **Values live only in leaves.** Internal nodes store separator keys and child pointers, never values.
2. **Leaves form a forward-linked list** via `right_sibling_page_id`. The rightmost leaf's `right_sibling_page_id` is `0`.
3. **All leaves are at the same depth.** The tree grows by splitting upward, never sideways.
4. **Slot arrays are sorted ascending by key.**
5. **Half-full rule.** Every non-root node uses at least 40% of its page for records. This is the variable-length analogue of the textbook "at least ⌈B/2⌉ keys" rule.
6. **Root is exempt** from the half-full rule. An empty database has a root that is an empty leaf.
7. **Internal node separator invariant.** For an internal node with separator keys `s[0] < s[1] < ... < s[k-1]` and children `c[0], c[1], ..., c[k]`, every key in `c[i]` is `< s[i]` and every key in `c[i+1]` is `≥ s[i]`. (For B+ trees with leaf-derived separators, the convention is that `s[i]` equals the smallest key in `c[i+1]`.)

These invariants are checked by the `assert_tree_invariants` test helper after every property-test operation.

### 5.2 `search(key)`

```
node = read_page(root_page_id)
while node.is_internal:
    i = first slot index where key < slot[i].key       (binary search)
    child_id = slot[i-1].child if i > 0 else node.leftmost_child_page_id
    node = read_page(child_id)
# node is a leaf
i = slot index where slot[i].key == key
return slot[i].value if found else None
```

### 5.3 `insert(key, value)`

Top-down search, bottom-up split.

1. Walk from the root to the target leaf, **remembering the path** as a stack of `(parent_page_id, child_index_within_parent)` pairs.
2. In the leaf:
   - If the key already exists, replace its value. (This may still trigger a split if the new value is larger.)
   - Otherwise insert the new record in sorted position.
3. If the leaf still satisfies the page-fits constraint, mark dirty and return.
4. **Otherwise split the leaf:**
   - Allocate a new leaf page from the Pager.
   - Move the upper half of the records (by byte size, not by count) into the new leaf.
   - Splice the new leaf into the sibling chain: `new_leaf.right_sibling_page_id = old_leaf.right_sibling_page_id; old_leaf.right_sibling_page_id = new_leaf.id`.
   - The promoted key is the **smallest key now in the new leaf** (B+ tree convention: separator equals first key of right sibling).
   - Mark both leaves dirty.
5. **Propagate the split upward.** Pop the parent off the path stack, insert `(promoted_key, new_leaf_id)`, mark dirty. If the parent is now over-full, split it as an internal node:
   - Allocate a new internal page.
   - Move the upper half of the separators to the new node, along with their corresponding child pointers.
   - The **median separator** is removed from the children entirely and promoted to the grandparent. (This is the asymmetry between leaf and internal splits: leaf splits *copy* the smallest right-side key up; internal splits *move* the median up.)
6. **If the root splits**, allocate a new internal page, give it two children (the old root and its new sibling) and one separator key, and update `meta.root_page_id` via the Pager. The meta page is marked dirty and will be flushed at the next checkpoint.

### 5.4 `delete(key)`

Top-down search, bottom-up rebalance.

1. Walk to the leaf, remembering the path.
2. In the leaf, locate the key. If absent, return `False`. Otherwise remove the slot, mark dirty.
3. If the leaf is still ≥ 40% full, return `True`.
4. **Otherwise rebalance.** Examine the immediate left and right **same-parent** siblings (both are looked up via the parent's child list, which is why the path stack matters; the leaf's `right_sibling_page_id` pointer is *not* used here, because it may cross parent boundaries and merging requires that both nodes share a parent):
   - **Redistribute** if a sibling has spare bytes such that moving one record across leaves both nodes ≥ 40% full. Move one record across, update the parent's separator key.
   - **Merge** otherwise. Combine the underfull leaf with one sibling into a single page (the chosen direction can be either; the algorithm picks the side with less data). Splice out the freed page from the sibling chain. Free the now-empty page via the Pager. Remove the corresponding separator key (and child pointer) from the parent.
5. Step 4's merge may make the parent under-full. Recurse: redistribute or merge among the parent's siblings, removing a separator from the grandparent, and so on.
6. **If the root becomes empty** in this process (an internal root left with only one child), the tree shrinks: set `meta.root_page_id` to that single child via the Pager, free the old root.

### 5.5 `scan(start, end)` — v0 (live iterator with version check)

```
snapshot_version = tree.version_counter
leaf, slot_index = find_leaf(start)        # smallest leaf containing key ≥ start
while True:
    if tree.version_counter != snapshot_version:
        raise DBConcurrentPageModificationError
    while slot_index < leaf.num_slots:
        key, value = leaf.slot(slot_index)
        if end is not None and key >= end:
            return
        yield (key, value)
        slot_index += 1
    if leaf.right_sibling_page_id == 0:
        return
    leaf = read_page(leaf.right_sibling_page_id)
    slot_index = 0
```

The iterator does not pin pages between `next()` calls. It re-fetches the current leaf from the buffer pool on each iteration, which is cheap on a cache hit and correct in the presence of cache evictions of clean pages.

`tree.version_counter` is bumped at the end of every successful `put` and `delete`.

### 5.6 What gets logged

Each public-API mutation produces exactly one WAL record before any page is mutated. The order is strict:

```
def put(key, value):
    lsn = wal.append(PUT, key, value)
    wal.fsync()
    tree._insert(key, value, lsn)        # tree marks all dirty pages with last_modified_lsn = lsn
    tree.version_counter += 1
```

A single `put` may split one leaf and propagate splits all the way up the tree, touching O(log N) pages. **It still produces only one WAL record.** Recovery replays the logical operation, which deterministically performs the same splits.

### 5.7 Determinism requirement

Logical replay only works if the tree's behaviour on `put` / `delete` depends solely on the tree's current contents and on the Pager's deterministic state (root id, freelist, `next_page_id`). It must not depend on:

- wall-clock time,
- random numbers,
- the order in which pages happen to be cached,
- iteration order over a `dict` or `set`.

The implementation must be disciplined about this. The crash-recovery property tests will catch violations.

---

## 6. WAL, recovery, and checkpoints

### 6.1 The buffer-pool policy

`bptreedb` uses a **NO-STEAL / FORCE-AT-CHECKPOINT** policy:

- **NO-STEAL.** Dirty pages are never written to the data file outside of a checkpoint. Between checkpoints they accumulate in the buffer pool, pinned. The LRU eviction policy operates on clean pages only.
- **FORCE-AT-CHECKPOINT.** A checkpoint flushes every dirty page, fsyncs the data file, updates the meta page, fsyncs again, then truncates the WAL.

This is the simplest correct combination of buffer-pool policies. It requires only REDO logging (which is what our logical WAL is); no UNDO log is needed because no uncommitted-by-checkpoint state ever reaches disk.

The cost: dirty pages occupy buffer-pool space until the next checkpoint. To bound memory, checkpoints are triggered:

1. When the WAL grows past `checkpoint_wal_size_bytes` (default 4 MiB).
2. When the fraction of dirty pages in the buffer pool exceeds `checkpoint_dirty_page_ratio` (default 0.5).
3. When `db.close()` is called.
4. Manually via `db.checkpoint()`.

Both thresholds are constructor parameters of `DB` (see Section 7.1).

### 6.2 LSN bookkeeping

Every dirty page records `last_modified_lsn` in its header — the LSN of the most recent WAL record that modified it. This is preserved when pages are flushed and reloaded so the invariant survives across reopens. Because `bptreedb` is NO-STEAL, the WAL ordering rule (page may not be written until WAL up to `last_modified_lsn` is fsynced) is automatically satisfied: pages are only written during a checkpoint, and a checkpoint always fsyncs the WAL first.

### 6.3 Recovery procedure

Triggered when opening the DB (entering a `with DB(...) as db:` block, or calling `db.open()` explicitly). Sequence:

1. **Read meta page (page 0).** Verify `magic`, `version`, and `crc32`. On any mismatch, raise `DBCorruptedError`.
2. **Initialise the in-memory tree object** pointing at `meta.root_page_id`, with `freelist_head` and `next_page_id` taken from the meta page.
3. **Open the WAL** and walk it from the start, validating each record's CRC and verifying that LSNs are strictly increasing. Stop at the first record that fails any check — this is the torn tail. Truncate the WAL on disk to the end of the last good record.
4. **Identify the replay range:** every record with `lsn > meta.last_checkpoint_lsn`. Records with smaller LSNs are already reflected in the data file; replaying them would corrupt the tree.
5. **Replay loop.** For each record in the replay range:
   - PUT → call `tree._insert(key, value, lsn)`.
   - DELETE → call `tree._delete(key, lsn)`.
   - CHECKPOINT → ignore. (CHECKPOINT records exist only as truncation markers and as a sanity check on `last_checkpoint_lsn`.)
6. **Eviction is disabled during recovery.** The buffer pool is allowed to grow unbounded for the duration of replay. This avoids any partial-state evictions during a half-replayed sequence.
7. **Take an immediate checkpoint** to persist the recovered state and re-enable normal eviction.
8. The DB is now open.

Recovery is **idempotent and restartable**: a crash during step 5 or 6 leaves the data file unchanged from the previous checkpoint, and the next open will repeat steps 1–6 to the same effect.

### 6.4 Checkpoint procedure

1. Note `wal.next_lsn` as `ckpt_lsn`. Operations acknowledged from this point on belong to the next checkpoint window.
2. **Flush every dirty page** in the buffer pool to the data file via the Pager. (Pages are written individually; no fsync between them.)
3. **fsync the data file.** Now everything described by WAL records with `lsn < ckpt_lsn` is durably in the data file.
4. **Append a CHECKPOINT WAL record** at `ckpt_lsn` containing the current `(root_page_id, freelist_head, next_page_id)`. Then `wal.fsync()`.
5. **Update the meta page** in memory: set `last_checkpoint_lsn = ckpt_lsn` and copy current `root_page_id`, `freelist_head`, `next_page_id`. Recompute the CRC. Write page 0 via the Pager. **fsync the data file again.**
6. **Truncate the WAL** to discard records with `lsn < ckpt_lsn`. (The CHECKPOINT marker itself, at `ckpt_lsn`, is retained as the new "first record.")

### 6.5 Crash-window analysis

| Crash point                                         | Effect on next open                                                                 |
|-----------------------------------------------------|-------------------------------------------------------------------------------------|
| Before step 3 (data fsync)                          | Meta still points at previous checkpoint. Recovery replays full WAL. ✓              |
| Between steps 3 and 5 (data current, meta stale)    | Recovery replays the just-checkpointed range against the already-updated tree. Idempotent because logical replay is deterministic. ✓ |
| Between steps 5 and 6 (meta advanced, WAL not yet truncated) | Recovery reads the new `last_checkpoint_lsn` and skips the already-applied range. ✓ |
| After step 6                                        | Clean state. Nothing to replay. ✓                                                   |

The "idempotent across an already-applied checkpoint window" property in row 2 depends on the NO-STEAL policy: because no dirty pages were ever written outside of a checkpoint, the data file at recovery time is **always either entirely pre- or entirely post- a given checkpoint**. There is no "half-applied" intermediate state to corrupt. This is the load-bearing reason `bptreedb` uses NO-STEAL rather than the more performant STEAL policies.

---

## 7. Public API

### 7.1 The `DB` class

```python
DB(
    dir_path: str | Path,
    *,
    page_size_bytes: int = 4096,
    cache_capacity_pages: int = 256,
    checkpoint_wal_size_bytes: int = 4 * 1024 * 1024,
    checkpoint_dirty_page_ratio: float = 0.5,
)
```

Constructs a `DB` handle for the directory `dir_path`. The handle is used as a context manager:

```python
with DB(dir_path) as db:
    db.put(b"k", b"v")
```

Entering the context (or calling `db.open()` explicitly) opens or creates the database. The directory contains exactly two files: `bptreedb.data` and `bptreedb.wal` (see Section 4.1). On open, validates the meta page, runs recovery if the WAL is non-empty, and takes an initial checkpoint. If the directory does not exist, it is created with an empty tree (a single empty leaf as the root). If the directory exists but contains files other than `bptreedb.data` and `bptreedb.wal`, `DBCorruptedError` is raised. Exiting the context calls `db.close()`.

Parameters:

- **`page_size_bytes`** — page size in bytes. Honoured only on creation; opening an existing database uses the page size stored in its meta page and ignores this argument. Must be at least 256. Default: 4096.
- **`cache_capacity_pages`** — maximum number of pages held in the buffer pool. Default: 256.
- **`checkpoint_wal_size_bytes`** — checkpoint is automatically triggered when the WAL grows past this size. Default: 4 MiB.
- **`checkpoint_dirty_page_ratio`** — checkpoint is automatically triggered when the fraction of dirty pages in the buffer pool exceeds this ratio. Must be in `(0.0, 1.0]`. Default: 0.5.

```python
db.put(key: bytes, value: bytes) -> None
```

Insert or overwrite. Raises `ValueError` if `key` or `value` is not `bytes`. Raises `DBRecordTooLargeError` if the encoded record would exceed `page_size / 5`. Durable when this method returns: the WAL has been appended and fsynced. Bumps the tree's version counter.

```python
db.get(key: bytes) -> bytes | None
```

Returns the value, or `None` if the key is not present. Does not modify the version counter. Returns a fresh `bytes` object so the caller can hold onto it across other operations.

```python
db.delete(key: bytes) -> bool
```

Returns `True` if a key was removed, `False` if it was already absent. Durable when this method returns. Bumps the version counter on a successful removal.

```python
db.scan(
    start_key_inclusive: bytes | None = None,
    end_key_exclusive: bytes | None = None,
) -> Iterator[tuple[bytes, bytes]]
```

Half-open range `[start_key_inclusive, end_key_exclusive)`. `None` means unbounded on that side. Yields `(key, value)` pairs in ascending key order. Live iterator (v0): raises `DBConcurrentPageModificationError` on `next()` if the tree was mutated between iterator construction and the call. The iterator is a generator; it does not hold pages pinned between `next()` calls.

```python
db.checkpoint() -> None
```

Forces a checkpoint immediately. Used by tests and by users who want to tighten the recovery window before shutdown.

```python
db.close() -> None
```

Takes a final checkpoint, fsyncs both files, releases handles. After `close`, all methods raise `DBClosedError`.

```python
db.__enter__ / db.__exit__
```

Context-manager support. `__exit__` calls `close()`.

### 7.2 Exceptions

A flat hierarchy, all sharing the `DB` prefix:

```
DBError(Exception)
├── DBCorruptedError                          # bad CRC, bad magic, malformed page
│   └── DBChecksumError                       # specifically a CRC mismatch (raised by the codec)
├── DBClosedError                             # operation on a closed DB
├── DBConcurrentPageModificationError         # iterator outlived a mutation
└── DBRecordTooLargeError                     # key + value won't fit on a page
```

`TypeError` is reserved for "wrong Python type" usage errors (e.g. passing a `str` as a key). State-of-the-DB errors all flow through `DBError`.

### 7.3 Things deliberately omitted (YAGNI)

- No transactions, no `begin` / `commit` / `abort`.
- No batch operations (`put_many`, `delete_many`).
- No keys-only or values-only iteration variants.
- No prefix-scan helper. (Use `scan(prefix, prefix + b"\xff" * something)` or compute the lexicographic successor.)
- No statistics or introspection on the public API.
- No async; single-threaded blocking I/O only.
- No dict-sugar (`__getitem__`, `__setitem__`, `__contains__`).

### 7.4 Internal `_debug` namespace

Tests need to inspect the tree state to verify invariants. Rather than exposing this on `DB` directly, a separate `db._debug` object provides:

```
db._debug.tree_height()              -> int     # leaves at depth 0, root at depth N
db._debug.total_pages_in_file()      -> int     # data file size / page size
db._debug.dirty_pages_in_cache()     -> int     # pinned dirty pages currently in the buffer pool
db._debug.freelist_length()          -> int     # number of free page ids on the freelist
db._debug.iter_all_pages()           -> Iterator[Page]
db._debug.dump_tree()                -> str     # human-readable, for failure messages
db._debug.highest_lsn_issued()       -> int     # most recent LSN handed out by the WAL
```

The leading underscore signals "do not depend on this from outside the test suite."

### 7.5 Internal class APIs

This section enumerates the public surface (attributes, properties, and methods) of every internal class. Everything listed here is callable from another module within `bptreedb`; only the `DB` class is part of the *user*-facing API. Private implementation details are omitted. Types are given in Python-ish shorthand.

#### 7.5.1 `entities.py` — the data model

Plain dataclasses. No methods beyond what `@dataclass` generates, no validation logic, no I/O. Every field is mutable so the layers above can mutate instances in place.

```python
# WAL records.
@dataclass
class WALRecord:
    lsn: int

@dataclass
class WALPutRecord(WALRecord):
    key: bytes
    value: bytes

@dataclass
class WALDeleteRecord(WALRecord):
    key: bytes

@dataclass
class WALCheckpointRecord(WALRecord):    # earned in Iteration 5
    root_page_id: int
    freelist_head: int
    next_page_id: int

# Pages.
@dataclass
class MetaPage:
    magic: bytes                          # always b"BPTREEDB"
    version: int
    page_size_bytes: int
    root_page_id: int
    next_page_id: int
    last_checkpoint_lsn: int = 0          # earned in Iteration 5
    freelist_head: int = 0                # earned in Iteration 6

@dataclass
class LeafSlot:
    key: bytes
    value: bytes

@dataclass
class InternalSlot:
    key: bytes
    child_page_id: int

@dataclass
class LeafPage:
    last_modified_lsn: int                # earned in Iteration 4
    right_sibling_page_id: int                    # 0 if rightmost
    slots: list[LeafSlot]

@dataclass
class InternalPage:
    last_modified_lsn: int                # earned in Iteration 4
    leftmost_child_page_id: int                   # the "slot −1" pointer
    slots: list[InternalSlot]

@dataclass
class FreelistPage:                       # earned in Iteration 6
    last_modified_lsn: int
    next_freelist_page_id: int
    freed_page_ids: list[int]
```

The tree never reaches for `free_space_start`, `num_slots`, or `record_offset`. Those exist only at encode time.

#### 7.5.2 `codec.py` — the wire-format codec

All functions below are **pure**: they take and return only `bytes` / dataclasses, never touch the filesystem, and raise `DBChecksumError` (a subclass of `DBCorruptedError`) on CRC mismatch.

```python
# Low-level helpers.
class BufferReader:
    def __init__(data: bytes) -> None
    def read_struct(spec) -> tuple
    def read_bytes(length: int) -> bytes
    def read_length_prefixed_bytes() -> bytes

class BufferWriter:
    def write_struct(spec, *values) -> None
    def write_bytes(value) -> None
    def write_length_prefixed_bytes(value) -> None
    def crc32() -> int
    def build() -> bytes

def verify_crc32(data: bytes) -> None

# WAL records.
def encode_wal_record(record: WALRecord) -> bytes
def decode_wal_record(data: bytes) -> WALRecord
def decode_next_wal_record_from_file(fd: IO[bytes]) -> WALRecord   # raises EOFError at EOF

# Pages (earned in Iteration 3 onward).
def encode_meta_page(meta: MetaPage, page_size_bytes: int) -> bytes
def decode_meta_page(buf: bytes) -> MetaPage

def encode_page(page: LeafPage | InternalPage | FreelistPage, page_size_bytes: int) -> bytes
def decode_page(buf: bytes) -> LeafPage | InternalPage | FreelistPage
```

The page codec is the only place in the project where slotted-page math (slot offset arithmetic, `free_space_start` / `free_space_end`, compaction of deleted records) lives.

#### 7.5.3 `wal.py` — the `WAL` class

Owns a single append-mode file. Appends are fsynced before returning. Replay walks from byte 0, stops on the first bad record, and truncates the torn tail in place.

**Attributes / properties:**

| name             | type        | description                                                    |
|------------------|-------------|----------------------------------------------------------------|
| `path`           | `Path`      | Absolute path to the log file.                                 |
| `current_lsn`    | `int`       | LSN of the most recently appended record (0 if empty).         |
| `size_bytes`     | `int`       | Current on-disk size in bytes. (Earned in Iteration 5.)        |

**Methods:**

| signature                                                      | behaviour                                                                              |
|----------------------------------------------------------------|----------------------------------------------------------------------------------------|
| `open() -> None`                                               | Open or create the log file. Fsyncs the parent directory on first creation.           |
| `close() -> None`                                              | Fsync and close the underlying file.                                                   |
| `__enter__ / __exit__`                                         | Context-manager sugar around `open` / `close`.                                         |
| `append_put(key, value) -> int`                                | Encode a `WALPutRecord`, append, fsync, return its LSN.                                |
| `append_delete(key) -> int`                                    | Encode a `WALDeleteRecord`, append, fsync, return its LSN.                             |
| `append_checkpoint(root_page_id, freelist_head, next_page_id) -> int` | (Iteration 5.) Encode and append a `WALCheckpointRecord`.                       |
| `replay(callback) -> None`                                     | Walk every record from byte 0, validating CRCs and strictly increasing LSNs. Calls `callback(record)` for each good record. On the first bad record, truncate the file to the end of the last good record, fsync, and return. A torn tail is silent; a bad record followed by a good record raises `DBCorruptedError`. |
| `truncate_before(lsn) -> None`                                 | (Iteration 5.) Discard records with `lsn < given_lsn` via a rename-based file rotation that is crash-safe. |

#### 7.5.4 `pager.py` — the `Pager` class *(earned in Iteration 3)*

Owns the data file. One instance per open DB. Reads and writes happen one page at a time, at `page_id * page_size_bytes` offsets.

**Attributes / properties:**

| name             | type        | description                                                    |
|------------------|-------------|----------------------------------------------------------------|
| `path`           | `Path`      | Absolute path to the data file.                                |
| `page_size_bytes`| `int`       | Honoured from the on-disk meta page on reopen.                 |
| `num_pages`      | `int`       | Current page count, derived from `meta.next_page_id`.          |

**Methods:**

| signature                                              | behaviour                                                                  |
|--------------------------------------------------------|----------------------------------------------------------------------------|
| `open() -> None`                                       | Open or create `bptreedb.data`. On create, write a valid meta page and an initial empty leaf at page 1. On reopen, decode and verify the meta page; raise `DBCorruptedError` on CRC mismatch. |
| `close() -> None`                                      | Flush the meta page (if dirty), fsync, close.                              |
| `read_page(page_id) -> bytes`                          | Read a raw page-sized buffer. No decoding.                                 |
| `write_page(page_id, data) -> None`                    | Write a raw page-sized buffer. No fsync.                                   |
| `fsync() -> None`                                      | `os.fsync` the data file.                                                  |
| `get_meta() -> MetaPage`                               | Return the in-memory meta dataclass.                                       |
| `update_meta(**fields) -> None`                        | Mutate the in-memory meta and mark it dirty.                               |
| `flush_meta() -> None`                                 | Encode the in-memory meta via `codec.encode_meta_page` and write it to page 0. Does not fsync. |
| `allocate_page() -> int`                               | Pop a freed id from the freelist if non-empty; otherwise bump-allocate from `next_page_id`. Marks meta dirty. (The freelist branch is earned in Iteration 6.) |
| `free_page(page_id) -> None`                           | (Iteration 6.) Push `page_id` onto the head freelist page.                  |

#### 7.5.5 `cache.py` — the `BufferPool` class *(earned in Iteration 4)*

Wraps a `Pager`. Caches decoded `Page` dataclasses, not raw bytes. On a miss, calls `pager.read_page` and `codec.decode_page`; on `flush_all`, calls `codec.encode_page` and `pager.write_page`.

**Attributes / properties:**

| name                 | type   | description                                                    |
|----------------------|--------|----------------------------------------------------------------|
| `capacity_pages`     | `int`  | Maximum number of cached pages.                                |
| `dirty_count`        | `int`  | Number of dirty pages currently pinned in the cache.           |

**Methods:**

| signature                                              | behaviour                                                                  |
|--------------------------------------------------------|----------------------------------------------------------------------------|
| `get(page_id) -> LeafPage \| InternalPage \| FreelistPage` | Cache hit promotes to MRU. Miss reads via `pager.read_page`, decodes, inserts. Evicts the LRU **clean** page if at capacity. Raises `BufferPoolFull` if every page is pinned dirty. |
| `mark_dirty(page_id, lsn) -> None`                     | Set the page's `dirty` flag and update its `last_modified_lsn`.            |
| `flush_all() -> None`                                  | Encode and write back every dirty page via the Pager. Mark all cached pages clean. Does not fsync (the caller does). |
| `dirty_page_ids() -> list[int]`                        | For tests and for the auto-checkpoint trigger.                             |

#### 7.5.6 `tree.py` — the `BTree` class *(earned in Iteration 3)*

Stateless with respect to the current root: reads the root page id from the Pager's meta page at the start of every operation, and writes it back via `pager.update_meta(root_page_id=...)` when the root changes. Takes a `BufferPool` (from Iteration 4 onward; uses the `Pager` directly in Iteration 3) and asks it for pages by id.

**Methods:**

| signature                                              | behaviour                                                                  |
|--------------------------------------------------------|----------------------------------------------------------------------------|
| `search(key) -> bytes \| None`                         | Walk root → leaf. Return the value or `None`.                              |
| `insert(key, value, lsn) -> None`                      | Walk root → leaf, remembering the path. Insert in sorted position. Split leaves / internal nodes upward as needed. Every touched page is marked dirty with `lsn`. Raises `DBRecordTooLargeError` if the encoded record exceeds the page size limit. |
| `delete(key, lsn) -> bool`                             | Walk root → leaf. Remove the slot if present. Redistribute or merge if the leaf drops below the half-full threshold; propagate upward; collapse the root if it is left with a single child. Returns `True` if a key was removed. |
| `scan(start_key_inclusive, end_key_exclusive, version_callback) -> Iterator[tuple[bytes, bytes]]` | Find the leaf containing the start key, walk leaves via `right_sibling_page_id`, stop at the end bound. Calls `version_callback()` at the top of each iteration; that callback raises `DBConcurrentPageModificationError` if the tree was mutated. |

The tree does **not** own the version counter — it is owned by `DB`, and the tree is given a callback so that a concurrent-modification check fires at the top of every scan iteration.

#### 7.5.7 `db.py` — the `DB` class

Described in detail in Section 7.1. In terms of composition, a `DB` owns a `WAL`, a `Pager`, a `BufferPool`, and a `BTree`, plus the version counter, the checkpoint policy, and the `_debug` namespace. Its `put` / `delete` methods implement the strict discipline "log first, fsync, then mutate the tree."

---

## 8. Testing strategy

The test suite is the primary correctness mechanism. It is structured as four layers of increasing scope and decreasing speed.

### 8.1 Test categories

| Category    | Protects against                                   | Speed         |
|-------------|----------------------------------------------------|---------------|
| Unit        | Off-by-ones, encoding bugs, slot-array math        | Fast (ms)     |
| Integration | Layer interface mismatches                         | Medium        |
| Property: dict-equivalence | Tree algorithm bugs (lost keys, wrong scan results) | Medium |
| Property: invariants       | Half-full violations, depth imbalance, slot ordering | Medium |
| Crash: random              | WAL ordering bugs, recovery bugs, idempotency bugs   | Slow   |

### 8.2 Unit tests, per layer

- `test_page.py` — slotted page round-trip, slot insert at every position, slot delete at every position, page compaction after deletes, header CRC if any.
- `test_wal.py` — append, fsync, replay, torn-tail detection (with hand-crafted truncated files), `truncate_before`.
- `test_cache.py` — LRU eviction order, dirty pinning (cannot evict a dirty page), eviction triggers checkpoint when no clean pages are available.
- `test_pager.py` — meta page CRC verification, freelist push/pop including the freelist-page-full case, `allocate_page` from freelist vs. bump-allocate.
- `test_tree.py` — leaf split, internal split, leaf merge, internal merge, redistribute, root split, root collapse. Uses an in-memory fake Pager so no I/O is exercised.

### 8.3 Integration tests

- `test_basic.py` — happy paths for `put`, `get`, `delete`, `scan`.
- `test_persistence.py` — write some data, close, reopen, verify data is intact.
- `test_checkpoint.py` — checkpoint truncates the WAL; recovery after a checkpoint replays nothing.

### 8.4 Property-based tests (Hypothesis)

- `test_dict_equivalence.py` — generate a random sequence of `put` / `get` / `delete` / `scan` operations. Apply the same sequence to `bptreedb` and to a Python `dict` (with sorted iteration for `scan`). Assert the outputs match at every step.
- `test_invariants.py` — same generation, but after every operation call `assert_tree_invariants(db)` (see Section 8.6).

The property tests run with a small `page_size_bytes` (256 or 512) so that splits, merges, and freelist activity happen frequently in short sequences.

### 8.5 Crash tests

The most interesting test infrastructure in the project. Hypothesis generates a random sequence of operations. At a randomly chosen point during the sequence, the test triggers a simulated crash that "loses" all writes since the last fsync, then reopens the DB and verifies that exactly the operations acknowledged before the crash are present.

The crash is simulated by a `FaultyFile` wrapper:

```python
class FaultyFile:
    """A file wrapper that can simulate crashes by 'losing' unfsynced writes."""

    def __init__(self, path: Path):
        self._path = path
        self._real = open(path, "r+b")
        self._fsynced_snapshot: bytes = self._real.read()
        self._unfsynced = False

    def write_at(self, offset: int, data: bytes) -> None:
        self._real.seek(offset)
        self._real.write(data)
        self._unfsynced = True

    def fsync(self) -> None:
        self._real.flush()
        os.fsync(self._real.fileno())
        self._real.seek(0)
        self._fsynced_snapshot = self._real.read()
        self._unfsynced = False

    def crash(self) -> None:
        # Revert the on-disk file to the last-fsynced state.
        self._real.seek(0)
        self._real.truncate()
        self._real.write(self._fsynced_snapshot)
        self._real.flush()
        os.fsync(self._real.fileno())
```

The Pager and the WAL accept an optional `file_factory` so that tests can substitute `FaultyFile` for the real file. The DB is opened, operations are applied, and at any point the test calls `db._files.crash()` and re-opens the DB. Hypothesis searches the space of crash points and operation sequences for any violation of the durability contract.

### 8.6 The `assert_tree_invariants` helper

A single function that walks the entire tree and asserts:

1. All leaves are at the same depth.
2. Every non-root page is ≥ 40% full by record bytes.
3. Every page's slot array is sorted ascending by key.
4. For every internal node, separator keys correctly partition the children: `max(child[i]) < separator[i] ≤ min(child[i+1])`.
5. Leaf sibling pointers form a complete forward chain in key order, with no cycles.
6. The freelist contains no page that is reachable from the tree.
7. Every page reachable from the tree is within `next_page_id`.

This helper is called at the end of every property-test step. It is cheap relative to the rest of the test and devastating to bugs.

### 8.7 Test conventions

- Plain test functions, no test classes.
- GIVEN / WHEN / THEN comment structure where it adds clarity.
- `pytest.raises` for expected errors.
- Tools: `uv` for the project, `pytest` for the runner, `ruff` for lint, `ty` for typing.
- Tests run with `page_size_bytes=256` by default so that splits, merges, and freelist activity happen in short sequences. A separate "slow" pytest mark uses `page_size_bytes=4096`.

---

## 9. Project structure

```
bptreedb/
├── pyproject.toml
├── README.md                        # short, just "what is this"
├── src/
│   └── bptreedb/
│       ├── __init__.py              # re-exports DB and exceptions
│       ├── exceptions.py            # exception hierarchy
│       ├── entities.py              # data model: dataclasses (WAL records, pages, slots)
│       ├── codec.py                 # wire format: pure encode/decode functions
│       ├── fs.py                    # tiny filesystem helpers (fsync_file, fsync_directory)
│       ├── wal.py                   # WAL append/fsync/replay/truncate
│       ├── pager.py                 # file I/O, meta page, freelist
│       ├── cache.py                 # buffer pool (LRU + dirty tracking)
│       ├── tree.py                  # B+ tree algorithms
│       ├── db.py                    # public DB class
│       └── _debug.py                # introspection helpers
├── tests/
│   ├── unit/
│   │   ├── test_page.py
│   │   ├── test_wal.py
│   │   ├── test_cache.py
│   │   ├── test_pager.py
│   │   └── test_tree.py
│   ├── integration/
│   │   ├── test_basic.py
│   │   ├── test_persistence.py
│   │   └── test_checkpoint.py
│   ├── property/
│   │   ├── test_dict_equivalence.py
│   │   └── test_invariants.py
│   ├── crash/
│   │   ├── conftest.py              # FaultyFile fixture
│   │   └── test_recovery.py
│   └── conftest.py                  # shared fixtures (tmpdir DB, small page size)
└── docs/
    ├── design.md                    # this document
    └── production-readiness-gap-analysis.md  # the companion doc, written after v0
```

One module per layer, one test file per module.

---

## 10. Planned v1 work (out of scope for v0)

These are deliberately deferred. Each is large enough to be its own brainstorming session.

1. **Mutation-tolerant range cursor.** Replace the version-check iterator with a key-based cursor that re-traverses from the root on each `next()` using the last yielded key. Allows mutations during iteration without raising. The trade is O(log N) per step instead of O(1).
2. **Ping-pong meta pages.** Keep two meta pages and alternate between them on each checkpoint. On open, pick the one with the higher `last_checkpoint_lsn` whose CRC is valid. Survives torn writes to the meta page itself.
3. **Overflow pages for oversized records.** Allow keys + values larger than `page_size / 5` by spilling values across linked overflow pages.
4. **STEAL buffer-pool policy with UNDO logging.** Allow eviction of dirty pages between checkpoints. Requires writing UNDO records and a rollback mechanism. Unlocks much larger working sets but adds significant complexity.
5. **Transactions.** `db.begin() / commit() / abort()` with at least read-committed semantics.
6. **Concurrency.** Page-level latches plus a top-level reader-writer lock, or jump straight to MVCC.

---

## 11. Companion deliverable

A separate document, `production-readiness-gap-analysis.md`, will be written **after v0 ships** so that it can be grounded in the actual implementation and the questions that arose while writing it. Tentative outline:

1. Buffer-manager policies — NO-STEAL/FORCE-AT-CHECKPOINT (us) vs. STEAL/NO-FORCE (Postgres, InnoDB). Why STEAL needs UNDO, why NO-FORCE needs REDO.
2. WAL record format — logical (us) vs. physical (Postgres full-page images) vs. physiological (most real engines). Torn-page handling.
3. Meta page durability — single CRC'd meta page (us) vs. ping-pong dual meta pages (LMDB) vs. control file + relfilenodes (Postgres).
4. Concurrency — single-threaded (us) vs. shared/exclusive page latches + lock manager (Postgres) vs. MVCC snapshots (Postgres, MySQL InnoDB, SQLite WAL mode).
5. Free space management — singly-linked freelist (us) vs. FSM trees (Postgres) vs. bitmap pages (InnoDB).
6. Variable-length records — slotted pages (us, and most real engines) vs. fixed-size pages with overflow.
7. Recovery — replay-from-last-checkpoint (us) vs. ARIES three-pass analysis/redo/undo.
8. Transactions — none (us) vs. begin/commit/abort with isolation levels.
9. Indexes — KV store only (us) vs. multiple secondary indexes pointing to a primary key.
10. Crash testing in the wild — Jepsen, ALICE, fault injection.
11. Glossary.

**Writing constraint:** every term of art (REDO, UNDO, STEAL, FORCE, ARIES, MVCC, latch, lock manager, etc.) is defined inline at first use, and the glossary at the end serves as a quick reference. The intended reader is "a curious developer who has never opened a database textbook."

---

## 12. Glossary (used in this document)

- **B+ tree.** A self-balancing search tree where all values live in the leaves and internal nodes only hold separator keys + child pointers. Leaves are linked for fast in-order iteration.
- **Buffer pool / page cache.** A fixed-size in-memory pool of pages, with eviction. Synonyms.
- **Checkpoint.** A point in time at which all dirty pages are flushed to the data file and the WAL can be truncated.
- **Dirty page.** A page that has been modified in the buffer pool but not yet written to the data file.
- **fsync.** A system call that forces buffered file writes to durable storage. Until you fsync, the OS may have your data in volatile memory only.
- **LSN (Log Sequence Number).** A monotonically increasing integer identifying a position in the WAL.
- **NO-STEAL.** A buffer-pool policy that forbids writing dirty pages to the data file outside of a checkpoint.
- **FORCE-AT-CHECKPOINT.** A buffer-pool policy that flushes all dirty pages at every checkpoint.
- **Slotted page.** A page layout with a header, a slot array of pointers growing from one end, and variable-length records growing from the other.
- **Torn write.** A partial write to disk caused by a crash or power loss in the middle of a write operation.
- **WAL (Write-Ahead Log).** A separate, append-only file that records every change before it is applied to the data file. Enables crash recovery.
