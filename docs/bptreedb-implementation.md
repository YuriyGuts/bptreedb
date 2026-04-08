# bptreedb Implementation Plan

> **Audience:** A human implementer learning how databases work, who wants every piece of machinery to earn its place. No literal source code in this document — descriptions and behaviours only. The full architecture lives in the spec at `docs/superpowers/specs/2026-04-07-bptreedb-design.md`; this plan is the *journey* to that architecture.

**Goal:** Build a single-process, crash-safe B+ tree key-value store in Python by walking from "the simplest thing that satisfies the public API" to "the architecture in the spec," motivating every layer and every on-disk field at the moment it earns its existence.

**Tech stack:** Python 3.12+, `uv`, `pytest`, `hypothesis`, `ruff`, `ty`.

**Working assumption:** the project lives in a fresh directory (e.g., `bptreedb/`) which is a git repository. All file paths in this plan are relative to that directory.

## How to read this plan

The plan is organised into seven **iterations**. Each iteration is a milestone where you have *working software you can use*. The next iteration always exists because the current one has a flaw you can feel.

Each iteration is structured the same way:

- **Goal** — one sentence.
- **Why now** — the pain from the previous iteration that this one fixes.
- **What you build** — a narrative walkthrough of the design and the decisions, with each new field, method, or concept justified at the moment it's introduced.
- **Tests that earn the milestone** — the behaviours that must be true for you to call this iteration done.
- **What you have at the end** — the user-visible state after the iteration.
- **The flaw you'll feel next** — the bridge to the next iteration.

There are **no 30-minute task slices**. Each iteration is a coherent multi-day chunk. You decide how to subdivide it inside your editor.

## What gets thrown away

The only code that gets *replaced* in this plan is the **in-memory dict** that backs Iterations 1 and 2. It's deleted in Iteration 3 when the on-disk B+ tree takes over.

Everything else evolves **additively**: parameters, error classes, methods, on-disk fields, and modules are introduced in the iteration where they earn their place. Earlier iterations have a strict subset of the final API. Tests written in earlier iterations keep passing through later iterations because their test bodies use a `db` pytest fixture that hides the constructor signature evolution from them.

The one place this isn't strictly additive is **layout-checking tests** in `test_page.py` and `test_pager.py`: when a header gains a field, the tests asserting "byte at offset N is field X" have to be updated.

Throwaway count: **one Python module** (`_inmem.py`), introduced as a deliberate stepping-stone so you have something to put behind the API while you build the WAL.

## Earn-your-place principle

Every parameter, every exception class, every method, every field exists in the iteration that *needs* it, not before. The iteration where each piece earns its place:

| Concept | Earned in |
|---|---|
| `DB()` constructor + context manager (no args) | Iter 1 |
| `put` / `get` / `delete` / `scan` / `close` / context manager | Iter 1 |
| `DBClosedError` | Iter 1 |
| Type checks (`bytes` only) → built-in `TypeError` | Iter 1 |
| `dir_path` parameter | Iter 2 |
| `DBCorruptError` | Iter 2 |
| `page_size_bytes` parameter | Iter 3 |
| `DBRecordTooLargeError` and the size limit | Iter 3 |
| `DBConcurrentModificationError` and `_version_counter` | Iter 3 |
| `last_modified_lsn` page-header field | Iter 4 |
| `cache_capacity_pages` parameter | Iter 4 |
| `checkpoint()` method, `last_checkpoint_lsn` meta field | Iter 5 |
| `checkpoint_wal_size_bytes`, `checkpoint_dirty_page_ratio` | Iter 5 |
| `freelist_head` meta field, freelist page type | Iter 6 |

If you ever find yourself adding something the current iteration doesn't justify, stop and ask "what would feel wrong without it?" If the answer is "nothing yet," defer it.

---

## Iteration 1 — In-memory dict, full public API

### Goal

Get the public API right. Have a working `DB` you can play with in a REPL and a passing test suite, with zero persistence.

### Why now

Because the API is the contract. Every later iteration is a *re-implementation* underneath the same API; if you don't lock down the API first, every later refactor will tempt you to bend it. By spending the first iteration just on the API and its tests — backed by something dead simple — you give yourself a fixed target to reimplement against.

### What you build

A package skeleton with `uv`, `ruff`, `ty`, `pytest`. The `src/bptreedb/` layout with these modules:

- `errors.py` — `DBError(Exception)` as the base class, plus one subclass: `DBClosedError`. Re-export from `__init__.py`. (`DBConcurrentModificationError` is deferred to Iteration 3, where the on-disk tree's lazy iterator makes mid-scan mutation actually unsafe.)
- `db.py` — the `DB` class.
- `_inmem.py` — a tiny in-memory backing store. Use `sortedcontainers.SortedDict` (add `sortedcontainers` to dependencies) or a plain `dict` with sorted iteration. The whole module is maybe 30 lines.

The `DB` class:

- `DB()` — no arguments. Constructs a fresh `DB` instance. Used as a context manager: `with DB() as db: ...`. Entering the context performs the open work; exiting calls `close()`. Manual `db.open()` and `db.close()` methods are also exposed for callers who don't want to use a `with`-statement.
- `put(key, value)` — raises `TypeError` if either argument is not `bytes`. Otherwise inserts into the in-memory dict.
- `get(key)` — looks up in the in-memory dict; returns the value or `None`.
- `delete(key)` — removes from the in-memory dict; returns `True` if it was present, `False` otherwise.
- `scan(start_key_inclusive, end_key_exclusive)` — returns a generator yielding `(key, value)` pairs in sorted order over the half-open range. Both bounds are required (no defaults) — callers must be explicit about the scan range, and pass `None` for an unbounded side. **The behaviour of mutating the DB while a scan is in flight is undefined in this iteration; it will become a `DBConcurrentModificationError` in Iteration 3.** Document this in the method's docstring so callers don't accidentally rely on the underlying `SortedDict`'s behaviour.
- `close()` — sets a `_closed` flag; subsequent calls to any other public method raise `DBClosedError`.
- `__enter__` / `__exit__` — context manager support; `__exit__` calls `close()`.

### Tests that earn the milestone

All tests live under `tests/`. You will keep adding to these as you go; nothing in this iteration's test suite gets deleted later.

**The `db` fixture in `tests/conftest.py`:**

A pytest fixture that yields a fresh, open DB. Its body is `with DB() as db: yield db`. Test bodies use this fixture instead of constructing `DB` directly, so that they don't have to know how a `DB` is opened.

**Unit tests for the API surface (`tests/unit/test_api.py`):**

Test bodies use the `db` fixture. None of the tests touch the constructor directly:

- `put` followed by `get` returns the value.
- `put` overwrites: re-putting the same key with a new value, then `get`, returns the new value.
- `delete` of a present key returns `True`; subsequent `get` returns `None`.
- `delete` of an absent key returns `False`.
- `put` with a non-`bytes` key raises `TypeError`.
- `put` with a non-`bytes` value raises `TypeError`.
- `scan` over an empty DB yields nothing.
- `scan` with no bounds yields all `(key, value)` pairs in ascending key order.
- `scan` with `start_key_inclusive` skips keys strictly less than the bound.
- `scan` with `end_key_exclusive` skips keys greater than or equal to the bound.
- `scan` with both bounds yields only the half-open range.
- After `close`, every public method raises `DBClosedError`.
- Context manager: a small handful of tests construct `DB()` directly (without the fixture) to verify `__enter__`/`__exit__` behaviour. Keep them isolated; they're the only tests that touch the constructor directly.

**Property test (`tests/property/test_dict_equivalence.py`):**

A Hypothesis test that generates a sequence of operations from the strategy `put | get | delete | scan` and applies them to both your `DB` (via the `db` fixture) and a Python `dict` (with sorted iteration for `scan`), asserting they agree at every step. Use small random `bytes` (1–8 bytes from a 4-character alphabet) so collisions are common. This test will also pass through every later iteration.

Use `@settings(max_examples=200, deadline=None)` because Hypothesis defaults are too short once we're doing real I/O.

### What you have at the end

A working in-process key-value store you can `pip install -e .` and use from a REPL:

```
from bptreedb import DB
with DB() as db:
    db.put(b"a", b"1")
    db.put(b"b", b"2")
    list(db.scan())   # → [(b"a", b"1"), (b"b", b"2")]
```

It is **fast and convenient and forgets everything when you close it.** Suggested commit: `feat: in-memory KV store with public API`.

### The flaw you'll feel next

Restart the REPL and open a fresh `DB`. Your data is gone. **Every put has to survive a process restart**, and the in-memory dict by definition can't promise that.

---

## Iteration 2 — Write-Ahead Log

### Goal

Make every acknowledged `put` and `delete` survive a crash. The dataset still lives in memory; only the *changelog* is on disk.

### Why now

Because in Iteration 1 you have no durability at all. Before you build any complicated on-disk index, the simplest thing that fixes durability is: write down what you did. The structured way to write down what you did is a log. By introducing the log first — when there's no other on-disk file and no other layer to interact with — you can focus entirely on getting the *log itself* right: framing, CRCs, fsync, torn-tail recovery. This is exactly the WAL from the spec, except its role at this point is "the only persistent thing" rather than "one of two files."

### What you build

**The constructor signature changes for the first time.** `DB()` becomes `DB(dir_path)` — `dir_path` is required because the API now has a place to persist things to. Update the `db` fixture in `tests/conftest.py` to take `tmp_path` and call `DB(tmp_path)`. Update the two or three tests that constructed `DB()` directly. Every other test body is unchanged.

**A new exception is earned.** `DBCorruptError` is added to `errors.py` and re-exported from `__init__.py`. It is raised when a WAL CRC check fails *somewhere other than the torn tail* — i.e., a record fails its CRC check but is followed by a record that passes. A torn tail at the end of the file is silently truncated (the user was never told about anything beyond the last fsync); a CRC failure followed by valid records is genuine corruption and we raise.

We deliberately do **not** police the contents of `dir_path`. If the directory contains files we don't recognise, that's the user's business — foreign files don't corrupt the DB, and refusing to open in their presence would just be officious. The DB only owns the files it created.

A new module:

- `wal.py` — the WAL writer and replayer. Constructor takes `dir_path` and creates `<dir_path>/wal.log` if it doesn't exist. (We'll create the directory itself here too — Iteration 1 never had to.)

A WAL record is framed as:

```
4 bytes  record_length        uint32, length of everything after this field
8 bytes  lsn                  uint64
1 byte   op_type              0x01 PUT, 0x02 DELETE
... payload ...
4 bytes  crc32                CRC of record_length + lsn + op_type + payload
```

PUT payload: `key_length(4) | key | value_length(4) | value`
DELETE payload: `key_length(4) | key`

Note we are **not** introducing the CHECKPOINT op type yet. There is no checkpoint. The WAL is the database.

The `WAL` class exposes:

- `append_put(key, value) -> int` — encodes, writes, returns the LSN it used.
- `append_delete(key) -> int` — same for delete.
- `fsync() -> None` — flushes the file and calls `os.fsync` on the descriptor.
- `replay() -> Iterator[Record]` — walks the file from the start, validating each record's CRC and verifying that LSNs are strictly increasing. Stops at the first record that fails any check (the **torn tail**) and truncates the file to the end of the last good record.

Why each piece exists, justified now:

- **`record_length`** — so the replayer knows how many bytes to read for each record without parsing the payload.
- **`lsn`** — a monotonically increasing identifier so the replayer can detect out-of-order or missing records (which would mean torn writes or filesystem misbehaviour). It will become the load-bearing timestamp in later iterations; for now it's just a sanity check.
- **`crc32`** — so the replayer can detect a record that was partially written (or otherwise corrupted) before its `fsync` completed. Without the CRC, you cannot distinguish a half-written torn record at the end of the log from a complete one.
- **`fsync`** — without it, "the OS has buffered your write" is *not* the same as "your write is on disk." A power loss or kernel panic between `write` and `fsync` loses everything in the OS buffer. Calling `fsync` before returning from `put` is the *only* thing that lets you tell the user "your write is durable."

In `db.py`, modify the `DB` open path, `DB.put`, and `DB.delete`:

- `DB(dir_path)` stores the path; the actual open work happens in `__enter__` (or in an explicit `db.open()` method that `__enter__` delegates to). Opening creates the directory if it doesn't exist. Then instantiates a `WAL` and immediately calls `wal.replay()` on it, applying each record to the in-memory dict. The dict is now seeded with the prior history. (No foreign-file check — see the note above on why we don't police directory contents.)
- `put` calls `wal.append_put(key, value)` and `wal.fsync()` *before* it touches the in-memory dict. This is the discipline of write-ahead logging: you log the change before you make the change.
- `delete` does the symmetric thing.
- `close` calls `wal.fsync()` once more (in case anything was buffered) and closes the WAL file.

Also, **the `_file_factory` test hook**. Add a private `_file_factory` parameter to `WAL.__init__` (and thread it through `DB.__init__` as a private parameter `_file_factory`, undocumented in the public API). It defaults to `open(...)`. Tests will use it to inject a `FaultyFile`. Yes, this is a private wart on the public class — that's the price of property-based crash testing without monkey-patching.

The `FaultyFile` test fixture lives in `tests/conftest.py` (or `tests/crash/conftest.py`):

- Wraps a real file. Every `write` is recorded in an "unfsynced" buffer in addition to going to the file.
- `fsync` clears the unfsynced buffer and snapshots the file's current contents.
- `crash()` reverts the file on disk to the last snapshot.

The fixture exposes a `file_factory` callable that produces `FaultyFile` instances and a `crash_all(...)` helper that crashes every file in the DB.

### Tests that earn the milestone

All Iteration 1 tests still pass — they should, because the public API is unchanged.

**New unit tests for the WAL (`tests/unit/test_wal.py`):**

- Round-trip encoding for both record types.
- Appending three records produces sequential LSNs.
- `replay` of an empty file yields nothing.
- `replay` of three good records yields all three in order.
- `replay` of a hand-truncated file yields only the records before the truncation point and physically truncates the file to that point.
- `replay` of a file with a hand-corrupted CRC stops at the corrupted record and truncates.
- `replay` of a file with an out-of-order LSN stops at the bad record and truncates.
- After `replay`, new appends use `last_good_lsn + 1`.

**New integration tests for persistence (`tests/integration/test_persistence.py`):**

- Open, put 10 keys, close, reopen, get them all.
- Open, put, close, reopen, delete some, close, reopen, scan: the deleted ones are gone.
- Open with mismatched `page_size_bytes` (currently still ignored): silently uses whatever the existing DB had.

**New crash tests (`tests/crash/test_recovery.py`):**

- Hand-built sequence: open with `FaultyFile`, put two keys, crash, reopen, both keys present.
- Hand-built sequence: put a key, do *not* fsync (use a private hook or temporarily skip the fsync — actually no, the WAL fsyncs internally on every put, so this case is automatically tested by the next one).
- Hand-built sequence: put a key, modify the WAL file directly to truncate the last byte of the record, reopen, the partial record is dropped and the key is gone.
- A Hypothesis property test that:
  - Generates a list of operations.
  - Opens a DB with `FaultyFile`.
  - Applies the operations one by one, recording each *acknowledged* `put` or `delete` in an `expected` dict.
  - At a randomly chosen step, calls `crash_all()` and reopens the DB.
  - Asserts that the reopened DB matches `expected` exactly.

This crash-property test is the heart of the test suite. It will run unchanged through every later iteration and catch any future durability bug.

### What you have at the end

A key-value store that **survives `kill -9` and reboots** as long as the data fits in RAM. You can put data, kill the process, restart, and read it back. Every record you put is durably on disk before `put` returns. Suggested commit: `feat(wal): durable changelog with crash recovery`.

### The flaw you'll feel next

Two flaws, actually, and they are linked:

1. **The dataset is bounded by RAM.** The in-memory dict holds everything. If you put a million keys, all million live in memory.
2. **WAL replay scales linearly with history.** Every time you open the DB, you replay the *entire* WAL from the start of time. An hour of writes turns into an hour of opens.

Both flaws have the same root cause: the WAL is the *only* place the data lives on disk. Every new piece of data appends to the log, and nothing ever ages out. The fix to flaw 1 is "store the data in a structure on disk that doesn't have to fit in RAM." The fix to flaw 2 will fall out naturally once that structure exists. Both flaws disappear in Iteration 3.

---

## Iteration 3 — The on-disk B+ tree

### Goal

Replace the in-memory dict with a paged file containing a B+ tree. The dataset is no longer bounded by RAM. The WAL stays exactly as it is — its records still describe logical operations and its replay still rebuilds the dataset from the log — but now "the dataset" lives in a file, and replay rebuilds *it* instead of the in-memory dict.

### Why now

This is the iteration where the database stops being a dict-with-a-log and starts being a database. It's the largest and most interesting iteration. You will build the slotted page format, the pager, and the tree algorithms; you will throw away `_inmem.py`; and at the end every existing test will still pass *unchanged*, because the public API is the contract.

**Three new things on the API surface earn their place here:**

1. **`page_size_bytes` parameter on the `DB` constructor.** Now that the database has pages, the page size is a real configurable. Default 4096; honoured only on creation (an existing DB uses its stored page size). Update the `db` fixture in `tests/conftest.py` to pass `page_size_bytes=256` so that splits and merges happen often in tests.
2. **`DBRecordTooLargeError`.** Now that there's a maximum record size (`(page_size_bytes - HEADER_SIZE) // 4 - SLOT_SIZE`), `put` enforces it. The error class is added to `errors.py` and re-exported.
3. **`DBConcurrentModificationError` and `_version_counter`.** Now that `scan` returns a *real* lazy iterator that walks leaf pages via `right_sibling`, mutating the tree mid-scan is genuinely unsafe — a `put` between two `next()` calls can split the leaf the iterator is sitting on, move records, and rewrite sibling pointers. The iterator's "I am at slot N of leaf page P" state stops being meaningful. The version counter is the cheap, honest fix: the iterator snapshots a counter at construction and re-checks it on every `next()`. The error class is added to `errors.py` and re-exported.

   Implementation details:

   - `DB.__init__` adds `self._version_counter = 0`.
   - `DB.put` bumps the counter at the end of every successful insert.
   - `DB.delete` bumps the counter only when the key was actually present (return value `True`). A failed `delete` is not a mutation and must not invalidate live iterators.
   - `DB.get` does NOT bump the counter — search is not a mutation.
   - `DB.scan` snapshots `self._version_counter` at the top, then checks it at the top of each loop iteration before yielding. The check belongs at the top of the loop body, which is the code that runs *after* the consumer's `next()` resumes the generator and *before* the next value is produced.
   - The check fires on the *first* `next()` too, so a mutation between iterator construction and the first `next()` raises.
   - `_replay_wal` must NOT go through `put` / `delete` (which would bump the counter). Apply records to the tree directly inside the replay loop and leave the counter alone.

I am going to walk you through this iteration in three sub-steps. Each sub-step is its own pass through the code with its own commit. The order matters: the page codec is purely functional and easy to test, the pager builds on it, and the tree builds on the pager. **Within each sub-step, I will introduce only the on-disk fields you need at this iteration.** Fields like `last_modified_lsn`, `last_checkpoint_lsn`, and `freelist_head` will *not* exist after this iteration — they get added in Iterations 4, 5, and 6, when the iterations themselves explain why they have to.

### Sub-step 3a: The slotted page codec

A new module: `page.py`. A new test file: `tests/unit/test_page.py`.

A page is a fixed-size `bytes` (or `bytearray`) buffer. Every page starts with a header. Internal nodes and leaves use the same header layout, distinguished by a type tag. The layout is:

| offset | size | field              | why it exists at this iteration                                              |
|-------:|-----:|--------------------|------------------------------------------------------------------------------|
|      0 |    1 | `type_tag`         | Internal vs leaf nodes have different record formats; the parser must know which it's looking at. |
|      1 |    3 | reserved           | Alignment padding so the next field starts at a 4-byte boundary.             |
|      4 |    4 | `num_slots`        | The slot directory grows downward as records are added; you need to know its current length. |
|      8 |    4 | `free_space_start` | The end of the slot directory; an insert needs to know where the next slot pointer goes. |
|     12 |    4 | `free_space_end`   | The start of the records region; an insert needs to know where the next record bytes go. |
|     16 |    8 | `right_sibling`    | Leaves only: `scan` walks the leaf chain forward via this pointer. Internal nodes overload this slot to hold their leftmost child pointer (so the slot directory only contains "key + child" pairs and doesn't need a special "slot −1"). |

Total: 24 bytes. **Yes, 24, not 32.** You don't have `last_modified_lsn` yet — there's no buffer pool to need it. The header will gain bytes in Iteration 4. Today it's 24.

After the header, the slot directory grows downward (each slot is `record_offset(4) + record_length(4) = 8` bytes). After the slot directory, free space. From the end of the page, records grow upward.

**Leaf record:** `key_length(4) | key | value_length(4) | value`.
**Internal record:** `key_length(4) | key | child_page_id(8)`.

The maximum usable record size is `(page_size_bytes - 24) // 4 - 8`. Compute it as a function so the value isn't magic; you'll use it both in `put`'s size check and in split decisions.

The `SlottedPage` class wraps a `bytearray` and exposes:

- Construction of an empty page given `(page_size_bytes, type_tag)`.
- `from_bytes(buf)` and `to_bytes()` for round-tripping.
- `num_slots`, `free_bytes`, `total_record_bytes` as read-only properties.
- `insert_slot(slot_index, payload)`, `get_slot_payload(slot_index)`, `delete_slot(slot_index)` operating on opaque payload bytes.
- `compact()` — repacks live records contiguously to reclaim garbage left by deleted slots. The codec needs this because deletes leave holes in the records region; compaction is called when an insert can't find a contiguous run despite enough total free bytes.
- `insert_leaf(slot_index, key, value)` and `get_leaf(slot_index)` as the leaf-typed convenience layer.
- `insert_internal(slot_index, key, child_page_id)` and `get_internal(slot_index)` as the internal-typed convenience layer.
- `find_slot_for_key(key) -> (insertion_index, exact_match)` — binary search.
- `iter_keys()`, `iter_leaf_items()`, `iter_internal_items()`.

**Tests for `test_page.py`:**

- Header round-trip.
- Empty page invariants (`num_slots == 0`, `free_bytes == page_size_bytes - 24`).
- Insert at index 0, end, middle; verify all surviving payloads readable in order.
- Out-of-space insert raises and leaves the page unchanged.
- Delete shifts slot pointers and reduces `num_slots`.
- After several deletes, `compact()` reclaims the garbage and `to_bytes`/`from_bytes` round-trips.
- Leaf and internal record round-trip helpers.
- Binary search returns the correct `(insertion_index, exact_match)` for keys present, between, before all, after all.
- Inserting in random order via `find_slot_for_key` produces a sorted page.

Suggested commit: `feat(page): slotted page codec with leaf and internal records`.

### Sub-step 3b: The pager

A new module: `pager.py`. A new test file: `tests/unit/test_pager.py`.

The pager owns the data file `data.db` inside `dir_path`. It is the only thing that touches the file. (The WAL, from Iteration 2, is still its own thing, owning `wal.log` independently.)

The data file is `N * page_size_bytes` bytes. Page 0 is the **meta page** with this layout:

| offset | size | field            | why it exists at this iteration                                                       |
|-------:|-----:|------------------|---------------------------------------------------------------------------------------|
|      0 |    8 | `magic`          | Identify the file format on open. Reject foreign files immediately.                   |
|      8 |    4 | `version`        | Permit format evolution.                                                              |
|     12 |    4 | `page_size_bytes`| Make the file self-describing — you don't have to be told the page size to open it.    |
|     16 |    8 | `root_page_id`   | The root of the tree. Meta page is the entry point to the tree structure.             |
|     24 |    8 | `next_page_id`   | The next page id to bump-allocate. (No freelist yet — that comes in Iteration 6.)     |
|     32 |    4 | `crc32`          | Detect torn writes to the meta page. A bad meta page is unrecoverable from `data.db` alone, so you raise. |

Total: 36 bytes. **No `last_checkpoint_lsn` yet** — there is no checkpoint. **No `freelist_head` yet** — there is no freelist.

The `Pager` class:

- Constructor: `Pager(dir_path, *, page_size_bytes=None, _file_factory=None)`. If `data.db` doesn't exist, create it with one page (the meta page) describing an empty tree where `root_page_id` points to a freshly initialised leaf at page 1; bump-allocate that leaf and write it. If the file exists, decode and verify the meta page.
- `read_page(page_id) -> bytes` — seek + read.
- `write_page(page_id, data) -> None` — seek + write. No fsync.
- `fsync() -> None` — `os.fsync` on the data file.
- `get_meta() -> MetaPage` — return the in-memory meta.
- `update_meta(**fields) -> None` — modify the in-memory meta. Does NOT write it to disk yet (the meta page is rewritten only on `flush_meta`).
- `flush_meta() -> None` — re-encode the meta with a fresh CRC and write it to page 0 via `write_page`.
- `allocate_page() -> int` — bump `meta.next_page_id` and return the prior value. Mark the meta dirty in memory. **No freelist consultation today.**
- `close() -> None`.

Two important calls about behaviour at this iteration:

1. **There is no buffer pool yet.** The pager reads and writes pages directly from disk on every call. This will be slow; it's fine for now. The buffer pool exists to fix this in Iteration 4, and you will be able to motivate it because you will have *felt the slowness*.
2. **`flush_meta` is called by `close`** for now. Iteration 5 introduces checkpoints, which will become the place that calls `flush_meta` periodically. Today, the only time the meta page hits disk is at close.

**Tests for `test_pager.py`:**

- Create new database in a fresh directory: succeeds, creates `data.db`, page 0 decodes to a valid meta with the requested `page_size_bytes` and `root_page_id == 1`.
- Reopen existing database: succeeds, decodes existing meta.
- Reopen with conflicting `page_size_bytes` is silently ignored (the file's value wins).
- Tampered meta page CRC raises `DBCorruptError`.
- `write_page` then `read_page` round-trips.
- `update_meta` then `get_meta` reflects the change without touching disk.
- `flush_meta` then reopen returns the updated meta.
- `allocate_page` returns sequential ids and grows the file by one page each time.

Suggested commit: `feat(pager): paged data file with meta page and bump-allocation`.

### Sub-step 3c: The B+ tree

A new module: `tree.py`. A new test file: `tests/unit/test_tree.py`. Add invariant-checking helpers to a new `_debug.py`.

The `BTree` class is given a pager and a way to read/write the root pointer (`get_root() / set_root()`). It does not know about WAL or recovery. Its only job is the algorithms.

I am not going to repeat the algorithms in detail — they are in spec §5 with full pseudocode, and you should refer to that section as you implement. The plan here is the *order in which to bring the tree to life*.

1. **`search(key)` first.** Hand-build a tiny tree of two leaves under one internal node directly via the pager (write the pages by hand into the file using the slotted-page codec from sub-step 3a). Test that `search` returns the right value for keys in each leaf, for the leaf boundaries, and for absent keys. This earns you confidence in the tree-walking and binary-search code without ever touching insert.

2. **`insert(key, value)` for the case where the leaf has room.** Walk to the leaf, find the sorted position, insert the slot, mark dirty (today "mark dirty" just means "you've already written it via the pager; nothing to remember"). Test by inserting many keys into a small page and `search`-ing each.

3. **Leaf split.** When a leaf is full, allocate a new leaf, move the upper half by *byte size* (not by count — slot directories with variable-length records must split by bytes), splice the new leaf into the right-sibling chain, compute the promoted key as the smallest key now in the new leaf. If the leaf was the root, allocate a new internal root with two children and one separator; update `root_page_id` via `set_root`.

4. **Internal split propagation.** When the parent is full, split it the same way, except the *median* separator is removed from both halves and *promoted* upward (this is the asymmetry between leaf and internal splits — leaf splits *copy* the smallest right key up; internal splits *move* the median up). Recurse all the way to the root.

5. **`scan(start_key_inclusive, end_key_exclusive)`.** Find the leaf containing the start key; walk forward via `right_sibling`; stop at the end. Capture and check the version counter from `DB`.

6. **`delete(key)` without rebalance.** Find the slot, remove it. Don't worry about the half-full invariant yet.

7. **Same-parent merge and redistribute.** When a leaf drops below 40% by record bytes, find its same-parent left and right siblings via the parent's child list (**not** via `right_sibling`, which can cross parent boundaries; merging requires same-parent siblings). Redistribute if a sibling has spare bytes; merge otherwise. Free the merged-out page. The parent may now be under-full — recurse symmetrically.

8. **Internal merge propagation and root collapse.** Symmetric to insert's split propagation. Internal merges concatenate the two children plus a separator pulled down from the parent. If the root is left with one child, collapse: set `root_page_id` to that child.

The half-full threshold is a configurable constant in the tree module; default 0.4 (40% of page bytes).

The **invariant helper** in `_debug.py`:

`assert_tree_invariants(tree)` walks the entire tree and asserts the invariants from spec §8.6. You will use this from every property test, and from `test_tree.py` after every operation. When an invariant fails, raise an assertion error that includes a `dump_tree(tree)` text rendering so the failure message is debuggable.

**Tests for `test_tree.py`:**

For brevity I'll group these — refer to spec §5 for the algorithm details and to spec §8.6 for the invariants.

- Hand-built two-level tree: `search` returns correct values for keys in both leaves and edge cases.
- Single-leaf insert (no split): all keys readable.
- Insert until split: tree has two leaves with correct distribution and a new root; sibling chain is correct.
- Insert until two levels of internal nodes: invariants hold throughout (mark slow).
- Internal split moves the median: the median key does NOT appear in either of the two resulting internal nodes after the split, only in the new parent.
- `scan` over a multi-leaf tree returns all keys in order.
- `scan` with bounds returns the correct half-open range.
- Delete present and absent keys.
- Delete enough to force a redistribution: both leaves end up ≥ 40% full and the parent's separator is updated.
- Delete enough to force a merge: the freed page id is recorded somewhere (today it leaks — we'll add the freelist in Iteration 6).
- Delete enough to force the root to collapse.
- Insert/delete fuzzing: 1000 random ops, `assert_tree_invariants` after every op.

**Wire it into `DB`:**

Now in `db.py`:

- Delete `_inmem.py`. (This is the only throwaway in the entire project.)
- Construct a `Pager` and a `BTree` in the `DB` open path (after the `WAL`).
- After the `WAL.replay()` call, the records are now applied to the *tree* instead of the in-memory dict. Each PUT in the WAL becomes `tree.insert(key, value)`; each DELETE becomes `tree.delete(key)`. The tree starts empty (the meta page's `root_page_id` points to an empty leaf created by the pager constructor).
- `put` still appends to the WAL and fsyncs, then calls `tree.insert`. Same pattern as before, just with a different backing structure.
- `delete` likewise.
- `get` becomes `tree.search`.
- `scan` becomes `tree.scan`.
- `close` calls `pager.flush_meta()`, `pager.fsync()`, `wal.fsync()`, `wal.close()`, `pager.close()`. (Note: `flush_meta` is called manually here today; in Iteration 5 the checkpoint procedure takes over.)

### Tests that earn the milestone

- All Iteration 1 unit tests still pass.
- All Iteration 1 property tests still pass — they should, because they only check the public API contract.
- All Iteration 2 persistence and crash tests still pass — also unchanged. The WAL is the same, the recovery procedure is the same, only what gets replayed into has changed.
- All new `test_page.py`, `test_pager.py`, `test_tree.py` tests pass.
- `assert_tree_invariants` passes after every operation in the property test that uses it.

**New unit tests for the version counter** (added to `tests/unit/test_db.py`):

- `scan` followed by `put` raises `DBConcurrentModificationError` on the next `next()`.
- `scan` followed by a *successful* `delete` raises likewise.
- `scan` followed by a `delete` of an absent key (returns `False`) does NOT raise — failed deletes are not mutations.
- `scan` followed by `get` does NOT raise — search is not a mutation.
- Partial-iteration test: consume the first item from a multi-item scan, then `put`, then assert the next `next()` raises. This proves the check runs on every step, not just the first.
- Mutation between iterator construction and the first `next()` also raises.
- After a failed mutation (e.g., `put` with a non-`bytes` argument that raises `TypeError`), a previously constructed scan still works — the counter must not bump on the failure path.

If any Iteration 1 or 2 test breaks, you have changed the contract — go fix the implementation, not the test.

### What you have at the end

A real database. You can put a million keys; they live on disk; you can search, scan, and delete them; you can kill the process at any time and reopen and find them all. The dataset is no longer bounded by RAM. This is the milestone where `bptreedb` becomes itself.

Suggested commit (or more likely, three commits, one per sub-step): `feat(tree): on-disk B+ tree replaces in-memory backing store`.

### The flaw you'll feel next

Two related flaws:

1. **Every operation reads pages from disk.** Inserting 100 keys does ~100 disk reads of the same root page. There is no caching at all. Your benchmarks (informal: time a `for _ in range(100000): db.put(...)` loop) will be embarrassing. You can feel this immediately.
2. **The WAL still grows forever**, and recovery still replays it from the start of time. Iteration 3 didn't fix this — it just made the dataset durable in a more interesting place. Replaying a million-record WAL into an on-disk tree is slower, not faster, than replaying it into a dict.

Iteration 4 fixes flaw 1 (the buffer pool). Iteration 5 fixes flaw 2 (checkpoints). They are introduced in this order because the buffer pool's existence motivates several of the rules that the checkpoint procedure depends on.

---

## Iteration 4 — The buffer pool

### Goal

Cache pages in memory between operations so that repeated accesses to the same page do not hit the disk every time. Establish the discipline that makes the buffer pool safe in the presence of crashes.

### Why now

Because Iteration 3 left you with a database where every read and every write goes to disk immediately. That's correct but slow. The fix is a cache of pages in memory: a *buffer pool*. The buffer pool also turns out to be the structural prerequisite for the checkpoint procedure in Iteration 5, because checkpoints need a clear notion of "dirty pages" and "when does a dirty page get to disk." So introducing the buffer pool now both fixes the speed problem and sets up the next iteration.

### What you build

**A new constructor parameter earns its place:** `cache_capacity_pages` on `DB`, default 256. This is the capacity of the buffer pool that exists for the first time in this iteration.

A new module: `cache.py`. A new test file: `tests/unit/test_cache.py`. Modifications to `pager.py` (or the tree's pager interface) so that all page reads and writes go through the cache.

The `BufferPool` class:

- Constructor: `BufferPool(pager, capacity_pages)`.
- Internally an `OrderedDict[page_id, CachedPage]` where each `CachedPage` carries the page bytes, a `dirty: bool`, and (newly!) a `last_modified_lsn: int`.
- `get(page_id) -> bytes` — cache hit moves the page to MRU and returns its buffer; cache miss reads from the pager, stores it in the cache, evicts the LRU clean page if at capacity. **Dirty pages are pinned and cannot be evicted.** If the cache is at capacity and every page is dirty, raise a sentinel `BufferPoolFull` (the DB layer will catch it in Iteration 5 and trigger a checkpoint; for now, just propagate it as an error — you won't hit it in tests yet).
- `mark_dirty(page_id, lsn) -> None` — sets `dirty = True` and `last_modified_lsn = max(current, lsn)`.
- `flush_all() -> None` — writes every dirty page back to the pager via `pager.write_page` and marks them clean. Used by `close` (and, in Iteration 5, by checkpoint).
- `dirty_count()`, `dirty_page_ids()` for testability and for the auto-checkpoint trigger in Iteration 5.

**Now is the moment to introduce `last_modified_lsn`.** Add it to the page header. The header grows from 24 bytes to 32. Update `page.py` accordingly:

- Header layout becomes the spec layout from §4.2.2 with `last_modified_lsn` at offset 16. The `right_sibling` field that was at offset 16 moves to offset 24. Total header size: 32.
- Update `max_record_payload_size` to account for the new header size.

You also have to update the *codec* tests in `test_page.py` because the header size changed. This is the only place in the project where an existing test gets edited (rather than added to or left alone). The tests are checking the literal layout, so they have to follow it.

Why `last_modified_lsn` exists, justified now: because dirty pages may be evicted or flushed at times the tree no longer controls. The LSN field ties each page to the WAL record that last touched it, so that the buffer pool / checkpoint logic can enforce the rule "no page hits disk until the WAL record that protects it is fsynced." That rule is the WAL ordering rule. In this iteration, the rule is automatically satisfied because:

- Every `put` calls `wal.append + wal.fsync` *before* `tree.insert` is called.
- Therefore by the time the tree calls `mark_dirty(page_id, lsn)`, the WAL record at `lsn` has already been fsynced.
- Therefore even if the buffer pool turns around and writes the dirty page to disk immediately, the WAL is ahead of it.

So the LSN field doesn't *do* anything you can observe in Iteration 4 — it's bookkeeping. It earns its place in Iteration 5 (as the checkpoint cursor) and would earn its place in a hypothetical Iteration 7 that switches to a STEAL policy.

In the tree (`tree.py`), modify every place that previously called `pager.read_page` to call `buffer_pool.get` instead, and every place that mutated a page to call `buffer_pool.mark_dirty(page_id, current_lsn)` after the mutation. The current LSN is passed in by `DB.put` and `DB.delete` (which get it from `wal.append_*`) and threaded down to the tree.

In `db.py`:

- Construct a `BufferPool` wrapping the pager in the `DB` open path.
- The tree is given the buffer pool, not the pager directly. (The pager is still used by the buffer pool internally.)
- `close` calls `buffer_pool.flush_all()` before `pager.fsync()`, so all dirty pages are persisted on shutdown. (`pager.flush_meta` and `pager.close` come after.)
- `_debug` namespace (in `_debug.py`) gains `dirty_pages_in_cache()` and `total_pages_in_file()` for tests.

### Tests that earn the milestone

- All Iteration 1, 2, 3 tests still pass.
- New unit tests for the buffer pool (`test_cache.py`):
  - Cache hit: `get` followed by `get` of the same id calls the pager exactly once.
  - LRU order: with capacity 3, accessing pages [1,2,3,4] evicts page 1.
  - Touch promotes to MRU: with capacity 3, sequence [1,2,3,1,4] evicts page 2.
  - Dirty pages are pinned: at capacity, dirty pages [1,2,3], `get` page 4 raises `BufferPoolFull`.
  - `mark_dirty` updates `last_modified_lsn` to the highest LSN passed.
  - `flush_all` writes every dirty page to the pager exactly once and clears the dirty bits.
  - Eviction never writes to the pager (only `flush_all` does).
- New informal benchmark in `tests/integration/test_perf_smoke.py` (not a strict assertion, just a sanity check that puts are now meaningfully faster than in Iteration 3): time a `for _ in range(10000): db.put(...)` loop. It should take seconds, not minutes.

### What you have at the end

The same database as Iteration 3, but **fast enough to actually use**. Repeated reads of the same page hit memory; the slow Iteration-3 benchmark goes from "minutes" to "seconds." Suggested commit: `feat(cache): LRU buffer pool with dirty pinning and last_modified_lsn`.

### The flaw you'll feel next

The WAL still grows forever. You haven't fixed that yet — you've only fixed the slowness. Open a long-running DB, watch `wal.log` grow, restart the DB, watch the open take longer and longer as the WAL replay does more work. The fix is checkpoints.

---

## Iteration 5 — Checkpoints

### Goal

Bound the size of the WAL and the time of recovery. After a checkpoint, the data file is consistent up to the checkpoint LSN, and the WAL only needs to retain records *after* that point.

### Why now

Because Iteration 4 left you with an unbounded WAL. Recovery scales linearly with the entire history of the database. The fix is to periodically declare "everything before this point is durably reflected in the data file; throw away the WAL records before it." That's a checkpoint. The buffer pool from Iteration 4 makes this implementable, because you now have a clear notion of "dirty pages" and "flush all dirty pages."

### What you build

**Three things on the API surface earn their place here:**

1. **`db.checkpoint()` method.** Public method on `DB` that forces a checkpoint immediately. Used by tests and by users who want to tighten the recovery window before shutdown.
2. **`checkpoint_wal_size_bytes` parameter** on `DB`, default 4 MiB. Auto-checkpoint trigger: when the WAL grows past this size, the next put/delete triggers a checkpoint.
3. **`checkpoint_dirty_page_ratio` parameter** on `DB`, default 0.5. Auto-checkpoint trigger: when the fraction of dirty pages in the buffer pool exceeds this ratio, the next put/delete triggers a checkpoint.

In `wal.py`:

- A new op type `OP_CHECKPOINT = 0x03` with payload `root_page_id(8) | freelist_head(8) | next_page_id(8)`. (Yes, the payload includes `freelist_head` even though the freelist doesn't exist yet — set it to 0 for now and Iteration 6 will populate it without needing to change the payload format. This is one of the few places I'm letting future-proofing leak in, because adding a field to a WAL record format later means a version bump. Acceptable.)
- `append_checkpoint(root_page_id, freelist_head, next_page_id) -> int`.
- `truncate_before(lsn)` — discards records with `lsn < given_lsn`. Implementation: write a new file `wal.log.new`, copy any records ≥ `lsn` (usually just the CHECKPOINT marker), `os.fsync` the new file, `os.replace` over `wal.log`, `os.fsync` the directory. Reopen the underlying file handle.
- `size_bytes` (property) — current on-disk size of the WAL.

**Add `last_checkpoint_lsn` to the meta page.** This is the iteration that earns it. Insert the new field after `next_page_id`; the CRC moves to make room. Total meta page header grows from 36 bytes (Iteration 3) to 44 bytes. Update `pager.py` and `test_pager.py`. (The final layout in spec §4.2.1 also contains `freelist_head`, which doesn't exist yet — Iteration 6 will add it. Until then, you may diverge from the spec's exact offsets; the spec is the destination, not a step-by-step contract.)

Why `last_checkpoint_lsn` exists, justified now: because recovery needs to know which WAL records are already reflected in the data file (and must be skipped) and which are not (and must be replayed). The meta page is the place to record this because it is the entry point for `open()`.

In `db.py`, add `db.checkpoint()` per spec §6.4 exactly:

1. Note `wal.next_lsn` as `ckpt_lsn`.
2. `buffer_pool.flush_all()`.
3. `pager.fsync()`.
4. `wal.append_checkpoint(...)`. `wal.fsync()`.
5. `pager.update_meta(last_checkpoint_lsn=ckpt_lsn, root_page_id=..., next_page_id=...)`. `pager.flush_meta()`. `pager.fsync()`.
6. `wal.truncate_before(ckpt_lsn)`.

Walk through the crash window analysis from spec §6.5 carefully — the order of these steps matters. If you crash between any two of them, the next `open()` must produce a consistent database. The plan does not repeat the analysis; refer to spec §6.5.

Also implement the **automatic checkpoint triggers** from spec §6.1:

- After every `put` and `delete`, check whether `wal.size_bytes > checkpoint_wal_size_bytes` OR `buffer_pool.dirty_count() / cache_capacity_pages > checkpoint_dirty_page_ratio`. If so, call `checkpoint()`.
- When `tree.insert` raises `BufferPoolFull`, call `checkpoint()` and retry once.
- When `db.close()` is called.

And implement the **recovery procedure** from spec §6.3 in the `DB` open path (i.e., `__enter__` / `db.open()`):

1. The pager has already validated the meta page in its constructor. (If the CRC fails, `DBCorruptError` is raised there.)
2. Construct the tree from `meta.root_page_id`. Construct the WAL.
3. Call `wal.replay()` and walk every record (torn-tail truncation already happens here from Iteration 2).
4. Skip records with `lsn ≤ meta.last_checkpoint_lsn`.
5. For each remaining PUT or DELETE, call `tree.insert` or `tree.delete`. CHECKPOINT records are skipped.
6. **Disable buffer pool eviction during recovery** by passing a flag (or temporarily setting capacity to infinity), so the pool can grow unbounded during replay without raising `BufferPoolFull`.
7. After replay, restore the cap and immediately call `checkpoint()` to persist the recovered state.

### Tests that earn the milestone

- All previous tests still pass.
- `test_wal.py` gains tests for the new CHECKPOINT op type and for `truncate_before`:
  - Encode/decode CHECKPOINT round-trip.
  - `truncate_before(lsn)` removes all earlier records and preserves all later ones.
  - Replay after truncation yields only the preserved records.
  - Crash safety of the rotation: hand-craft a `wal.log` and a stale `wal.log.new`; opening the WAL ignores or removes the stale `.new` and leaves `wal.log` intact.
- `test_pager.py` updates for the new `last_checkpoint_lsn` field and the larger meta page.
- New `tests/integration/test_checkpoint.py`:
  - Manual `checkpoint()` truncates the WAL: after, `wal.size_bytes` is small (just the CHECKPOINT marker) and `replay()` yields only that marker.
  - Auto-checkpoint by WAL size: with a small `checkpoint_wal_size_bytes`, after enough puts the WAL eventually shrinks (sample `wal.size_bytes` periodically).
  - Auto-checkpoint by dirty ratio: similar with a small ratio.
  - Checkpoint persists tree state: after a checkpoint, `pager.get_meta().root_page_id` matches the in-memory root, and reopening reads the same root.
- New `tests/integration/test_recovery.py`:
  - Open, put 5, close, reopen — recovery is a no-op (the close did the checkpoint).
  - Open, put 5, do not close, reopen — recovery replays 5 records.
  - Open, put 5, checkpoint, put 5 more, do not close, reopen — recovery replays 5 records (the second batch); the first batch came from the data file.
  - Open, put 5, delete 2, do not close, reopen — only 3 keys present.
- The crash recovery property test from Iteration 2 still passes, but now exercises a real on-disk tree with a real checkpoint policy. **This is the test that proves the whole stack works.** If it ever fails, treat it as a critical bug — it's the durability contract.

### What you have at the end

A database where the WAL stays bounded, recovery is fast (proportional to the WAL since the last checkpoint, not the entire history), and durability is preserved through crashes at any point in the checkpoint procedure. Suggested commit: `feat(db): checkpoint procedure and bounded recovery`.

### The flaw you'll feel next

Delete a few thousand keys from a populated database and watch `data.db` *not shrink*. Pages that the tree has merged out of existence are leaked — they remain in the file forever, taking up space, never reused. The fix is a freelist.

---

## Iteration 6 — Freelist

### Goal

Reuse pages that have been freed by tree merges, instead of leaking them. Bound the on-disk file size to roughly the working-set size.

### Why now

Because Iteration 5 left you with a file that grows monotonically — there is no way for the tree to give back a page. Every leaf merge frees a page id that nobody will ever pop. After enough churn, the file is mostly garbage. The fix is a freelist of free page ids that the pager pops from before bump-allocating a new page.

### What you build

**Add `freelist_head` to the meta page.** Append it after `last_checkpoint_lsn`; the CRC moves to make room. The meta page header grows from 44 bytes (Iteration 5) to 52 bytes. The set of fields now matches spec §4.2.1, though the on-disk order may differ from the spec's exact offsets — that's fine; the spec was written assuming all fields exist at once, and the implementation order chose to append rather than insert. If you'd rather have the on-disk layout match the spec exactly, you can rearrange fields in the encoder at this iteration; it's a one-time cost and the round-trip tests will tell you if you got it wrong.

Add a third page type tag, `TYPE_FREELIST = 0x03`, in `page.py`. A freelist page is *not* a slotted page — it has a simpler layout:

- The standard page header (so the type tag is in the same place and the page is parseable by generic code).
- 8 bytes: `next_freelist_page_id` (forming a singly linked list of freelist pages).
- An array of 8-byte freed page ids, with `num_slots` (from the standard header) repurposed to mean "number of freed page ids stored on this page."

Helper functions to encode/decode a freelist page; max capacity per freelist page = `(page_size_bytes - HEADER_SIZE - 8) // 8`.

In `pager.py`, modify `allocate_page` and add `free_page`:

- `allocate_page() -> int`:
  - If `meta.freelist_head != 0`, read the head freelist page, pop one id from its array, write the page back. If the head becomes empty, advance `meta.freelist_head` to its `next_freelist_page_id` and free the now-empty freelist page (push it to the new head — but **bump-allocate any new freelist pages directly**, never via the freelist itself, to break the circular dependency).
  - Otherwise bump-allocate from `meta.next_page_id` and grow the data file by one page.
  - In either case, mark the meta dirty.
- `free_page(page_id)`:
  - Read the head freelist page (or, if `freelist_head == 0`, bump-allocate a new freelist page and set it as the head).
  - If the head has room, append the page id and write it back.
  - If the head is full, bump-allocate a new freelist page, link it to the old head (`new.next = freelist_head`), set `meta.freelist_head = new_id`, and append the freed id to the new head.
  - Mark the meta dirty.

The "always bump-allocate freelist pages" rule is the load-bearing simplification. Without it, the freelist allocation logic becomes recursive in unpleasant ways. Document it inline as a comment.

In the tree, the existing `delete` code already had a "free this page" point (the merge case). Until now that point was a TODO; now it calls `pager.free_page(merged_out_page_id)`.

In the WAL CHECKPOINT record, populate the `freelist_head` field with the current value (instead of the 0 placeholder from Iteration 5). The recovery procedure already reads it from the meta page, so no recovery changes are needed.

### Tests that earn the milestone

- All previous tests still pass.
- `test_pager.py` gains:
  - Bump-allocate from an empty freelist returns sequential ids.
  - Free then allocate returns the freed id (LIFO behaviour).
  - Free many pages (more than fit in one freelist page) and then allocate them all back; total bytes accounted for.
  - Freelist persists across reopen (free, `flush_meta`, close, reopen, allocate).
  - When the freelist needs a new freelist page (because the head fills up), the new freelist page id is bump-allocated, not recycled.
- `test_tree.py` gains:
  - After enough deletes to force a merge, the freed page id appears in the freelist (use `_debug.freelist_length()`).
  - Insert, delete to force merges, insert again — the new inserts reuse the freed page ids before bump-allocating.
- The crash recovery property test from Iteration 2/5 continues to pass with the freelist active. (No new property test is needed — the existing one already exercises delete-heavy workloads under crash.)

### What you have at the end

A database that reuses space. Insert a million keys, delete most of them, insert another million — the file size stays roughly bounded by the working set, not the total history of allocations. You have arrived at the architecture in the spec.

Suggested commit: `feat(pager): freelist for page reuse`.

### The flaw you'll feel next

None. You have built `bptreedb`. The remaining work is documentation and reflection.

---

## Iteration 7 — Reconcile the gap-analysis doc with what you actually built

### Goal

The production-readiness gap analysis already exists at `docs/superpowers/specs/2026-04-07-bptreedb-production-readiness-gap-analysis.md`. It was written upfront, from the spec, before any code existed. After finishing Iteration 6, re-read it from the perspective of someone who has just spent weeks implementing the thing, and patch any sections where the reality differs from what the spec assumed.

### Why now

Because the gap analysis was written from a *spec*, not from a running implementation. The spec is detailed enough to be mostly right, but writing real code surfaces details that weren't in the spec — slightly different field choices, subtle invariants you discovered, simplifications you stumbled into, complications you didn't anticipate. Now that the implementation exists, the doc can be calibrated against it.

### What you build

A revision pass of the gap-analysis document. Concretely:

1. Read the doc front to back as a critical reader.
2. For each section, ask: "is this still accurate? Did I actually do what the spec said? Did I learn something while coding that should be in here?"
3. Patch any drift between the spec-as-written and the code-as-built. If a simplification was bigger than the doc claimed, sharpen it. If you discovered an unexpected complication, add it.
4. Add any new "what it would take to bridge the gap" insights you have from having actually written the code. The bridge sections were written somewhat speculatively from the spec; you may now have a sharper sense of how invasive each bridge would be.
5. Optionally, add a short "what I learned" appendix listing the things that surprised you most during the build. This is for you, not for the future reader, but it's the kind of thing that's invaluable to look back at.

### Tests that earn the milestone

A human reader (you, a friend, or future-you a month from now) can read both the spec and the gap-analysis doc and:

- Understand the architecture without reading any code.
- Answer "why does PostgreSQL use STEAL while we use NO-STEAL?"
- Answer "why does Postgres write full-page images into the WAL while we don't?"
- Answer "what does ARIES recovery do that ours doesn't?"
- Answer "what problem does MVCC solve that single-threaded blocking doesn't have?"
- Find no statement in the doc that the implementation contradicts.

### What you have at the end

A finished educational repository. A working B+ tree database. A spec describing the architecture. A plan describing the journey. A gap-analysis doc that has been calibrated against the actual implementation. Suggested commit: `docs: reconcile gap analysis with implementation`.

---

## Coverage check

Quick map of spec sections to iterations, so you can verify that nothing in the spec is unimplemented at the end:

| Spec section                       | Iteration where it earns its place                  |
|------------------------------------|-----------------------------------------------------|
| §3 Architecture (six layers)       | Spread across 1–6, in motivated order               |
| §4.1 Directory layout              | Iter 2 (`wal.log`), Iter 3 (`data.db`)              |
| §4.2.1 Meta page                   | Iter 3 (initial), Iter 5 (`last_checkpoint_lsn`), Iter 6 (`freelist_head`) |
| §4.2.2 Slotted page layout         | Iter 3 (initial 24-byte header), Iter 4 (32-byte with `last_modified_lsn`) |
| §4.2.3 Freelist page               | Iter 6                                              |
| §4.3 Size limits                   | Iter 3                                              |
| §4.4 WAL framing                   | Iter 2 (PUT/DELETE), Iter 5 (CHECKPOINT)            |
| §5.1 Tree invariants               | Iter 3 (`assert_tree_invariants`)                   |
| §5.2 search                        | Iter 3                                              |
| §5.3 insert + splits               | Iter 3                                              |
| §5.4 delete + merge                | Iter 3 (algorithm), Iter 6 (`free_page` integration) |
| §5.5 scan with version check       | Iter 1 (version counter), Iter 3 (across leaves)    |
| §5.6 What gets logged              | Iter 2                                              |
| §5.7 Determinism                   | Discipline; verified by the crash property test from Iter 2 onward |
| §6.1 Buffer-pool policy            | Iter 4                                              |
| §6.2 LSN bookkeeping               | Iter 4                                              |
| §6.3 Recovery                      | Iter 2 (initial WAL replay), Iter 5 (with checkpoint cursor) |
| §6.4 Checkpoint                    | Iter 5                                              |
| §6.5 Crash-window analysis         | Iter 5 (validated by crash property test)           |
| §7.1 DB API (no-arg constructor + put/get/delete/scan/close/CM) | Iter 1                |
| §7.1 `dir_path` parameter          | Iter 2                                              |
| §7.1 `page_size_bytes` parameter   | Iter 3                                              |
| §7.1 `cache_capacity_pages` parameter | Iter 4                                           |
| §7.1 `checkpoint()` + checkpoint parameters | Iter 5                                     |
| §7.2 `DBClosedError`               | Iter 1                                              |
| §7.2 `DBCorruptError`              | Iter 2                                              |
| §7.2 `DBRecordTooLargeError`       | Iter 3                                              |
| §7.2 `DBConcurrentModificationError` | Iter 3 (deferred from Iter 1 — earned only once `scan` is a real lazy iterator) |
| §7.4 `_debug` namespace            | Iter 3 onward, growing as needed                    |
| §8.1–8.4 Test categories           | Iter 1 (unit, property), Iter 2 (crash), all later iterations add more |
| §8.5 `FaultyFile` infrastructure   | Iter 2                                              |
| §8.6 `assert_tree_invariants`      | Iter 3                                              |
| §8.7 Test conventions              | Iter 1                                              |
| §9 Project structure               | Cumulative                                          |
| §10 Planned v1 work                | Out of scope (deferred)                             |
| §11 Companion doc outline          | Iter 7                                              |
