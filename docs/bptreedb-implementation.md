# bptreedb Implementation Plan

> **Audience:** A human implementer learning how databases work, who wants every piece of machinery to earn its place. **This document is designed to be self-contained.** You should be able to read it top-to-bottom and implement each iteration without flipping over to the design spec. The design spec (`docs/bptreedb-design.md`) exists as a reference for architectural *justification* — why the policy is NO-STEAL, why recovery is idempotent, why the half-full rule is 40% — but the plan below spells out every on-disk layout, every class API, and every algorithm you need to type into the editor.

**Goal:** Build a single-process, crash-safe B+ tree key-value store in Python by walking from "the simplest thing that satisfies the public API" to "a real database," motivating every module and every on-disk field at the moment it earns its existence.

**Tech stack:** Python 3.12+, `uv`, `pytest`, `hypothesis`, `ruff`, `ty`.

**Working assumption:** the project lives in a fresh directory (e.g., `bptreedb/`) which is a git repository. All file paths in this plan are relative to that directory.

## The core decomposition (the thing that stays stable across every iteration)

Before the iterations themselves, understand the decomposition you'll maintain throughout. Every iteration fits into this shape:

- **`entities.py` — the data model.** Plain, mutable Python dataclasses. One class per persistent entity: WAL records, pages, meta page. Zero I/O, zero serialization, zero validation. This is the shared vocabulary all other modules use. New iterations grow this file by adding dataclasses or fields to existing dataclasses.
- **`codec.py` — the wire format.** Pure functions over `bytes` and dataclasses: `encode_wal_record`, `decode_wal_record`, `encode_page`, `decode_page`, `encode_meta_page`, `decode_meta_page`. Raises `DBChecksumError` on CRC failure. Never touches a file descriptor. This is where slotted-page arithmetic and struct packing live, and nothing else.
- **`fs.py` — filesystem helpers.** Tiny module with `fsync_file(fd)` and `fsync_directory(path)`. Exists from iteration 2.
- **Stateful services.** `wal.py`, `pager.py`, `cache.py`, `tree.py`, `db.py`. Each owns some state and delegates byte-level work to `codec.py` and file-level work to `fs.py`. Each iteration adds at most one new service module.
- **`exceptions.py` — the error hierarchy.** Grows by one or two classes per iteration as new failure modes earn their place.

**Why this matters for reading the plan.** Every iteration from 2 onward follows the same rhythm: "grow the data model, grow the codec, grow or add a service, wire it into `DB`." If you ever find yourself wanting a class that both mutates a `bytearray` in place *and* speaks on-disk format *and* is the thing the tree operates on — stop. That's three roles in one class. Split them into a dataclass, a codec function, and a service.

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

The only code that gets *replaced* in this plan is the **in-memory `SortedDict`** that backs iterations 1 and 2 as the implementation of `DB.get` / `DB.put` / `DB.delete`. It is deleted in iteration 3 when the on-disk B+ tree takes over. Every other module evolves **additively**: parameters, error classes, methods, on-disk fields, and fields on dataclasses are introduced in the iteration where they earn their place.

Tests written in earlier iterations keep passing through later iterations because their test bodies use a `db` pytest fixture that hides the constructor signature evolution. The only tests that ever get *edited* (rather than added to) are layout-checking tests that assert "byte at offset N is field X" — when a header gains a field, those assertions must follow it.

## Earn-your-place principle

Every parameter, every exception class, every method, every field exists in the iteration that *needs* it, not before. The iteration where each piece earns its place:

| Concept | Earned in |
|---|---|
| `DB()` constructor + context manager | Iter 1 |
| `put` / `get` / `delete` / `scan` / `close` | Iter 1 |
| `DBClosedError` | Iter 1 |
| Type checks (`bytes` only) → built-in `TypeError` | Iter 1 |
| `data_dir` constructor parameter | Iter 2 |
| `entities.py` (WAL record dataclasses) | Iter 2 |
| `codec.py` (WAL record encode/decode) | Iter 2 |
| `wal.py` + `fs.py` | Iter 2 |
| `DBCorruptedError`, `DBChecksumError` | Iter 2 |
| Page dataclasses in `entities.py` | Iter 3 |
| Page codec functions in `codec.py` | Iter 3 |
| `pager.py`, `tree.py` | Iter 3 |
| `page_size_bytes` constructor parameter | Iter 3 |
| `DBRecordTooLargeError` and the size limit | Iter 3 |
| `DBConcurrentPageModificationError` and the version counter | Iter 3 |
| `last_modified_lsn` field on page dataclasses | Iter 4 |
| `cache.py`, `cache_capacity_pages` constructor parameter | Iter 4 |
| `checkpoint()` method, `last_checkpoint_lsn` on `MetaPage` | Iter 5 |
| `WALCheckpointRecord` | Iter 5 |
| `checkpoint_wal_size_bytes`, `checkpoint_dirty_page_ratio` | Iter 5 |
| `FreelistPage` dataclass, `freelist_head` on `MetaPage` | Iter 6 |

If you ever find yourself adding something the current iteration doesn't justify, stop and ask "what would feel wrong without it?" If the answer is "nothing yet," defer it.

---

## Iteration 1 — In-memory dict, full public API

### Goal

Get the public API right. Have a working `DB` you can play with in a REPL and a passing test suite, with zero persistence.

### Why now

Because the API is the contract. Every later iteration is a *re-implementation* underneath the same API; if you don't lock down the API first, every later refactor will tempt you to bend it. By spending the first iteration just on the API and its tests — backed by something dead simple — you give yourself a fixed target to reimplement against.

### What you build

A package skeleton with `uv`, `ruff`, `ty`, `pytest`, `sortedcontainers`, and `hypothesis`. The `src/bptreedb/` layout with these modules:

- `exceptions.py` — the error hierarchy. Only two classes this iteration:

  ```
  DBError(Exception)
  └── DBClosedError(DBError)
  ```

  Re-export both from `__init__.py`.

- `db.py` — the `DB` class, which internally stores key/value pairs in a `SortedDict` (from `sortedcontainers`). No separate backing-store module is needed — a `SortedDict` attribute is fine, and you'll rip it out wholesale in iteration 3 anyway.

**`DB` class, iteration 1 surface.**

| name | kind | signature | behaviour |
|---|---|---|---|
| `__init__` | method | `DB() -> None` | No arguments. Initialize an internal `SortedDict`, an `is_opened = False` flag. |
| `open` | method | `open() -> None` | Flip `is_opened = True`. Idempotent: calling twice is fine. Exists so callers who don't use `with` can still use the DB. |
| `close` | method | `close() -> None` | Clear the `SortedDict`, flip `is_opened = False`. |
| `__enter__` / `__exit__` | methods | context manager | `__enter__` calls `open()` and returns `self`; `__exit__` calls `close()` and does not suppress exceptions. |
| `put` | method | `put(key: bytes, value: bytes) -> None` | Raises `DBClosedError` if not opened. Raises `TypeError` if either argument is not `bytes`. Otherwise inserts into the `SortedDict`. |
| `get` | method | `get(key: bytes) -> bytes \| None` | Raises `DBClosedError` if not opened. Raises `TypeError` if `key` is not `bytes`. Returns the value or `None`. |
| `delete` | method | `delete(key: bytes) -> bool` | Raises `DBClosedError` if not opened. Raises `TypeError` if `key` is not `bytes`. Returns `True` if a key was removed, `False` if it was already absent. |
| `scan` | method | `scan(start_key_inclusive: bytes \| None, end_key_exclusive: bytes \| None) -> Iterator[tuple[bytes, bytes]]` | Raises `DBClosedError` / `TypeError` eagerly (validate *before* entering the generator body, otherwise bad arguments wouldn't surface until the first `next()`). Yields `(key, value)` pairs in ascending key order over the half-open range `[start, end)`. Both bounds are required arguments; pass `None` for an unbounded side. |

**Implementation note on eager validation in `scan`.** A function containing a `yield` becomes a generator, which means its body doesn't run until `next()` is first called. If you want `scan` to raise immediately on a closed DB or a non-`bytes` argument, split it into a public `scan` method that does the checks and then `return self._scan(...)`, and a private `_scan` generator method that does the yielding.

**Mutation during scan is undefined in iteration 1.** Document this in the docstring. Iteration 3 will turn it into a real `DBConcurrentPageModificationError` once `scan` becomes a lazy iterator over actual on-disk pages.

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

**Four new modules appear, in this dependency order:** `fs.py` (filesystem helpers), `entities.py` (data model dataclasses), `codec.py` (wire-format encode/decode), `wal.py` (the stateful WAL service). The `DB` constructor changes for the first time.

**Two new exceptions are earned.** Add to `exceptions.py`:

```
DBError(Exception)
├── DBClosedError
└── DBCorruptedError
    └── DBChecksumError(expected: int, actual: int)
```

`DBCorruptedError` is raised when the WAL contains *genuine* corruption — e.g., a CRC failure followed by one or more valid records. `DBChecksumError` is raised by the codec on any CRC mismatch and is caught and re-raised (or swallowed, in the torn-tail case) by the WAL service. A torn tail at the very end of the log is silently truncated; the user was never told about anything beyond the last fsync.

**We deliberately do not police the contents of the data directory.** If the directory contains files we don't recognise, that's the user's business. The DB only owns the files it created.

#### `fs.py`

Two tiny helpers:

```python
def fsync_file(fd: IO) -> None
def fsync_directory(path: str | os.PathLike) -> None
```

`fsync_directory` should silently no-op on platforms that don't support it (Windows, some network filesystems).

#### `entities.py` — the data model for WAL records

Plain dataclasses. No methods:

```python
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
```

These are the *only* structured values that cross the WAL / DB boundary. The DB hands a `WALPutRecord` to the WAL on `put`; the WAL hands `WALRecord` subclasses back to the DB during `replay`.

#### `codec.py` — the wire-format codec

A WAL record on disk is framed as:

```
4 bytes  record_length       uint32 (length of everything after this field, including the CRC)
8 bytes  lsn                 uint64
1 byte   op_type             uint8 (0x01 PUT, 0x02 DELETE)
... op-specific payload ...
4 bytes  crc32               uint32 (over record_length + lsn + op_type + payload)
```

- PUT payload: `key_length(4) | key | value_length(4) | value`
- DELETE payload: `key_length(4) | key`

All integers are little-endian unsigned. (We are **not** introducing the CHECKPOINT op type yet; there is no checkpoint.)

Why each piece exists:

- **`record_length`** — so the replayer knows how many bytes to read without parsing the payload.
- **`lsn`** — a monotonically increasing identifier so the replayer can detect out-of-order records. It becomes the load-bearing timestamp in later iterations; today it's a sanity check.
- **`crc32`** — so the replayer can detect a torn record at the end of the log. Without the CRC, a half-written record is indistinguishable from a complete one.

Introduce small reader/writer helpers so the codec stays legible:

```python
class BufferReader:
    def __init__(data: bytes) -> None
    def read_struct(spec: str | Struct) -> tuple
    def read_bytes(length: int) -> bytes
    def read_length_prefixed_bytes() -> bytes

class BufferWriter:
    def write_struct(spec: str | Struct, *values) -> None
    def write_bytes(value: bytes) -> None
    def write_length_prefixed_bytes(value: bytes) -> None
    def crc32() -> int
    def build() -> bytes
```

And the WAL-record codec functions:

```python
def verify_crc32(data: bytes) -> None
    # Raises DBChecksumError on mismatch. Reads the trailing 4 bytes as the
    # expected CRC and checks against the CRC of everything before them.

def encode_wal_record(record: WALRecord) -> bytes
    # Dispatches on the concrete class (WALPutRecord / WALDeleteRecord),
    # writes length + lsn + op_type + payload + crc, returns the full frame.

def decode_wal_record(data: bytes) -> WALRecord
    # Calls verify_crc32 first, then parses lsn, op_type, and the payload.

def decode_next_wal_record_from_file(fd: IO[bytes]) -> WALRecord
    # Reads the 4-byte length field from the file, reads the rest of the
    # frame by that length, and delegates to decode_wal_record. Raises
    # EOFError at end of file or on a short read (which is how torn tails
    # manifest).
```

The codec is **pure** — it never touches a file descriptor except for the one convenience wrapper that reads framed records off a file handle so the WAL service doesn't have to duplicate length-prefix logic.

#### `wal.py` — the stateful WAL service

One class, `WAL`, that owns the log file.

**`WAL` class.**

| name | kind | signature | behaviour |
|---|---|---|---|
| `__init__` | method | `WAL(path: Path) -> None` | Store the path. Do not touch the filesystem. |
| `path` | attribute | `Path` | The log file path. |
| `current_lsn` | attribute | `int` | LSN of the most recently appended record; 0 if empty. |
| `open` | method | `open() -> None` | Open the log file in append mode (`"a+b"`). If the file did not already exist, `fs.fsync_directory` on the parent to make the file creation durable. Idempotent: calling twice is a no-op. |
| `close` | method | `close() -> None` | Fsync, close the file descriptor, drop it. |
| `__enter__` / `__exit__` | methods | context manager | Wrap `open` / `close`. |
| `append_put` | method | `append_put(key: bytes, value: bytes) -> int` | Build a `WALPutRecord` with `lsn = current_lsn + 1`, encode it, write to the file, fsync, bump `current_lsn`, return the LSN. |
| `append_delete` | method | `append_delete(key: bytes) -> int` | Same, for `WALDeleteRecord`. |
| `replay` | method | `replay(callback: Callable[[WALRecord], None]) -> None` | Walk the file from the start, decoding one record at a time. On each successful record: check that its LSN equals the previous good LSN + 1 (sequential invariant), and call `callback(record)`. On a `DBChecksumError`, set a "saw-a-broken-record" flag and keep reading. If we later see *another* successful record after a broken one, raise `DBCorruptedError` — that's genuine corruption, not a torn tail. On `EOFError`, stop. Finally, truncate the file to the end of the last good record and fsync. |

Why the sequential-LSN check belongs in `replay` and not in the codec: the codec deals with *one* record at a time and can't know what came before. The WAL service holds the "previous good LSN" as loop state.

**Modifications to `DB`:**

- `DB(data_dir: str | Path)` — `data_dir` is required. The `is_opened` flag stays. Instantiate a `WAL` at `data_dir / "bptreedb.wal"` in `__init__`. Do not open it yet.
- `open()` — create `data_dir` if it doesn't exist (and fsync its parent via `fs.fsync_directory` if we created it). Then `wal.open()`, then `wal.replay(self._apply_wal_record)` to seed the in-memory `SortedDict` from history. Flip `is_opened = True`. If replay raises, close the WAL and re-raise.
- `_apply_wal_record(record)` — dispatch on type: `WALPutRecord` → `self.data[record.key] = record.value`; `WALDeleteRecord` → `self.data.pop(record.key, None)`. This is the shared in-memory application logic used by both replay and live operations. Crucially, **replay must not bump any version counter** (there is no counter yet, but iteration 3 will add one, and you will come back and carefully avoid bumping it from here).
- `put(key, value)` — `wal.append_put(key, value)` *first*, then `self.data[key] = value`. This is the discipline of write-ahead logging: log the change before making the change. The fsync happens inside `append_put`.
- `delete(key)` — in iteration 1 you unconditionally set a flag. Now, only log if the key is actually present (otherwise you pollute the log with no-ops). Check `key in self.data` first; if present, `wal.append_delete(key)`, then `del self.data[key]`, return `True`; otherwise return `False`.
- `close()` — `wal.close()`, clear the dict, flip `is_opened = False`.

Update the `db` pytest fixture to take `tmp_path` and instantiate `DB(tmp_path)`. Update the handful of tests that construct `DB()` directly.

#### The `FaultyFile` crash-test fixture

Lives in `tests/conftest.py` (or `tests/crash/conftest.py`). Wraps a real file. Every `write` goes to the file AND to an "unfsynced" buffer. `fsync` clears the unfsynced buffer and snapshots the current file contents. `crash()` reverts the on-disk file to the last snapshot.

To let tests inject `FaultyFile`, thread a private `_file_factory` parameter through `WAL.__init__` and (transitively) `DB.__init__`. It defaults to the built-in `open`. Yes, this is a private wart on the constructor — it's the price of property-based crash testing without monkey-patching, and it's well worth it.

The fixture exposes a `file_factory` callable that produces `FaultyFile` instances and a `crash_all(...)` helper that crashes every faulty file in the DB.

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

Replace the in-memory `SortedDict` with a paged file containing a B+ tree. The dataset is no longer bounded by RAM. The WAL stays exactly as it is — its records still describe logical operations and its replay still rebuilds the dataset from the log — but now "the dataset" lives in a file, and replay rebuilds *it* instead of an in-memory dict.

### Why now

This is the iteration where the database stops being a dict-with-a-log and starts being a database. It's the largest and most interesting iteration. You will introduce page dataclasses in `entities.py`, page codec functions in `codec.py`, and two new service modules (`pager.py`, `tree.py`); you will throw away the `SortedDict`; and at the end every existing iteration 1 / iteration 2 test will still pass *unchanged*, because the public API is the contract.

**Three new things on the API surface earn their place here:**

1. **`page_size_bytes` parameter on the `DB` constructor.** Default 4096; honoured only on creation (an existing DB uses the page size stored in its meta page). Update the `db` pytest fixture to pass `page_size_bytes=256` so that splits and merges happen often in tests.
2. **`DBRecordTooLargeError`.** Now that there's a maximum record size, `put` enforces it. Added to `exceptions.py` and re-exported.
3. **`DBConcurrentPageModificationError` and the version counter.** Now that `scan` returns a *real* lazy iterator that walks leaf pages via `right_sibling_page_id`, mutating the tree mid-scan is genuinely unsafe — a `put` between two `next()` calls can split the leaf the iterator is sitting on, move records, and rewrite sibling pointers. The iterator's "I am at slot N of leaf page P" state stops being meaningful. The version counter is the cheap, honest fix: the iterator snapshots the counter at construction and re-checks it on every `next()`.

   **Rules for the version counter** (owned by `DB`, not by the tree):

   - `DB.__init__` adds `self._version_counter = 0`.
   - `DB.put` bumps the counter at the end of every successful insert.
   - `DB.delete` bumps the counter only when the key was actually present. A failed `delete` is not a mutation and must not invalidate live iterators.
   - `DB.get` does NOT bump the counter — search is not a mutation.
   - `DB.scan` snapshots `self._version_counter` at the top, then checks it at the top of each loop iteration *before* yielding. The check belongs at the top of the loop body, which is the code that runs after the consumer's `next()` resumes the generator and before the next value is produced. This guarantees the check fires on the *first* `next()` too, so a mutation between iterator construction and the first `next()` raises.
   - `_apply_wal_record` (called from recovery) must NOT go through `put` / `delete` — that would bump the counter during replay. Keep it calling tree operations directly.
   - On the failure path (e.g., a `TypeError` from `put` on a non-`bytes` argument), the counter must NOT bump. Only successful mutations count.

This iteration proceeds in four sub-steps. Each is its own pass through the code, with its own commit. The order matters: data model first (pure data, trivial to test), codec next (pure functions over the data model, trivial to round-trip test), then pager (stateful wrapper around the codec and a file), then tree (algorithms operating on the dataclasses via the pager).

### Sub-step 3a: Grow the data model and the codec for pages

No new service modules yet. You are just adding dataclasses to `entities.py` and pure functions to `codec.py`.

#### Add to `entities.py`

```python
@dataclass
class MetaPage:
    magic: bytes              # always b"BPTREEDB"
    version: int              # currently 1
    page_size_bytes: int
    root_page_id: int
    next_page_id: int

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
    right_sibling_page_id: int                 # page id of the next leaf in key order; 0 if rightmost
    slots: list[LeafSlot]

@dataclass
class InternalPage:
    leftmost_child_page_id: int                # the "slot -1" pointer
    slots: list[InternalSlot]
```

These dataclasses carry **only** the fields the tree directly needs. Fields like `num_slots`, `free_space_start`, `free_space_end` are *wire-format bookkeeping* — the codec computes them on encode and discards them on decode. They do not belong on the dataclass.

Iteration 4 will add `last_modified_lsn` to `LeafPage` and `InternalPage`. Iteration 5 will add `last_checkpoint_lsn` to `MetaPage`. Iteration 6 will add `freelist_head` to `MetaPage` and a `FreelistPage` dataclass. Today, the fields above are all you need.

#### On-disk layouts (what the codec reads and writes)

All multi-byte integers are little-endian unsigned.

**Meta page (page 0), 36 bytes + zero padding to `page_size_bytes`:**

| offset | size | field             |
|-------:|-----:|-------------------|
|      0 |    8 | `magic` (`b"BPTREEDB"`) |
|      8 |    4 | `version`         |
|     12 |    4 | `page_size_bytes` |
|     16 |    8 | `root_page_id`    |
|     24 |    8 | `next_page_id`    |
|     32 |    4 | `crc32` (over bytes 0..32) |

**Slotted page header (internal & leaf), 24 bytes:**

| offset | size | field              |
|-------:|-----:|--------------------|
|      0 |    1 | `type_tag` (0x01 internal, 0x02 leaf) |
|      1 |    3 | reserved (zero, for alignment) |
|      4 |    4 | `num_slots`        |
|      8 |    4 | `free_space_start` (offset just past the last slot entry) |
|     12 |    4 | `free_space_end`   (offset of the first byte of the lowest record) |
|     16 |    8 | `right_sibling_page_id`    (leaves: next-leaf page id or 0; internal nodes: `leftmost_child_page_id`) |

The `right_sibling_page_id` slot is overloaded by node type: in a leaf it is the forward pointer used by `scan`; in an internal node it stores the leftmost child pointer. This overload keeps the header a single fixed shape and avoids a special "slot -1" case.

After the 24-byte header comes the slot directory growing downward from offset 24, with each slot entry being 8 bytes: `record_offset(4) | record_length(4)`. After the slot directory is free space. From the end of the page, records grow upward toward the slot directory.

**Leaf record body:** `key_length(4) | key | value_length(4) | value`
**Internal record body:** `key_length(4) | key | child_page_id(8)`

**Size limit.** The maximum encoded record body is `(page_size_bytes - 24) // 5 - 8` bytes. For `page_size_bytes = 4096` this is 806 bytes; for 256 it is 38 bytes. The `/5` keeps `max_slot_size < 0.2 * page_size + 24`, which is what lets the split algorithm always find a partition with both halves above the 40% threshold; `/4` would technically still guarantee "at least four records fit in an empty page", but with larger slots an unbalanced split can leave one half underfull. Expose this as a function `max_record_body_size(page_size_bytes) -> int` in `codec.py` so the tree can call it from both the size check in `put` and the split decision.

#### Add to `codec.py`

Pure functions, plus a helper:

```python
def max_record_body_size(page_size_bytes: int) -> int

def encode_meta_page(meta: MetaPage, page_size_bytes: int) -> bytes
    # Packs magic, version, page_size_bytes, root_page_id, next_page_id,
    # appends crc32, and zero-pads to page_size_bytes.

def decode_meta_page(buf: bytes) -> MetaPage
    # Verifies magic and CRC. Raises DBCorruptedError on magic mismatch.
    # Raises DBChecksumError on CRC mismatch (which is a DBCorruptedError).

def encode_page(page: LeafPage | InternalPage, page_size_bytes: int) -> bytes
    # Walks page.slots, packs record bodies from the end of the buffer growing
    # upward, packs slot entries from offset 24 growing downward, writes the
    # header with computed num_slots / free_space_start / free_space_end /
    # right_sibling_page_id (or leftmost_child_page_id). The output is exactly page_size_bytes.
    # Raises DBRecordTooLargeError if any single record body exceeds
    # max_record_body_size(page_size_bytes).
    # Raises ValueError if the encoded result would overflow page_size_bytes
    # (this indicates a tree-level bug — the tree should have split before
    # asking the codec to encode an oversized page).

def decode_page(buf: bytes) -> LeafPage | InternalPage
    # Reads the header, dispatches on type_tag, walks the slot directory to
    # reconstruct the slots list. No CRC — slotted pages are protected by
    # the WAL, not by per-page checksums.
```

Because the codec re-packs from scratch on every `encode_page`, there is no in-memory "compaction" step. Deletions simply `slots.pop(i)`; the next encode produces a tight page with no holes. This is a deliberate simplification — you're trading per-encode CPU (fine in Python) for a *lot* of algorithmic simplicity compared to an in-place byte-buffer design.

#### Tests (`tests/unit/test_codec.py`, growing the file from iteration 2)

- `encode_meta_page` → `decode_meta_page` round-trips.
- Tampered meta page CRC raises `DBChecksumError`.
- Wrong magic raises `DBCorruptedError`.
- `encode_page(LeafPage(...))` → `decode_page` round-trips. Verify `slots` list matches.
- Same for `InternalPage`.
- `encode_page` of an empty leaf / empty internal page round-trips.
- `encode_page` with a record body exceeding `max_record_body_size` raises `DBRecordTooLargeError`.
- Property test: generate random `LeafPage` instances with valid record bodies, round-trip, assert equality.

Suggested commit: `feat(codec): slotted page and meta page encode/decode`.

### Sub-step 3b: The pager

A new module: `pager.py`. A new test file: `tests/unit/test_pager.py`.

The pager owns the file `<data_dir>/bptreedb.data`. It is the only thing that touches the file. The WAL (from iteration 2) is still its own thing, owning `bptreedb.wal` independently.

The data file is `N * page_size_bytes` bytes for some `N ≥ 2`: page 0 is the meta page, page 1 is the initial empty root leaf. The pager **reads and writes raw page-sized `bytes` buffers** via `read_page` / `write_page`. For data pages it is the buffer pool (iteration 4+) or the tree (iteration 3 only) that calls `codec.encode_page` / `codec.decode_page`; for the meta page, the pager itself calls the codec because it holds the in-memory meta.

**`Pager` class, iteration 3 surface.**

| name | kind | signature | behaviour |
|---|---|---|---|
| `__init__` | method | `Pager(path: Path, *, page_size_bytes: int, _file_factory=...) -> None` | Store the path and requested page size. No filesystem work. |
| `path` | attribute | `Path` | Path to `bptreedb.data`. |
| `page_size_bytes` | attribute | `int` | Honoured from the on-disk meta page after `open()` for existing databases. |
| `num_pages` | property | `int` | Return `meta.next_page_id`. This is the authoritative page count — do not derive it from the file size, which can disagree due to write buffering. |
| `open` | method | `open() -> None` | If the file does not exist: create it, write an initial meta page with `root_page_id=1, next_page_id=2`, bump-allocate page 1 as an empty `LeafPage(right_sibling_page_id=0, slots=[])` and write it, fsync the file, fsync the parent directory. If the file exists: read and decode the meta page (raise `DBCorruptedError` on checksum or magic failure), adopt its stored `page_size_bytes`. |
| `close` | method | `close() -> None` | If the meta is dirty, `flush_meta()`. Then fsync and close the file. |
| `read_page` | method | `read_page(page_id: int) -> bytes` | Seek to `page_id * page_size_bytes` and read exactly `page_size_bytes` bytes. No decoding. |
| `write_page` | method | `write_page(page_id: int, data: bytes) -> None` | Seek to the offset and write. `len(data)` must equal `page_size_bytes`. No fsync. |
| `fsync` | method | `fsync() -> None` | `fs.fsync_file` on the data file. |
| `get_meta` | method | `get_meta() -> MetaPage` | Return the in-memory `MetaPage` snapshot. Callers must treat it as read-only. |
| `update_meta` | method | `update_meta(**fields) -> None` | Mutate the in-memory `MetaPage` (e.g. `update_meta(root_page_id=17)`) and mark it dirty. Does not touch the file. |
| `flush_meta` | method | `flush_meta() -> None` | Re-encode the in-memory meta via `codec.encode_meta_page` and write it to page 0 via `write_page`. Clear the dirty flag. Does not fsync. |
| `allocate_page` | method | `allocate_page() -> int` | Return `meta.next_page_id` and bump it (`update_meta(next_page_id=meta.next_page_id + 1)`). Extend the file by one zero-filled page so `read_page` on the new id returns a valid buffer. (Iteration 6 will also consult the freelist first.) |

**What "the meta is dirty" means at this iteration.** The Pager holds two pieces of state for the meta page: the in-memory `MetaPage` dataclass (returned by `get_meta`) and a single `_meta_dirty: bool` flag. `update_meta` mutates the dataclass and sets the flag to `True`; `flush_meta` encodes the dataclass, calls `write_page(0, ...)`, and sets the flag back to `False`; `close` checks the flag and calls `flush_meta` if needed. That's the whole mechanism — one boolean on the Pager instance. There is no per-page dirty tracking for *data* pages at this iteration, because data pages are written through immediately by the tree (see next point) and therefore can't be "dirty" in any meaningful sense.

**Two deliberate simplifications at this iteration:**

1. **There is no buffer pool yet.** `read_page` and `write_page` go to disk every time. When the tree mutates a leaf, it re-encodes the page and immediately calls `pager.write_page` — the new bytes are in the OS page cache before the mutating method returns. Nothing is ever "dirty but not yet written" at the data-page level. This will be slow; you will *feel* the slowness, and that feeling is what motivates iteration 4. When iteration 4 adds the buffer pool, `mark_dirty` will gain a real meaning: "this cached page has been mutated in memory, and its new bytes have not yet reached the pager" — plus a per-page `dirty: bool` flag on every cache entry to track exactly that.
2. **`flush_meta` is called only by `close`.** The meta page hits disk at most once per DB session. Iteration 5 (checkpoints) will take over that responsibility.

#### Tests (`tests/unit/test_pager.py`)

- Fresh directory → `open()` creates `bptreedb.data` of size `2 * page_size_bytes`, meta page decodes with the requested size and `root_page_id == 1`.
- Reopen existing database → meta decodes correctly.
- Reopen with a conflicting `page_size_bytes` → the file's value wins silently (the constructor argument is only honoured on first creation).
- Tampered meta page CRC → `open` raises `DBCorruptedError`.
- `write_page` then `read_page` round-trips arbitrary bytes.
- `update_meta` then `get_meta` reflects the change without touching the file.
- `flush_meta` then reopen returns the updated meta.
- `allocate_page` returns sequential ids and grows the file by one page each call.

Suggested commit: `feat(pager): paged data file with meta page and bump-allocation`.

### Sub-step 3c: The B+ tree

A new module: `tree.py`. A new test file: `tests/unit/test_tree.py`. A new `_debug.py` with the invariant checker.

The `BTree` class takes a `Pager` and exposes the algorithms. It does not know about WAL or recovery; it works purely in terms of page reads, page writes, and the meta page's `root_page_id`. In iteration 3 it asks the pager directly for pages; in iteration 4 you will add a buffer pool *alongside* the pager — the tree will route page reads and writes through the pool, but it still needs the pager for meta operations (`get_meta`, `update_meta`, `allocate_page`), which the pool does not handle. Most of the tree code is unchanged in iteration 4 because a buffer-pool `get` returns the same kind of dataclass that `codec.decode_page` returns.

#### How the tree, pager, and codec interact

The pager deals exclusively in raw `bytes` buffers — it does not know what a `LeafPage` or `InternalPage` is. The codec converts between raw bytes and typed dataclasses. The tree is the glue: it calls the pager to get bytes, then calls the codec to interpret them.

**Reading a page** (used by every tree operation):

```python
raw: bytes       = self.pager.read_page(page_id)          # pager → raw bytes
page: LeafPage | InternalPage = codec.decode_page(raw)     # codec → dataclass
```

**Writing a page** (used after every mutation):

```python
raw: bytes = codec.encode_page(page, self.pager.page_size_bytes)  # dataclass → raw bytes
self.pager.write_page(page_id, raw)                               # raw bytes → pager
```

Wrap these two patterns in private helpers on `BTree`:

```python
def _read_page(self, page_id: int) -> LeafPage | InternalPage:
    return codec.decode_page(self.pager.read_page(page_id))

def _write_page(self, page_id: int, page: LeafPage | InternalPage) -> None:
    self.pager.write_page(page_id, codec.encode_page(page, self.pager.page_size_bytes))
```

Every tree method uses `_read_page` and `_write_page` — it never touches `pager.read_page` or `codec.decode_page` directly. This is the seam that iteration 4 will exploit: replacing `_read_page` / `_write_page` with `buffer_pool.get` / `buffer_pool.mark_dirty` without changing any of the tree logic.

**Finding the root page id:** the tree gets it from the pager's meta page — `self.pager.get_meta().root_page_id`. After a split that creates a new root, the tree calls `self.pager.update_meta(root_page_id=new_root_id)` to update it.

#### How `search` works, step by step

`search(key)` is a top-to-bottom walk from the root to a leaf. Here is the complete algorithm:

1. Read the root page id from the meta page: `page_id = self.pager.get_meta().root_page_id`.
2. Read the page: `page = self._read_page(page_id)`.
3. **If `page` is a `LeafPage`:** binary-search (or linear-search — the list is small) the `slots` list by `slot.key` for the target `key`. If found, return `slot.value`. If not found, return `None`.
4. **If `page` is an `InternalPage`:** determine which child to descend into. An internal page has `leftmost_child_page_id` plus N slots, each slot being `InternalSlot(key, child_page_id)`. The slots act as separators: all keys in the subtree under `leftmost_child_page_id` are strictly less than `slots[0].key`; all keys in the subtree under `slots[0].child_page_id` are ≥ `slots[0].key` and < `slots[1].key`; and so on. So:
   - Binary-search the slots for the *rightmost* slot where `slot.key <= key`.
   - If you find one at index `i`, descend into `slots[i].child_page_id`.
   - If no slot has `slot.key <= key` (i.e., `key` is less than all separators), descend into `leftmost_child_page_id`.
   - Set `page_id` to the chosen child and go back to step 2.

This is the entire algorithm. It terminates because the tree has finite depth, and every step descends one level.

**A concrete example.** Imagine a tree with page size 256, containing keys `b"01"` through `b"09"`. The root is an internal page at page 3:

```
InternalPage(leftmost_child_page_id=1, slots=[InternalSlot(key=b"05", child_page_id=2)])
```

Leaf at page 1: `LeafPage(right_sibling_page_id=2, slots=[LeafSlot(b"01", ...), ..., LeafSlot(b"04", ...)])`
Leaf at page 2: `LeafPage(right_sibling_page_id=0, slots=[LeafSlot(b"05", ...), ..., LeafSlot(b"09", ...)])`

Searching for `b"03"`: read page 3 (internal), `b"03" < b"05"` so descend into `leftmost_child_page_id=1`, read page 1 (leaf), find `b"03"` in the slots, return its value.

Searching for `b"07"`: read page 3 (internal), `b"07" >= b"05"` so descend into `child_page_id=2`, read page 2 (leaf), find `b"07"`, return its value.

Searching for `b"99"`: read page 3 (internal), `b"99" >= b"05"` so descend into `child_page_id=2`, read page 2 (leaf), `b"99"` is not in the slots, return `None`.

#### How `_find_leaf` factors out the walk

The root-to-leaf walk appears in `search`, `insert`, `delete`, and `scan`. Factor it into a helper:

```python
def _find_leaf(self, key: bytes) -> tuple[int, LeafPage, list[tuple[int, int]]]:
    """
    Walk from root to the leaf that would contain `key`.

    Return (leaf_page_id, leaf_page, path) where `path` is a list of
    (parent_page_id, child_index) pairs.  `child_index` is the index into
    the parent's slots that was followed, or -1 if the descent went through
    `leftmost_child_page_id`.
    """
```

`search` calls `_find_leaf` and then scans the leaf's slots. `insert` and `delete` also use the `path` to propagate splits and merges upward.

**`BTree` class, iteration 3 surface.**

| name | kind | signature | behaviour |
|---|---|---|---|
| `__init__` | method | `BTree(pager: Pager) -> None` | Store the pager. No other state — the root pointer lives in `pager.get_meta().root_page_id`, so there's nothing to cache here. |
| `HALF_FULL_THRESHOLD` | class constant | `float = 0.4` | Minimum fraction of `page_size_bytes` that a non-root page must use for its encoded form after a delete. |
| `search` | method | `search(key: bytes) -> bytes \| None` | Call `_find_leaf(key)`, then binary-search the leaf's `slots` by key. Return the value if found, else `None`. |
| `insert` | method | `insert(key: bytes, value: bytes) -> None` | Call `_find_leaf(key)` to get the leaf and path. Insert in sorted position. If the leaf overflows, split it; propagate splits upward using the path. Raise `DBRecordTooLargeError` if the encoded leaf-record body would exceed `codec.max_record_body_size(pager.page_size_bytes)`. |
| `delete` | method | `delete(key: bytes) -> bool` | Call `_find_leaf(key)` to get the leaf and path. If the key is absent, return `False`. Otherwise remove the slot. If the leaf drops below `HALF_FULL_THRESHOLD * page_size_bytes` encoded bytes, rebalance: redistribute or merge with a same-parent sibling; propagate upward using the path; collapse the root if it is left with a single child. Return `True`. |
| `scan` | method | `scan(start_key_inclusive: bytes \| None, end_key_exclusive: bytes \| None, version_check: Callable[[], None]) -> Iterator[tuple[bytes, bytes]]` | Call `_find_leaf(start_key_inclusive)` to find the starting leaf. Walk leaves forward via `right_sibling_page_id`, yielding `(key, value)` pairs. At the top of every iteration — *before* yielding — call `version_check()`, which is the callback `DB` passes in; that callback raises `DBConcurrentPageModificationError` if the tree was mutated. Stop at the end bound or at a null `right_sibling_page_id` (value 0). |
| `_read_page` | method | `_read_page(page_id: int) -> LeafPage \| InternalPage` | `codec.decode_page(self.pager.read_page(page_id))`. |
| `_write_page` | method | `_write_page(page_id: int, page: LeafPage \| InternalPage) -> None` | `self.pager.write_page(page_id, codec.encode_page(page, self.pager.page_size_bytes))`. |
| `_find_leaf` | method | `_find_leaf(key: bytes) -> tuple[int, LeafPage, list[tuple[int, int]]]` | Walk root → leaf as described above. Return `(leaf_page_id, leaf_page, path)`. |

**The version counter is owned by `DB`, not by `BTree`.** The tree takes a callback so that the check fires at the tree level without the tree needing to know about `DB`'s version field. `DB.scan` will implement the callback as something like:

```python
def scan(self, start, end):
    self._check_if_opened()
    ...
    version_snapshot = self._version_counter
    def check():
        if self._version_counter != version_snapshot:
            raise DBConcurrentPageModificationError()
    return self._tree.scan(start, end, version_check=check)
```

#### Crash safety of multi-page writes

Splits, merges, and root changes touch multiple pages non-atomically. A crash mid-split can leave orphaned pages, dangling parent pointers, or half-written siblings. **This is fine in iteration 3** because the tree pages are not the source of truth — the WAL is. On recovery, `open()` replays the entire WAL into a fresh tree, so any on-disk tree corruption is discarded and rebuilt. The cost is that the WAL grows forever and recovery replays from the start of time. Iteration 5 (checkpoints) will introduce a way to establish a consistent on-disk tree state, after which only the WAL tail needs replaying.

#### Bring the tree to life in this order

Do not try to implement everything at once. Walk this ladder:

1. **`search(key)` first.** Hand-build a tiny two-leaf tree by calling `pager.write_page` with the output of `codec.encode_page` on freshly constructed `LeafPage` and `InternalPage` dataclasses. Test that `search` returns the right value for keys in each leaf, for the leaf boundaries, and for absent keys. This earns you confidence in the tree-walking and binary-search code without ever touching insert.

2. **`insert(key, value)` for the case where the leaf has room.** Walk to the leaf (remembering the path), locate the sorted position, `slots.insert(i, LeafSlot(key, value))`, `codec.encode_page` and `pager.write_page`. If the key already exists, overwrite the slot's `value`. Test by inserting many keys into a small page and `search`-ing each.

3. **Leaf split.** After inserting the new slot in step 2, check whether the leaf still fits in a page. Use `_encoded_size_estimate(leaf)` to check cheaply without encoding. If it overflows, split:

   **How a leaf split works, step by step:**

   Starting state — leaf page 1 is full after inserting `b"05"`:
   ```
   Page 1 (leaf): slots=[("01",v), ("02",v), ("03",v), ("04",v), ("05",v)]
                  right_sibling_page_id=0
   Meta: root_page_id=1
   ```

   a. **Find the split point.** Walk the slot list, summing encoded byte sizes. The byte midpoint — the first index where the running total crosses half the page capacity — is the starting guess, and splitting by byte size rather than slot count matters because variable-length records mean equal counts don't guarantee equal sizes. The byte midpoint alone isn't always good enough, though: when one slot is much larger than the others, the half that falls on the wrong side of it can land below the 40% threshold, and the split would persist an underpopulated page. Handle that by starting at the byte midpoint and scanning outward for a split index where *both* halves are at or above threshold. Such an index is guaranteed to exist given the `/5` record cap, which bounds `max_slot_size < 0.2 * page_size + 24`.

   b. **Allocate a new leaf:** `new_leaf_id = pager.allocate_page()`.

   c. **Move the upper half of slots into the new leaf.** Suppose the split point is after index 2:
   ```python
   new_leaf = LeafPage(
       right_sibling_page_id=old_leaf.right_sibling_page_id,  # inherit the old tail
       slots=old_leaf.slots[3:],                               # upper half moves out
   )
   old_leaf.slots = old_leaf.slots[:3]                         # lower half stays
   old_leaf.right_sibling_page_id = new_leaf_id                # point old → new
   ```

   d. **The promoted key** is `new_leaf.slots[0].key` — the smallest key in the new (right) leaf. For a leaf split, this key is *copied* up: it still exists in the leaf's slot list.

   e. **Write both leaves:** `_write_page(page_1_id, old_leaf)` and `_write_page(new_leaf_id, new_leaf)`.

   f. **Insert the promoted key into the parent.** This is where the `path` from `_find_leaf` comes in. Pop the last entry `(parent_page_id, child_index)` off the path. Read the parent: `parent = _read_page(parent_page_id)`. Insert a new slot into the parent's slot list:
   ```python
   parent.slots.insert(child_index + 1, InternalSlot(key=promoted_key, child_page_id=new_leaf_id))
   ```
   Why `child_index + 1`? Because `child_index` is the slot (or -1 for `leftmost_child_page_id`) that led to the old leaf. The new child goes immediately *after* that position in the parent's slot list. Then `_write_page(parent_page_id, parent)`.

   **Special case — splitting the root leaf.** If the path is empty (the leaf *was* the root), there is no parent to insert into. Instead, create a brand new internal page to be the new root:
   ```python
   new_root_id = pager.allocate_page()
   new_root = InternalPage(
       leftmost_child_page_id=old_leaf_id,    # left half
       slots=[InternalSlot(key=promoted_key, child_page_id=new_leaf_id)],  # right half
   )
   _write_page(new_root_id, new_root)
   pager.update_meta(root_page_id=new_root_id)
   ```

   End state after splitting:
   ```
   Page 4 (internal, new root): leftmost_child_page_id=1
                                slots=[InternalSlot("03", child_page_id=2)]
   Page 1 (leaf): slots=[("01",v), ("02",v)]   right_sibling_page_id=2
   Page 2 (leaf): slots=[("03",v), ("04",v), ("05",v)]   right_sibling_page_id=0
   Meta: root_page_id=4
   ```

4. **Internal split propagation.** After inserting a promoted key into the parent in step 3f, the parent itself might overflow. Check with `_encoded_size_estimate(parent)`. If it overflows, split the internal page — the mechanics are similar to a leaf split, but with one critical asymmetry:

   **How an internal split works, step by step:**

   Starting state — internal page 4 is full after receiving a promoted key:
   ```
   Page 4 (internal): leftmost_child_page_id=1
                      slots=[("10", child=2), ("20", child=3), ("30", child=5), ("40", child=6)]
   ```

   a. **Find the median.** Suppose the median is at index 2 (the slot `("20", child=3)`).

   b. **The median is *removed* from both halves and promoted.** This is the key difference from leaf splits. In a leaf split, the promoted key is *copied* (it stays in the new right leaf). In an internal split, the median key is *moved* out — it does not appear in either resulting internal page.

   c. **Build the two halves:**
   ```python
   # Left half keeps everything before the median.
   # old_page.leftmost_child_page_id stays unchanged.
   old_page.slots = old_page.slots[:2]   # [("10", child=2)]
   # So left page has: leftmost=1, slots=[("10", child=2)]
   # Its children are: 1, 2

   # Right half gets everything after the median.
   # The median's child_page_id becomes the new page's leftmost_child.
   new_page_id = pager.allocate_page()
   new_page = InternalPage(
       leftmost_child_page_id=old_page.slots[median_idx].child_page_id,  # = 3
       slots=old_page.slots[median_idx + 1:],  # [("30", child=5), ("40", child=6)]
   )
   # So right page has: leftmost=3, slots=[("30", child=5), ("40", child=6)]
   # Its children are: 3, 5, 6
   ```

   d. **The promoted key** is the median's key (`"20"`), and the promoted child is `new_page_id`. Write both pages.

   e. **Insert the promoted key into the grandparent** using the same logic as step 3f — pop the next entry off the path, insert `InternalSlot(promoted_key, new_page_id)` at the right position.

   f. **If the path is exhausted**, you've split the root. Create a new root:
   ```python
   new_root_id = pager.allocate_page()
   new_root = InternalPage(
       leftmost_child_page_id=old_page_id,
       slots=[InternalSlot(key=promoted_key, child_page_id=new_page_id)],
   )
   _write_page(new_root_id, new_root)
   pager.update_meta(root_page_id=new_root_id)
   ```

   **Why "copy up" for leaves but "move up" for internals?** In a leaf, every key *is* the data — removing it from the leaf would lose it. In an internal page, keys are just separators — they're routing metadata, not data. Moving the median up to the parent is sufficient; keeping a copy in the child would create a redundant separator that wastes space and complicates traversal.

5. **`scan(start, end, version_check)`.** Find the leaf containing `start` via a search-style walk that stops at leaf level. Iterate slots from there, yielding while the key is below `end`. When you run off the end of a leaf, move to `right_sibling_page_id` and continue. Call `version_check()` at the top of every step of the loop, *before* yielding.

6. **`delete(key)` without rebalance.** Locate the slot, `slots.pop(i)`, encode, write. Return `True`. Don't worry about the half-full invariant yet.

7. **Same-parent merge and redistribute.** After a delete, if the leaf's encoded size drops below `HALF_FULL_THRESHOLD * page_size_bytes`, the leaf is underful and needs rebalancing.

   **Finding same-parent siblings.** Use the `path` from `_find_leaf`. The last entry is `(parent_page_id, child_index)` — the parent page and which of its children led to our leaf. Read the parent, then find the leaf's immediate left and right siblings *within that parent*:

   ```
   Parent: leftmost_child_page_id=1, slots=[("10",child=2), ("20",child=3), ("30",child=5)]
                                       idx=0              idx=1              idx=2
   Children in order: 1,       2,       3,       5
                     (lm)    (s[0])   (s[1])   (s[2])
   ```

   - If our leaf is `child=3` (the parent reached it via `slots[1]`, so `child_index=1`), its left sibling is `slots[0].child_page_id = 2` and its right sibling is `slots[2].child_page_id = 5`.
   - If our leaf is `child=1` (reached via `leftmost_child_page_id`, so `child_index=-1`), it has no left sibling. Its right sibling is `slots[0].child_page_id = 2`.
   - If our leaf is `child=5` (`child_index=2`, the last slot), it has no right sibling. Its left sibling is `slots[1].child_page_id = 3`.

   **Important:** do **not** use `right_sibling_page_id` to find siblings for merging. That pointer can cross parent boundaries, and merging across parents would corrupt the tree.

   **Try to redistribute first.** Read the sibling. If the sibling has enough slots that you can move one or more records from the sibling to the underful leaf and leave *both* pages ≥ 40% full, redistribute:

   ```
   Before redistribute (left sibling has spare capacity):
     Page 2 (leaf): slots=[("10",v), ("11",v), ("12",v), ("13",v)]
     Page 3 (leaf): slots=[("20",v)]                  ← underful after delete
     Parent separator between them: "20" at slots[1]

   Move the last slot from page 2 into the front of page 3:
     Page 2 (leaf): slots=[("10",v), ("11",v), ("12",v)]
     Page 3 (leaf): slots=[("13",v), ("20",v)]

   Update the parent's separator to reflect the new boundary:
     Parent slots[1].key = "13"    ← the new smallest key in page 3
   ```

   Write both leaves and the parent.

   **If redistribution isn't possible, merge.** Combine the two leaves into one and remove the separator from the parent:

   ```
   Before merge:
     Page 2 (leaf): slots=[("10",v)]          ← also small
     Page 3 (leaf): slots=[("20",v)]          ← underful
     Parent: leftmost=1, slots=[("10",child=2), ("20",child=3), ("30",child=5)]

   Merge page 3 into page 2 (append right's slots to left's):
     Page 2 (leaf): slots=[("10",v), ("20",v)]
     Page 2's right_sibling_page_id = page 3's old right_sibling_page_id  ← splice out page 3

   Remove the separator that pointed to the merged-away page (page 3) from the parent:
     Parent: leftmost=1, slots=[("10",child=2), ("30",child=5)]
     (The separator "20" is gone; page 2 now covers everything < "30".)

   Page 3 is now unreachable. Its page id leaks in iteration 3; iteration 6's freelist will reclaim it.
   ```

   Write the surviving leaf and the parent. The parent may now be underful — recurse upward (step 8).

8. **Internal merge propagation and root collapse.** When an internal page becomes underful after losing a separator in step 7, the same redistribute-or-merge logic applies, but with the same asymmetry as splits: the parent's separator is *pulled down* into the merge.

   **Internal merge, step by step:**
   ```
   Before:
     Grandparent: leftmost=4, slots=[("50",child=7)]
     Page 4 (internal): leftmost=1, slots=[("10",child=2), ("30",child=5)]
     Page 7 (internal): leftmost=8, slots=[("70",child=9)]      ← underful

   The grandparent's separator between pages 4 and 7 is "50".

   Merge page 7 into page 4:
     1. Pull the separator "50" DOWN from the grandparent into page 4 as a new slot.
        The child for this slot is page 7's leftmost_child_page_id (= 8).
        Page 4 slots: [("10",child=2), ("30",child=5), ("50",child=8)]
     2. Append all of page 7's slots to page 4:
        Page 4 slots: [("10",child=2), ("30",child=5), ("50",child=8), ("70",child=9)]
     3. Remove the separator "50" from the grandparent.
        Grandparent: leftmost=4, slots=[]
   ```

   **Internal redistribute** works similarly: move a slot from the sibling, but route it *through* the parent's separator. Move the parent's separator down into the underful node, then move the sibling's edge slot up to replace the parent's separator.

   **Root collapse.** After a merge, if the root is left with zero slots (an internal page with only `leftmost_child_page_id` and no separators), it has exactly one child. Collapse:
   ```python
   pager.update_meta(root_page_id=root.leftmost_child_page_id)
   ```
   The old root page leaks (iteration 6 reclaims it). The tree is now one level shorter.

**Helper functions you'll want in `tree.py`:**

- `_encoded_size_estimate(page) -> int` — walks the slot list summing header + slot directory + record bodies, without actually calling `encode_page`. Used in split/merge decisions.
- `_find_leaf(key) -> (leaf_page_id, path)` — returns the leaf that would contain `key` and the path of `(parent_id, child_index)` entries. Used by `search`, `insert`, and `delete`.

#### The invariant helper (`_debug.py`)

```python
def assert_tree_invariants(tree: BTree) -> None
    # Walks the entire tree and asserts:
    #   1. All leaves are at the same depth.
    #   2. Every non-root page uses >= HALF_FULL_THRESHOLD of the page.
    #   3. Every page's slot list is sorted ascending by key.
    #   4. Internal node separators correctly partition the children:
    #      max(child[i]) < separator[i] <= min(child[i+1]).
    #   5. Leaf sibling pointers form a complete forward chain in key order,
    #      with no cycles.
    #   6. Every page reachable from the tree has a page id < next_page_id.
    # On failure, raise AssertionError with a dump_tree(tree) rendering so
    # the test output is debuggable.

def dump_tree(tree: BTree) -> str
    # Human-readable BFS walk of the tree. Used in assertion messages.
```

Call `assert_tree_invariants(tree)` from every property test after every operation. It is the test you will thank yourself for.

#### Tests (`tests/unit/test_tree.py`)

- Hand-built two-level tree: `search` returns correct values for keys in both leaves, at boundaries, and for absent keys.
- Single-leaf insert (no split): all keys readable.
- Insert until split: tree has two leaves with correct distribution and a new root; sibling chain is correct.
- Insert until two levels of internal nodes: invariants hold throughout (mark slow).
- Internal split moves the median: the median key does NOT appear in either of the two resulting internal nodes, only in the new parent.
- `scan` over a multi-leaf tree returns all keys in order; bounded scans return the correct half-open range.
- Delete of a present key returns `True`; delete of an absent key returns `False`.
- Delete enough to force a redistribution: both leaves end up ≥ 40% full and the parent's separator is updated.
- Delete enough to force a merge: the resulting tree still passes `assert_tree_invariants`.
- Delete enough to force the root to collapse.
- Insert/delete fuzzing: 1000 random ops, `assert_tree_invariants` after every op.

Suggested commit: `feat(tree): on-disk B+ tree with split and merge`.

### Sub-step 3d: Wire the tree into `DB`

Now that `pager.py` and `tree.py` exist, rewire `db.py`:

- Remove the `SortedDict` attribute and everything that touches it. This is the only throwaway in the project.
- Add `page_size_bytes: int = 4096` to the `DB.__init__` signature, after `data_dir`.
- In `__init__`, also instantiate a `Pager(data_dir / "bptreedb.data", page_size_bytes=page_size_bytes)` and a `BTree(pager)`. Do not open them.
- Add `self._version_counter = 0`.
- `open()` — create `data_dir` if it doesn't exist (iteration 2 logic). Open the pager. Open the WAL. `wal.replay(self._apply_wal_record)`. Flip `is_opened`.
- `_apply_wal_record(record)` now becomes: `WALPutRecord` → `self._tree.insert(record.key, record.value)`; `WALDeleteRecord` → `self._tree.delete(record.key)`. Does not touch the version counter.
- `put(key, value)` — checks, `wal.append_put(key, value)`, `tree.insert(key, value)`, bump version counter.
- `delete(key)` — checks, `wal.append_delete(key)` *only* if the key is present (you need a way to know — either do a `tree.search` first, or have `tree.delete` return `False` without mutating and have `DB.delete` consult the return value). Prefer the second approach: call `tree.search` first to decide whether to log, then call `tree.delete`. Bump the counter on success only.
- `get(key)` — checks, `return tree.search(key)`. No counter bump.
- `scan(start, end)` — checks eagerly (not in the generator body), snapshot the counter, and return `tree.scan(start, end, version_check=...)` where the callback compares the live counter to the snapshot and raises.
- `close()` — `pager.flush_meta()`, `pager.close()`, `wal.close()`. Clear the version counter for good measure.

> **Note on `delete`'s "only log if present" rule.** In iteration 2, the in-memory dict made the presence check cheap (`key in self.data`). On a real tree, `tree.search` is O(log N) disk reads, which doubles the cost of every delete. That's acceptable in iteration 3. Iteration 4's buffer pool will make it cheap again.

### Tests that earn the milestone

- All Iteration 1 unit tests still pass.
- All Iteration 1 property tests still pass — they should, because they only check the public API contract.
- All Iteration 2 persistence and crash tests still pass — also unchanged. The WAL is the same, the recovery procedure is the same, only what gets replayed into has changed.
- All new `test_page.py`, `test_pager.py`, `test_tree.py` tests pass.
- `assert_tree_invariants` passes after every operation in the property test that uses it.

**New unit tests for the version counter** (added to `tests/unit/test_db.py`):

- `scan` followed by `put` raises `DBConcurrentPageModificationError` on the next `next()`.
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

A new module: `cache.py`. A new test file: `tests/unit/test_cache.py`. Modifications to `tree.py` so that all page reads and writes go through the cache instead of the pager.

**`last_modified_lsn` earns its place.** Add it as a field to `LeafPage` and `InternalPage` in `entities.py`, and grow the slotted-page header in `codec.py` from 24 bytes to 32 bytes, with `last_modified_lsn` at offset 16 and `right_sibling_page_id` / `leftmost_child_page_id` shifting to offset 24. Update `codec.max_record_body_size` to account for the new header size. This is the one place where an existing codec test from iteration 3 gets edited rather than added to — the `test_codec.py` tests that assert "byte N of the header is field X" must follow the new layout.

**Why `last_modified_lsn` earns its place *now*.** In iterations 3, the tree was the only thing writing pages, and it did so synchronously: every mutation immediately re-encoded the page and wrote it through. Once the buffer pool exists, pages may be evicted or flushed at times the tree no longer controls. The LSN field ties each page to the WAL record that last touched it, so the buffer pool / checkpoint logic can enforce the rule "no page hits disk until the WAL record that protects it is fsynced." That is the WAL ordering rule.

In this iteration the rule is automatically satisfied because every `put` calls `wal.append + wal.fsync` *before* `tree.insert`, so by the time the tree calls `mark_dirty(page_id, lsn)`, the WAL record at `lsn` has already been fsynced. The LSN field therefore doesn't *do* anything observable in iteration 4 — it's bookkeeping. It earns its place in iteration 5 as the checkpoint cursor, and would earn a second role in a hypothetical iteration 7 that switched to a STEAL policy.

**Note on what the buffer pool does *not* replace.** The pool caches `LeafPage` and `InternalPage` dataclasses — tree pages. The meta page is *not* buffer-pooled: it remains owned by the `Pager`, with its own in-memory `MetaPage` dataclass and its own `_meta_dirty` flag (described in iteration 3). The `BTree` therefore holds **both** a `Pager` reference and a `BufferPool` reference in iteration 4: it goes through the pool for tree page reads and writes, and continues to call the pager directly for `allocate_page`, `get_meta`, and `update_meta`. Root splits and root collapses update `root_page_id` via `self.pager.update_meta(root_page_id=...)` exactly as in iteration 3.

#### The `BufferPool` class

The buffer pool caches *decoded* `LeafPage` / `InternalPage` dataclasses, not raw bytes. On a miss it calls `pager.read_page` and then `codec.decode_page`. On `flush_all` it calls `codec.encode_page` and then `pager.write_page`. This is the whole point of the entities/codec split: the pool holds objects the tree can mutate directly, and only crosses the byte boundary at flush time.

| name | kind | signature | behaviour |
|---|---|---|---|
| `__init__` | method | `BufferPool(pager: Pager, capacity_pages: int) -> None` | Store the pager and capacity. Initialize an empty `OrderedDict[int, CachedPage]`. |
| `capacity_pages` | attribute | `int` | Maximum number of pages held in memory. |
| `dirty_count` | property | `int` | Number of dirty pages currently pinned. |
| `get` | method | `get(page_id: int) -> LeafPage \| InternalPage` | Cache hit → move to MRU, return the cached dataclass. Miss → `pager.read_page`, `codec.decode_page`, insert at MRU, evict the LRU *clean* entry if at capacity. If the pool is full and every page is dirty, raise `BufferPoolFull`. |
| `insert_new_page` | method | `insert_new_page(page_id: int, page: LeafPage \| InternalPage, lsn: int) -> None` | Register a freshly-allocated page in the cache. `page_id` must come from `pager.allocate_page()` and must not already be in the cache. Insert at MRU with `dirty=True` and set `page.last_modified_lsn = lsn`. Evict the LRU clean entry first if at capacity; raise `BufferPoolFull` if every entry is dirty. Does *not* call `pager.read_page` — the disk bytes are zero-filled and would not decode. |
| `mark_dirty` | method | `mark_dirty(page_id: int, lsn: int) -> None` | Set the cached entry's `dirty=True` flag and update its page's `last_modified_lsn = max(current, lsn)`. |
| `flush_all` | method | `flush_all() -> None` | For every dirty entry: `codec.encode_page(page, page_size_bytes)` and `pager.write_page`. Clear the dirty flag. Does not fsync. |
| `dirty_page_ids` | method | `dirty_page_ids() -> list[int]` | For tests and for the auto-checkpoint trigger in iteration 5. |

Internally, each cached entry is a tiny record carrying `(page, dirty: bool)`. Use an `OrderedDict` and `move_to_end` to implement LRU, since `last_modified_lsn` now lives on the page dataclass itself.

#### Bring the buffer pool to life in this order

The buffer pool is a single `OrderedDict` plus a small set of rules. Each cached entry is:

```python
@dataclass
class CachedPage:
    page: LeafPage | InternalPage
    dirty: bool
```

The dict is `self._cache: OrderedDict[int, CachedPage]`, used by the convention **leftmost entry is the LRU, rightmost is the MRU**. Every method below maintains that ordering by inserting and promoting to the right end and only evicting from the left.

This is a **NO-STEAL** buffer pool: dirty pages are *pinned* in memory until `flush_all` writes them out. Eviction itself never calls the pager — clean means "on-disk bytes already match the in-memory copy", so dropping a clean entry is free. The WAL ordering rule mentioned in *Why `last_modified_lsn` earns its place now* is satisfied trivially as a consequence: nothing in this iteration writes a dirty data page to the pager except `flush_all`, and `flush_all` is only called from `close()` and (in iteration 5) the checkpoint.

1. **`get(page_id)` — cache hit.** `page_id in self._cache`. Call `self._cache.move_to_end(page_id)` to promote the entry to the MRU end, then return `self._cache[page_id].page`. The `move_to_end` is what makes the policy LRU rather than FIFO: every read counts as a use.

2. **`get(page_id)` — cache miss, room available.** `page_id not in self._cache` and `len(self._cache) < self.capacity_pages`. Read the page through the pager and decode it:

   ```python
   buf = self.pager.read_page(page_id)
   page = codec.decode_page(buf)
   self._cache[page_id] = CachedPage(page=page, dirty=False)
   return page
   ```

   The fresh entry lands at the MRU end automatically because `OrderedDict` inserts at the right.

3. **`get(page_id)` — cache miss, at capacity.** Before inserting, evict the **leftmost clean** entry. Iterate `self._cache.items()` from the left; the first entry whose `dirty` is `False` is the victim, `del self._cache[victim_id]`. Then load and insert the new page exactly as in step 2.

   If the scan finishes without finding a clean entry — every page in the pool is dirty — raise `BufferPoolFull`. In iteration 4 this is a real failure mode if the dirty working set exceeds capacity; iteration 5's auto-checkpoint trigger keeps it from happening in practice by flushing before the pool fills.

   Worked example with capacity 3:

   ```
   State:                  [page=1 clean, page=2 dirty, page=3 clean]
                            (LRU end →)                          (← MRU end)

   Call get(page_id=4):
     pool is full → scan from the left for the first clean entry.
     page 1 is clean → page 1 is the victim. del self._cache[1].

   State after eviction:   [page=2 dirty, page=3 clean]

   Load page 4 from pager, decode, insert.

   State after insert:     [page=2 dirty, page=3 clean, page=4 clean]
   ```

   Note that page 2 stayed in the pool even though it was older than page 3, because it was dirty. That's the pinning rule in action.

4. **`insert_new_page(page_id, page, lsn)`.** Called by the tree after `pager.allocate_page()` returns a fresh page id during a split, redistribute, merge, or root creation. The pager extends the file with a zero-filled page at the new id, so a normal `get(new_id)` would fail at `codec.decode_page` — zero bytes are not a valid page. Instead, the tree constructs the dataclass in memory and hands it to the pool directly:

   ```python
   new_leaf_id = self.pager.allocate_page()
   new_leaf = LeafPage(right_sibling_page_id=..., slots=...)
   self.buffer_pool.insert_new_page(new_leaf_id, new_leaf, current_lsn)
   ```

   Internally, this is the eviction half of step 3 followed by a direct insert, with no `pager.read_page`:

   - If `len(self._cache) >= self.capacity_pages`, evict the leftmost clean entry (same scan as step 3); raise `BufferPoolFull` if every entry is dirty.
   - `self._cache[page_id] = CachedPage(page=page, dirty=True)` — lands at MRU, born dirty.
   - `page.last_modified_lsn = lsn`.

   The page is born `dirty=True` because its bytes have not yet reached disk; the zero-filled bytes the pager wrote on `allocate_page` are placeholder content that will be overwritten by the next `flush_all`. The tree therefore does *not* call `mark_dirty` afterwards — `insert_new_page` already covers both registration and the LSN.

5. **`mark_dirty(page_id, lsn)`.** The tree calls this after every mutation of an *existing* page (one it loaded via `get`). The entry *must* already be in the cache — the tree only mutates pages it just `get`-ed, so `mark_dirty` is always immediately preceded by a `get` on the same id. Look up the entry, set `entry.dirty = True`, and update the LSN:

   ```python
   entry = self._cache[page_id]
   entry.dirty = True
   entry.page.last_modified_lsn = max(entry.page.last_modified_lsn, lsn)
   ```

   The `max` matters: a single tree operation can touch the same page under two different WAL LSNs (rare, but a split that re-mutates the original leaf is one path). The page's recorded LSN must reflect the *latest* WAL record that touched it, never an earlier one.

   Do **not** `move_to_end` here. The page is already at the MRU end (the `get` that preceded this call just promoted it), and writes shouldn't double-count as accesses.

6. **`flush_all()`.** Walk every entry, encode and write the dirty ones, clear the dirty bit:

   ```python
   for page_id, entry in self._cache.items():
       if entry.dirty:
           buf = codec.encode_page(entry.page, self.pager.page_size_bytes)
           self.pager.write_page(page_id, buf)
           entry.dirty = False
   ```

   Do not fsync, do not evict, do not reorder. `flush_all`'s only job is to push dirty bytes to the pager. The actual fsync happens later, in `pager.fsync()` called by `db.close()`. The LRU order is preserved on purpose: a page that was hot before the flush is still likely to be hot after it.

7. **`dirty_page_ids()` and `dirty_count`.** Both are derived on demand from the cache: a list comprehension and a `sum(1 for ...)` respectively. At iteration-4 cache sizes (256 pages by default) this is cheap, and computing it from the source of truth avoids the class of bugs where a separately-maintained counter drifts out of sync with the dict. Iteration 5's auto-checkpoint trigger reads `dirty_count` once per `put`/`delete`, which is still trivial.

#### Wiring

In `tree.py`, there are three patterns to translate:

- **Read an existing page.** Replace every `pager.read_page` + `codec.decode_page` with a single `buffer_pool.get(page_id)`.
- **Mutate an existing page.** Replace every "encode and write" at the end of a mutation with a single `buffer_pool.mark_dirty(page_id, current_lsn)`.
- **Allocate a fresh page** (during a leaf split, internal split, redistribute that needs a new sibling, or root creation). Replace `pager.allocate_page() → construct dataclass → encode → pager.write_page` with `pager.allocate_page() → construct dataclass → buffer_pool.insert_new_page(new_id, page, current_lsn)`. The pager is still the one that bumps `next_page_id`; the buffer pool is what registers the in-memory dataclass.

The current LSN is the LSN returned by `wal.append_put` / `wal.append_delete`, which `DB.put` / `DB.delete` now thread into `tree.insert` / `tree.delete`. Meta updates — for example, `pager.update_meta(root_page_id=new_root_id)` on a root split — go through the pager unchanged, as described in the *What you build* section.

In `db.py`:

- Construct a `BufferPool(pager, cache_capacity_pages)` in `open()`.
- Pass *both* the pager and the buffer pool to `BTree`. The new constructor is `BTree(pager: Pager, buffer_pool: BufferPool)`. The pager is still needed for meta operations (`allocate_page`, `get_meta`, `update_meta`); the buffer pool is the new path for tree page reads and writes.
- `close()` calls `buffer_pool.flush_all()` before `pager.flush_meta()` and `pager.fsync()`, so all dirty pages are persisted on shutdown.
- The `_debug` namespace gains `dirty_pages_in_cache()` and `total_pages_in_file()` for tests.

### Tests that earn the milestone

- All Iteration 1, 2, 3 tests still pass.
- New unit tests for the buffer pool (`test_cache.py`):
  - Cache hit: `get` followed by `get` of the same id calls the pager exactly once.
  - LRU order: with capacity 3, accessing pages [1,2,3,4] evicts page 1.
  - Touch promotes to MRU: with capacity 3, sequence [1,2,3,1,4] evicts page 2.
  - Dirty pages are pinned: at capacity, dirty pages [1,2,3], `get` page 4 raises `BufferPoolFull`.
  - `insert_new_page` registers a fresh page as MRU and dirty, with `last_modified_lsn` set, and does not call `pager.read_page`. At capacity with one clean entry, `insert_new_page` evicts it; with all entries dirty, it raises `BufferPoolFull`.
  - `mark_dirty` updates `last_modified_lsn` to the highest LSN passed.
  - `flush_all` writes every dirty page to the pager exactly once and clears the dirty bits.
  - Eviction never writes to the pager (only `flush_all` does).
- New informal benchmark in `tests/integration/test_perf_smoke.py` (not a strict assertion, just a sanity check that puts are now meaningfully faster than in Iteration 3): time a `for _ in range(10000): db.put(...)` loop. It should take seconds, not minutes.

### What you have at the end

The same database as Iteration 3, but **fast enough to actually use**. Repeated reads of the same page hit memory; the slow Iteration-3 benchmark goes from "minutes" to "seconds." Suggested commit: `feat(cache): LRU buffer pool with dirty pinning and last_modified_lsn`.

### The flaw you'll feel next

The WAL still grows forever. You haven't fixed that yet — you've only fixed the slowness. Open a long-running DB, watch `bptreedb.wal` grow, restart the DB, watch the open take longer and longer as the WAL replay does more work. The fix is checkpoints.

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

#### Add to `entities.py`

```python
@dataclass
class WALCheckpointRecord(WALRecord):
    root_page_id: int
    freelist_head: int      # Always 0 in iteration 5; iteration 6 will populate it.
    next_page_id: int
```

The `freelist_head` field is included even though the freelist doesn't exist yet — it's set to `0` for now and iteration 6 will populate it without needing to change the WAL record format. Adding a field to a WAL record format after the fact would require a version bump; future-proofing the payload by one field is cheaper.

#### Add to `codec.py`

A new op type `0x03 CHECKPOINT` in `encode_wal_record` / `decode_wal_record`. CHECKPOINT payload: `root_page_id(8) | freelist_head(8) | next_page_id(8)`.

Grow `MetaPage` in `entities.py` with a `last_checkpoint_lsn: int = 0` field, and update `encode_meta_page` / `decode_meta_page` to include it. Insert the new field after `next_page_id` in the wire format; the CRC moves to make room. The total header grows from 36 bytes (iteration 3) to 44 bytes. Update the codec tests for the new layout. (Iteration 6 will add one more meta-page field, `freelist_head`.)

Why `last_checkpoint_lsn` earns its place *now*: recovery needs to know which WAL records are already reflected in the data file (and must be skipped) and which are not (and must be replayed). The meta page is the place to record this because it is the entry point for `open()`.

#### Grow `wal.py`

| name | kind | signature | behaviour |
|---|---|---|---|
| `append_checkpoint` | method | `append_checkpoint(root_page_id: int, freelist_head: int, next_page_id: int) -> int` | Build a `WALCheckpointRecord`, encode, append, fsync, return its LSN. |
| `truncate_before` | method | `truncate_before(lsn: int) -> None` | Discard records with `lsn < given_lsn`. Implementation: open `bptreedb.wal.new`, copy any records `≥ lsn` (usually just the CHECKPOINT marker itself), `fs.fsync_file`, `os.replace` over `bptreedb.wal`, `fs.fsync_directory` on the parent. Reopen the underlying file handle. |
| `size_bytes` | property | `int` | Current on-disk size of the log file. |

On `open()`, also check for a stale `bptreedb.wal.new` file: if it exists, remove it (a previous `truncate_before` crashed mid-rotation; the live `bptreedb.wal` is still authoritative).

#### The `checkpoint()` procedure in `DB`

The order of these steps is load-bearing. If you crash between any two of them, the next `open()` must produce a consistent database. The spec's §6.5 walks through the crash analysis; here's just the procedure itself.

1. `ckpt_lsn = wal.current_lsn + 1` — the LSN the next record will use. All records *before* this point are about to be persisted; everything from this point on belongs to the next checkpoint window.
2. `buffer_pool.flush_all()` — every dirty page now lives in the data file buffer (but not yet on disk).
3. `pager.fsync()` — everything described by WAL records with `lsn < ckpt_lsn` is now durably in the data file.
4. `wal.append_checkpoint(root_page_id, 0, next_page_id)`. This append calls `wal.fsync()` internally.
5. `pager.update_meta(last_checkpoint_lsn=ckpt_lsn, root_page_id=..., next_page_id=...)`, then `pager.flush_meta()`, then `pager.fsync()`.
6. `wal.truncate_before(ckpt_lsn)`. The CHECKPOINT marker itself at `ckpt_lsn` is retained as the new "first record."

**The load-bearing correctness property** comes from the NO-STEAL policy: because no dirty pages were ever written outside of a checkpoint, the data file at recovery time is *always either entirely pre- or entirely post- a given checkpoint*. There is no "half-applied" intermediate state. This is why `bptreedb` uses NO-STEAL rather than the more performant STEAL policies that production engines use.

#### Automatic checkpoint triggers

After every `put` and `delete`, check whether any of these are true, and if so, call `checkpoint()`:

- `wal.size_bytes > checkpoint_wal_size_bytes`
- `buffer_pool.dirty_count / cache_capacity_pages > checkpoint_dirty_page_ratio`

Additionally:

- When `tree.insert` or `tree.delete` raises `BufferPoolFull`, call `checkpoint()` and retry the operation once.
- When `db.close()` is called, take a final checkpoint.

#### The recovery procedure in `DB.open()`

1. The pager has already validated the meta page during its `open()`. (If the CRC fails, `DBCorruptedError` is raised there.)
2. Open the WAL. Call `wal.replay(self._apply_wal_record_for_recovery)`.
3. In the recovery callback, skip records with `lsn ≤ meta.last_checkpoint_lsn` — those are already reflected in the data file. For remaining PUT / DELETE records, call `tree.insert` / `tree.delete`. CHECKPOINT records are always skipped.
4. **Disable buffer-pool eviction during recovery** — pass a flag to the buffer pool (or temporarily set its capacity to effectively infinite) so the pool can grow unbounded during replay without raising `BufferPoolFull`. A crash during recovery would leak partially-applied state into the data file if eviction were allowed.
5. After replay, restore the cap and immediately call `checkpoint()` to persist the recovered state.

### Tests that earn the milestone

- All previous tests still pass.
- `test_wal.py` gains tests for the new CHECKPOINT op type and for `truncate_before`:
  - Encode/decode CHECKPOINT round-trip.
  - `truncate_before(lsn)` removes all earlier records and preserves all later ones.
  - Replay after truncation yields only the preserved records.
  - Crash safety of the rotation: hand-craft a `bptreedb.wal` and a stale `bptreedb.wal.new`; opening the WAL ignores or removes the stale `.new` and leaves `bptreedb.wal` intact.
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

Delete a few thousand keys from a populated database and watch `bptreedb.data` *not shrink*. Pages that the tree has merged out of existence are leaked — they remain in the file forever, taking up space, never reused. The fix is a freelist.

---

## Iteration 6 — Freelist

### Goal

Reuse pages that have been freed by tree merges, instead of leaking them. Bound the on-disk file size to roughly the working-set size.

### Why now

Because Iteration 5 left you with a file that grows monotonically — there is no way for the tree to give back a page. Every leaf merge frees a page id that nobody will ever pop. After enough churn, the file is mostly garbage. The fix is a freelist of free page ids that the pager pops from before bump-allocating a new page.

### What you build

#### Add to `entities.py`

```python
@dataclass
class FreelistPage:
    last_modified_lsn: int
    next_freelist_page_id: int            # 0 if this is the tail
    freed_page_ids: list[int]
```

Add a `freelist_head: int = 0` field to `MetaPage`.

#### Grow `codec.py`

Add a third page type tag, `0x03 FREELIST`. A freelist page is *not* a slotted page — it has a simpler layout:

- The same 32-byte page header as leaves and internal nodes, with `type_tag = 0x03` and `num_slots` repurposed to mean "number of freed page ids stored on this page." The `right_sibling_page_id` slot (offset 24) is repurposed to hold `next_freelist_page_id`, which keeps the header a single fixed shape.
- After the header, an array of 8-byte freed page ids.

Expose `max_freed_ids_per_freelist_page(page_size_bytes) -> int = (page_size_bytes - 32) // 8`.

Extend `encode_page` and `decode_page` to dispatch on `type_tag` and handle `FreelistPage` as a third case. Update `encode_meta_page` / `decode_meta_page` to include `freelist_head`; the total header grows from 44 bytes (iteration 5) to 52 bytes.

Also: in `codec.encode_wal_record`'s CHECKPOINT branch, populate the `freelist_head` field with the real value from the `WALCheckpointRecord` dataclass (which iteration 5 was always setting to `0`).

#### Grow `pager.py`

Modify `allocate_page` and add `free_page`:

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

Quick map of features to iterations, so you can verify that nothing is unimplemented at the end:

| Feature                                                | Iteration where it earns its place                           |
|--------------------------------------------------------|--------------------------------------------------------------|
| Directory layout (`bptreedb.wal`, `bptreedb.data`)     | Iter 2 (WAL), Iter 3 (data file)                             |
| `MetaPage` dataclass and wire format                   | Iter 3 (initial), Iter 5 (`last_checkpoint_lsn`), Iter 6 (`freelist_head`) |
| Slotted page header                                    | Iter 3 (initial 24-byte header), Iter 4 (32-byte with `last_modified_lsn`) |
| Freelist page                                          | Iter 6                                                       |
| Record size limit and `DBRecordTooLargeError`          | Iter 3                                                       |
| WAL framing (PUT/DELETE/CHECKPOINT)                    | Iter 2 (PUT/DELETE), Iter 5 (CHECKPOINT)                     |
| Tree invariants (`assert_tree_invariants`)             | Iter 3                                                       |
| `search` / `insert` with splits                        | Iter 3                                                       |
| `delete` with merge / redistribute                     | Iter 3 (algorithm), Iter 6 (freelist integration)            |
| `scan` with version check across leaves                | Iter 3                                                       |
| What gets logged (one WAL record per public mutation)  | Iter 2                                                       |
| Determinism discipline                                 | Iter 3 onward; verified by the crash property test           |
| NO-STEAL buffer-pool policy                            | Iter 4                                                       |
| LSN bookkeeping on pages                               | Iter 4                                                       |
| Recovery (WAL replay)                                  | Iter 2 (initial), Iter 5 (with checkpoint cursor)            |
| Checkpoint procedure                                   | Iter 5                                                       |
| Crash-window analysis                                  | Iter 5 (validated by crash property test)                    |
| `DB` API: `put` / `get` / `delete` / `scan` / `close` / context manager | Iter 1                                      |
| `DB(data_dir)` parameter                               | Iter 2                                                       |
| `DB(..., page_size_bytes=...)` parameter               | Iter 3                                                       |
| `DB(..., cache_capacity_pages=...)` parameter          | Iter 4                                                       |
| `db.checkpoint()` + checkpoint parameters              | Iter 5                                                       |
| `DBClosedError`                                        | Iter 1                                                       |
| `DBCorruptedError`, `DBChecksumError`                  | Iter 2                                                       |
| `DBRecordTooLargeError`                                | Iter 3                                                       |
| `DBConcurrentPageModificationError`                    | Iter 3 (deferred from Iter 1 — earned only once `scan` is a real lazy iterator) |
| `_debug` namespace                                     | Iter 3 onward, growing as needed                             |
| Unit + property tests                                  | Iter 1                                                       |
| `FaultyFile` crash-test infrastructure                 | Iter 2                                                       |
