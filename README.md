# bptreedb

An educational implementation of a database engine based on B+ Trees.

[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

`bptreedb` is an embedded, single-writer key-value store written in Python. On disk, it's a binary-encoded B+ tree with a write-ahead log (WAL) and crash recovery.
Its closest real-world cousin is [LMDB](https://en.wikipedia.org/wiki/Lightning_Memory-Mapped_Database).

## Background

I wanted to understand how database engines actually work end-to-end, from query to disk: paged
files, slotted nodes, B+ tree housekeeping, buffer pools, write-ahead logging, durability, crash
recovery. Reading about this stuff only gets you so far. Implementing one is different.

So I treated it as a learning project with a twist. I used an AI agent to write a thorough
design specification and an iteration-by-iteration implementation plan (both live in `docs`).
Then I wrote all the Python code myself, working through the plan one iteration at a time,
and used the AI as a reviewer and rubber duck along the way.

## Features

- `put`, `get`, `delete`, `scan` on arbitrary `bytes` keys and values.
- Lexicographic key ordering, making range queries fast.
- B+ tree clustered index with auto-balancing (split, merge, slot redistribution).
- Write-ahead log, CRC-protected and fsynced on each write operation for strong durability.
- Buffer pool with LRU eviction and dirty page tracking. NO-STEAL / FORCE-AT-CHECKPOINT policy.
- Crash recovery: replays the WAL from the last checkpoint on open.
- Configurable page size (default 4 KiB). Slotted pages with variable-length records.
- Page reuse via a singly-linked freelist.
- Tested with Hypothesis-driven property tests and crash tests introducing random I/O errors.

## Out of Scope

Some features often found in production-grade databases were deliberately left out of scope
for simplicity and code readability. See [the production readiness gap analysis](docs/bptreedb-production-readiness-gap-analysis.md) document for details.

- **No transactions.** Each `put` and `delete` is its own atomic, durable unit.
- **No concurrency.** One thread, one writer, one reader, everything serialized.
- **No SQL, schemas, or secondary indexes.** Just an ordered key-value API. Real SQL engines like CockroachDB or TiDB put a relational layer on top of exactly this kind of KV backend.
- **Max record size: ~20% of the page size.** Oversized values are rejected outright.
- **Single meta page, no ping-pong.** If the meta page suffers a torn write, it is unrecoverable.
- **Logical WAL only.** Records describe `PUT(k, v)` / `DELETE(k)`, not byte-level page deltas. This is sound only because we're single-threaded and NO-STEAL.
- **No compression, encryption, replication, or networking.** Out of scope by a wide margin.

## Code Usage

```python
from bptreedb import DB

with DB("path/to/dir") as db:
    db.put(b"hello", b"world")
    print(db.get(b"hello"))
    for k, v in db.scan(start_key_inclusive=b"a", end_key_exclusive=b"z"):
        ...
```

## Development

```shell
uv sync      # install project dependencies
make check   # linter, type checker, tests
```

`scripts/bench.py` runs configurable workloads against a fresh DB and reports throughput, under-the-hood
statistics, and disk footprint. Run `uv run python scripts/bench.py --help` for the presets and flags.

## License

The source code is licensed under the [MIT License](LICENSE).
