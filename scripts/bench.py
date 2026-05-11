"""
Performance benchmark and demo for `bptreedb`.

Run a named workload preset against a fresh DB and report throughput, latency,
tree shape, disk footprint, and structural counters.

Examples
--------
    uv run python scripts/bench.py random-put
    uv run python scripts/bench.py random-put --n-ops 10000 --value-size 128
    uv run python scripts/bench.py mixed-rw --read-pct 0.9
    uv run python scripts/bench.py --all --page-size 1024
    uv run python scripts/bench.py random-put --json

    Quick sweep (every preset, ~45s total, no phase runs for long):
    uv run python scripts/bench.py --all --n-ops 1000 --preload-n 500 --n-scans 20 --steady-size 300
"""

from __future__ import annotations

import argparse
import json
import random
import shutil
import sys
import time
from dataclasses import asdict
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path

from bptreedb.cache import BufferPoolStats
from bptreedb.codec import calculate_page_size
from bptreedb.db import DB
from bptreedb.db import PAGER_FILENAME
from bptreedb.db import WAL_FILENAME
from bptreedb.debug import bfs_walk_tree
from bptreedb.entities import InternalPage
from bptreedb.entities import LeafPage
from bptreedb.pager import PagerStats
from bptreedb.tree import TreeStats
from bptreedb.wal import WALStats

DEFAULT_PAGE_SIZE = 4096
DEFAULT_DATA_DIR = Path("./bench-data")
PROGRESS_INTERVAL_SECONDS = 1.0
# Widest bracketed label is "[sequential-put:measure]" (24 chars); one extra for breathing room.
PROGRESS_LABEL_WIDTH = 25


@dataclass
class WorkloadParams:
    n_ops: int = 100_000
    key_size: int = 16
    value_size: int = 64
    read_pct: float = 0.0
    delete_pct: float = 0.0
    preload_n: int = 0
    n_scans: int = 0
    scan_limit: int | None = None
    steady_size: int = 0
    seed: int = 42


@dataclass
class TreeShape:
    depth: int
    leaf_pages: int
    internal_pages: int
    avg_leaf_fill: float


@dataclass
class WorkloadResult:
    preset_name: str
    params: WorkloadParams
    page_size_bytes: int
    wall_seconds: float
    n_measured_ops: int
    user_bytes_written: int
    latencies_ns: list[int] = field(repr=False)
    pager_stats: PagerStats = field(default_factory=PagerStats)
    tree_stats: TreeStats = field(default_factory=TreeStats)
    wal_stats: WALStats = field(default_factory=WALStats)
    buffer_pool_stats: BufferPoolStats = field(default_factory=BufferPoolStats)
    data_file_bytes: int = 0
    wal_file_bytes: int = 0
    tree_shape: TreeShape = field(default_factory=lambda: TreeShape(0, 0, 0, 0.0))


PRESETS: dict[str, WorkloadParams] = {
    "sequential-put": WorkloadParams(n_ops=100_000),
    "random-put": WorkloadParams(n_ops=100_000),
    "mixed-rw": WorkloadParams(n_ops=100_000, preload_n=20_000, read_pct=0.8, delete_pct=0.05),
    "scan-heavy": WorkloadParams(preload_n=50_000, n_ops=0, n_scans=100),
    "delete-churn": WorkloadParams(
        preload_n=50_000, n_ops=100_000, steady_size=50_000, delete_pct=0.5
    ),
    "bulk-load": WorkloadParams(n_ops=200_000),
}


def _make_sequential_key(i: int, size: int) -> bytes:
    raw = f"{i:0{size}d}".encode()
    return raw[:size] if len(raw) >= size else raw.rjust(size, b"0")


def _make_random_key(rng: random.Random, size: int) -> bytes:
    return rng.randbytes(size)


def _make_value(rng: random.Random, size: int) -> bytes:
    return rng.randbytes(size)


class ProgressReporter:
    def __init__(self, label: str, total: int, *, enabled: bool) -> None:
        self._label = label
        self._total = total
        self._enabled = enabled
        self._last_tick = time.perf_counter()
        self._start = self._last_tick

    def tick(self, done: int) -> None:
        if not self._enabled or self._total <= 0:
            return
        now = time.perf_counter()
        if now - self._last_tick < PROGRESS_INTERVAL_SECONDS:
            return
        self._last_tick = now
        elapsed = now - self._start
        rate = done / elapsed if elapsed > 0 else 0.0
        pct = 100.0 * done / self._total
        bracketed = f"[{self._label}]".ljust(PROGRESS_LABEL_WIDTH)
        print(
            f"\r{bracketed}{done:>9,}/{self._total:>9,} ({pct:5.1f}%)  {rate:>10,.0f} ops/s",
            end="",
            file=sys.stderr,
        )

    def finish(self) -> None:
        if self._enabled and self._total > 0:
            print("", file=sys.stderr)


@dataclass
class RunContext:
    preset_name: str
    show_progress: bool

    def progress_for(self, phase: str, total: int) -> ProgressReporter:
        return ProgressReporter(f"{self.preset_name}:{phase}", total, enabled=self.show_progress)


def _run_sequential_put(
    db: DB, params: WorkloadParams, _rng: random.Random, ctx: RunContext
) -> tuple[list[int], int]:
    progress = ctx.progress_for("measure", params.n_ops)
    latencies: list[int] = []
    user_bytes = 0
    value = b"v" * params.value_size
    for i in range(params.n_ops):
        key = _make_sequential_key(i, params.key_size)
        t0 = time.perf_counter_ns()
        db.put(key, value)
        latencies.append(time.perf_counter_ns() - t0)
        user_bytes += len(key) + len(value)
        progress.tick(i + 1)
    progress.finish()
    return latencies, user_bytes


def _run_random_put(
    db: DB, params: WorkloadParams, rng: random.Random, ctx: RunContext
) -> tuple[list[int], int]:
    progress = ctx.progress_for("measure", params.n_ops)
    latencies: list[int] = []
    user_bytes = 0
    for i in range(params.n_ops):
        key = _make_random_key(rng, params.key_size)
        value = _make_value(rng, params.value_size)
        t0 = time.perf_counter_ns()
        db.put(key, value)
        latencies.append(time.perf_counter_ns() - t0)
        user_bytes += len(key) + len(value)
        progress.tick(i + 1)
    progress.finish()
    return latencies, user_bytes


def _preload(
    db: DB, n: int, params: WorkloadParams, rng: random.Random, ctx: RunContext
) -> list[bytes]:
    progress = ctx.progress_for("preload", n)
    live_keys: list[bytes] = []
    for i in range(n):
        key = _make_random_key(rng, params.key_size)
        value = _make_value(rng, params.value_size)
        db.put(key, value)
        live_keys.append(key)
        progress.tick(i + 1)
    progress.finish()
    return live_keys


def _reset_stats(db: DB) -> None:
    db.pager.stats.reset()
    db.tree.stats.reset()
    db.wal.stats.reset()
    db.buffer_pool.stats.reset()


def _run_mixed_rw(
    db: DB, params: WorkloadParams, rng: random.Random, ctx: RunContext
) -> tuple[list[int], int]:
    live_keys = _preload(db, params.preload_n, params, rng, ctx)
    # Reset stats so the preload phase doesn't contaminate the measured numbers.
    _reset_stats(db)

    progress = ctx.progress_for("measure", params.n_ops)
    latencies: list[int] = []
    user_bytes = 0
    for i in range(params.n_ops):
        roll = rng.random()
        t0 = time.perf_counter_ns()
        if roll < params.read_pct and live_keys:
            db.get(rng.choice(live_keys))
        elif roll < params.read_pct + params.delete_pct and live_keys:
            victim = rng.choice(live_keys)
            if db.delete(victim):
                live_keys.remove(victim)
        else:
            key = _make_random_key(rng, params.key_size)
            value = _make_value(rng, params.value_size)
            db.put(key, value)
            live_keys.append(key)
            user_bytes += len(key) + len(value)
        latencies.append(time.perf_counter_ns() - t0)
        progress.tick(i + 1)
    progress.finish()
    return latencies, user_bytes


def _run_scan_heavy(
    db: DB, params: WorkloadParams, rng: random.Random, ctx: RunContext
) -> tuple[list[int], int]:
    _preload(db, params.preload_n, params, rng, ctx)
    _reset_stats(db)

    progress = ctx.progress_for("measure", params.n_scans)
    latencies: list[int] = []
    for i in range(params.n_scans):
        t0 = time.perf_counter_ns()
        for consumed, _ in enumerate(db.scan(None, None), start=1):
            if params.scan_limit is not None and consumed >= params.scan_limit:
                break
        latencies.append(time.perf_counter_ns() - t0)
        progress.tick(i + 1)
    progress.finish()
    return latencies, 0


def _run_delete_churn(
    db: DB, params: WorkloadParams, rng: random.Random, ctx: RunContext
) -> tuple[list[int], int]:
    live_keys = _preload(db, params.preload_n, params, rng, ctx)
    _reset_stats(db)

    progress = ctx.progress_for("measure", params.n_ops)
    latencies: list[int] = []
    user_bytes = 0
    target = params.steady_size
    for i in range(params.n_ops):
        # Tilt toward delete when above target, toward insert when below.
        over = len(live_keys) > target
        roll = rng.random()
        delete_prob = params.delete_pct + (0.2 if over else -0.2)
        t0 = time.perf_counter_ns()
        if live_keys and roll < delete_prob:
            victim = rng.choice(live_keys)
            if db.delete(victim):
                live_keys.remove(victim)
        else:
            key = _make_random_key(rng, params.key_size)
            value = _make_value(rng, params.value_size)
            db.put(key, value)
            live_keys.append(key)
            user_bytes += len(key) + len(value)
        latencies.append(time.perf_counter_ns() - t0)
        progress.tick(i + 1)
    progress.finish()
    return latencies, user_bytes


def _run_bulk_load(
    db: DB, params: WorkloadParams, rng: random.Random, ctx: RunContext
) -> tuple[list[int], int]:
    return _run_random_put(db, params, rng, ctx)


RUNNERS = {
    "sequential-put": _run_sequential_put,
    "random-put": _run_random_put,
    "mixed-rw": _run_mixed_rw,
    "scan-heavy": _run_scan_heavy,
    "delete-churn": _run_delete_churn,
    "bulk-load": _run_bulk_load,
}


def _snapshot_tree_shape(db: DB) -> TreeShape:
    levels = bfs_walk_tree(db.tree)
    if not levels:
        return TreeShape(depth=0, leaf_pages=0, internal_pages=0, avg_leaf_fill=0.0)

    leaf_nodes = levels[-1]
    internal_count = sum(
        1 for level in levels for node in level if isinstance(node.page, InternalPage)
    )
    leaf_count = sum(1 for node in leaf_nodes if isinstance(node.page, LeafPage))
    page_size = db.pager.page_size_bytes
    leaf_fill_fractions = [
        calculate_page_size(node.page) / page_size
        for node in leaf_nodes
        if isinstance(node.page, LeafPage)
    ]
    avg_fill = sum(leaf_fill_fractions) / len(leaf_fill_fractions) if leaf_fill_fractions else 0.0
    return TreeShape(
        depth=len(levels),
        leaf_pages=leaf_count,
        internal_pages=internal_count,
        avg_leaf_fill=avg_fill,
    )


def measure(
    preset_name: str,
    params: WorkloadParams,
    page_size_bytes: int,
    data_dir: Path,
    *,
    show_progress: bool,
) -> WorkloadResult:
    if data_dir.exists():
        shutil.rmtree(data_dir)

    rng = random.Random(params.seed)
    runner = RUNNERS[preset_name]
    ctx = RunContext(preset_name=preset_name, show_progress=show_progress)

    db = DB(data_dir, page_size_bytes=page_size_bytes)
    db.open()
    try:
        t0 = time.perf_counter()
        latencies, user_bytes = runner(db, params, rng, ctx)
        wall = time.perf_counter() - t0

        tree_shape = _snapshot_tree_shape(db)
        # Flush before snapshotting so close-time page writes (which the buffer pool defers
        # to `flush_all`) are reflected in `pager_stats.page_writes`. Otherwise, `write_amp`
        # would dramatically undercount disk traffic for any workload that relies on the cache.
        db.buffer_pool.flush_all()
        pager_stats = PagerStats(**asdict(db.pager.stats))
        tree_stats = TreeStats(**asdict(db.tree.stats))
        wal_stats = WALStats(**asdict(db.wal.stats))
        buffer_pool_stats = BufferPoolStats(**asdict(db.buffer_pool.stats))
    finally:
        db.close()

    data_file_bytes = (data_dir / PAGER_FILENAME).stat().st_size
    wal_file_bytes = (data_dir / WAL_FILENAME).stat().st_size

    return WorkloadResult(
        preset_name=preset_name,
        params=params,
        page_size_bytes=page_size_bytes,
        wall_seconds=wall,
        n_measured_ops=len(latencies),
        user_bytes_written=user_bytes,
        latencies_ns=latencies,
        pager_stats=pager_stats,
        tree_stats=tree_stats,
        wal_stats=wal_stats,
        buffer_pool_stats=buffer_pool_stats,
        data_file_bytes=data_file_bytes,
        wal_file_bytes=wal_file_bytes,
        tree_shape=tree_shape,
    )


def _percentile(sorted_samples: list[int], p: float) -> int:
    if not sorted_samples:
        return 0
    idx = min(int(p * len(sorted_samples)), len(sorted_samples) - 1)
    return sorted_samples[idx]


def _format_bytes(n: int) -> str:
    units = ("B", "KB", "MB", "GB")
    value = float(n)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:,.2f} {unit}"
        value /= 1024
    return f"{n} B"


def _write_amplification(result: WorkloadResult) -> str:
    if result.user_bytes_written == 0:
        return "n/a"
    disk_bytes = (
        result.pager_stats.page_writes * result.page_size_bytes + result.wal_stats.bytes_appended
    )
    ratio = disk_bytes / result.user_bytes_written
    return f"{ratio:.2f}x"


def _cache_hit_ratio(result: WorkloadResult) -> str:
    stats = result.buffer_pool_stats
    total = stats.cache_hits + stats.cache_misses
    if total == 0:
        return "n/a"
    return f"{100.0 * stats.cache_hits / total:.1f}%"


def render_text(result: WorkloadResult) -> str:
    sorted_lat = sorted(result.latencies_ns)
    p50 = _percentile(sorted_lat, 0.50) / 1000
    p95 = _percentile(sorted_lat, 0.95) / 1000
    p99 = _percentile(sorted_lat, 0.99) / 1000
    max_lat = (sorted_lat[-1] / 1000) if sorted_lat else 0.0

    ops_per_sec = result.n_measured_ops / result.wall_seconds if result.wall_seconds > 0 else 0.0
    bytes_per_key = (
        result.data_file_bytes / result.tree_shape.leaf_pages
        if result.tree_shape.leaf_pages
        else 0.0
    )

    p = result.params
    header = (
        f"=== {result.preset_name} "
        f"(n_ops={p.n_ops}, key_size={p.key_size}, value_size={p.value_size}, "
        f"page_size={result.page_size_bytes}) ==="
    )

    lines = [
        header,
        "",
        "Throughput:",
        f"  ops/sec:             {ops_per_sec:>14,.0f}",
        f"  wall time:           {result.wall_seconds:>14.3f}s",
        f"  measured ops:        {result.n_measured_ops:>14,}",
        "",
        "Latency (µs):",
        f"  p50:                 {p50:>14,.1f}",
        f"  p95:                 {p95:>14,.1f}",
        f"  p99:                 {p99:>14,.1f}",
        f"  max:                 {max_lat:>14,.1f}",
        "",
        "Tree shape:",
        f"  depth:               {result.tree_shape.depth:>14}",
        f"  leaf pages:          {result.tree_shape.leaf_pages:>14,}",
        f"  internal pages:      {result.tree_shape.internal_pages:>14,}",
        f"  avg leaf fill:       {result.tree_shape.avg_leaf_fill * 100:>13.1f}%",
        "",
        "Disk:",
        f"  data file:           {_format_bytes(result.data_file_bytes):>14}",
        f"  WAL:                 {_format_bytes(result.wal_file_bytes):>14}",
        f"  bytes/leaf page:     {bytes_per_key:>14,.1f}",
        f"  user bytes written:  {_format_bytes(result.user_bytes_written):>14}",
        f"  write amplification: {_write_amplification(result):>14}",
        "",
        "Pager counters:",
        f"  page reads:          {result.pager_stats.page_reads:>14,}",
        f"  page writes:         {result.pager_stats.page_writes:>14,}",
        f"  pages allocated:     {result.pager_stats.pages_allocated:>14,}",
        f"  meta flushes:        {result.pager_stats.meta_flushes:>14,}",
        f"  fsyncs:              {result.pager_stats.fsyncs:>14,}",
        "",
        "Buffer pool counters:",
        f"  cache hits:          {result.buffer_pool_stats.cache_hits:>14,}",
        f"  cache misses:        {result.buffer_pool_stats.cache_misses:>14,}",
        f"  hit ratio:           {_cache_hit_ratio(result):>14}",
        f"  evictions:           {result.buffer_pool_stats.evictions:>14,}",
        f"  flushes:             {result.buffer_pool_stats.flushes:>14,}",
        f"  dirty pages flushed: {result.buffer_pool_stats.dirty_pages_flushed:>14,}",
        "",
        "Tree counters:",
        f"  leaf splits:         {result.tree_stats.leaf_splits:>14,}",
        f"  internal splits:     {result.tree_stats.internal_splits:>14,}",
        f"  leaf merges:         {result.tree_stats.leaf_merges:>14,}",
        f"  internal merges:     {result.tree_stats.internal_merges:>14,}",
        f"  leaf redistributes:  {result.tree_stats.leaf_redistributes:>14,}",
        f"  internal redistr.:   {result.tree_stats.internal_redistributes:>14,}",
        f"  root collapses:      {result.tree_stats.root_collapses:>14,}",
        "",
        "WAL counters:",
        f"  records appended:    {result.wal_stats.records_appended:>14,}",
        f"  bytes appended:      {_format_bytes(result.wal_stats.bytes_appended):>14}",
        f"  fsyncs:              {result.wal_stats.fsyncs:>14,}",
    ]
    return "\n".join(lines)


def render_json(result: WorkloadResult) -> dict:
    sorted_lat = sorted(result.latencies_ns)
    return {
        "preset": result.preset_name,
        "params": asdict(result.params),
        "page_size_bytes": result.page_size_bytes,
        "throughput": {
            "ops_per_sec": (
                result.n_measured_ops / result.wall_seconds if result.wall_seconds > 0 else 0.0
            ),
            "wall_seconds": result.wall_seconds,
            "n_measured_ops": result.n_measured_ops,
        },
        "latency_ns": {
            "p50": _percentile(sorted_lat, 0.50),
            "p95": _percentile(sorted_lat, 0.95),
            "p99": _percentile(sorted_lat, 0.99),
            "max": sorted_lat[-1] if sorted_lat else 0,
        },
        "tree_shape": asdict(result.tree_shape),
        "disk": {
            "data_file_bytes": result.data_file_bytes,
            "wal_file_bytes": result.wal_file_bytes,
            "user_bytes_written": result.user_bytes_written,
            "write_amplification": _write_amplification(result),
        },
        "pager_stats": asdict(result.pager_stats),
        "tree_stats": asdict(result.tree_stats),
        "wal_stats": asdict(result.wal_stats),
        "buffer_pool_stats": {
            **asdict(result.buffer_pool_stats),
            "hit_ratio": _cache_hit_ratio(result),
        },
    }


def _merge_overrides(base: WorkloadParams, args: argparse.Namespace) -> WorkloadParams:
    merged = WorkloadParams(**asdict(base))
    for name in (
        "n_ops",
        "key_size",
        "value_size",
        "read_pct",
        "delete_pct",
        "preload_n",
        "n_scans",
        "scan_limit",
        "steady_size",
        "seed",
    ):
        value = getattr(args, name)
        if value is not None:
            setattr(merged, name, value)
    return merged


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Benchmark bptreedb against a named workload preset.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "workload",
        nargs="?",
        choices=sorted(PRESETS.keys()),
        help="Preset name. Omit when passing --all.",
    )
    parser.add_argument("--all", action="store_true", help="Run every preset.")
    parser.add_argument("--page-size", type=int, default=DEFAULT_PAGE_SIZE)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    parser.add_argument("--no-progress", action="store_true", help="Suppress progress ticks.")

    # Per-param overrides. default=None so merge logic can tell "not supplied" from "supplied".
    parser.add_argument("--n-ops", type=int, default=None)
    parser.add_argument("--key-size", type=int, default=None)
    parser.add_argument("--value-size", type=int, default=None)
    parser.add_argument("--read-pct", type=float, default=None)
    parser.add_argument("--delete-pct", type=float, default=None)
    parser.add_argument("--preload-n", type=int, default=None)
    parser.add_argument("--n-scans", type=int, default=None)
    parser.add_argument("--scan-limit", type=int, default=None)
    parser.add_argument("--steady-size", type=int, default=None)
    parser.add_argument("--seed", type=int, default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if not args.all and not args.workload:
        parser.error("Specify a workload preset or pass --all.")

    preset_names = sorted(PRESETS) if args.all else [args.workload]

    results = [
        measure(
            name,
            _merge_overrides(PRESETS[name], args),
            page_size_bytes=args.page_size,
            data_dir=args.data_dir,
            show_progress=not args.no_progress,
        )
        for name in preset_names
    ]

    if args.json:
        print(json.dumps([render_json(r) for r in results], indent=2))
    else:
        for result in results:
            print(render_text(result))
            print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
