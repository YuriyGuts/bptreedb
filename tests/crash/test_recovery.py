"""
Crash-recovery tests for the WAL-backed DB.

The hand-built tests cover specific scenarios (clean crash, torn-tail truncation).
The Hypothesis property test is the heart of the suite: it generates arbitrary operation
sequences, crashes mid-stream, and asserts that every acknowledged write is recoverable
on reopen.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from hypothesis import HealthCheck
from hypothesis import given
from hypothesis import settings
from hypothesis import strategies as st

from bptreedb.db import DB
from bptreedb.db import WAL_FILENAME
from bptreedb.fs import _fsync_fd
from tests.crash.conftest import FaultyFile
from tests.crash.conftest import FaultyFileFixture

# A small alphabet keeps key collisions frequent so the property test exercises
# overwrites and deletes against existing keys, not just inserts.
_KEY_BYTES = st.binary(min_size=1, max_size=4)
_VALUE_BYTES = st.binary(min_size=0, max_size=4)

_op_strategy = st.one_of(
    st.tuples(st.just("put"), _KEY_BYTES, _VALUE_BYTES),
    st.tuples(st.just("delete"), _KEY_BYTES),
)


def test_two_puts_survive_crash(tmp_path: Path, faulty_files: FaultyFileFixture) -> None:
    # GIVEN a DB with two keys, opened against the FaultyFile factory
    db = DB(tmp_path)
    db.open()
    db.put(b"foo", b"\x01")
    db.put(b"bar", b"\x02")

    # WHEN the process "crashes" before close
    faulty_files.crash_all()

    # THEN reopening the DB recovers both keys
    with DB(tmp_path) as recovered:
        assert recovered.get(b"foo") == b"\x01"
        assert recovered.get(b"bar") == b"\x02"


def test_delete_survives_crash(tmp_path: Path, faulty_files: FaultyFileFixture) -> None:
    # GIVEN a DB with three keys, one of which is then deleted
    db = DB(tmp_path)
    db.open()
    db.put(b"foo", b"\x01")
    db.put(b"bar", b"\x02")
    db.put(b"baz", b"\x03")
    db.delete(b"bar")

    # WHEN the process crashes
    faulty_files.crash_all()

    # THEN the deleted key stays gone after reopen
    with DB(tmp_path) as recovered:
        assert recovered.get(b"foo") == b"\x01"
        assert recovered.get(b"bar") is None
        assert recovered.get(b"baz") == b"\x03"


def test_partial_record_truncated_on_reopen(
    tmp_path: Path, faulty_files: FaultyFileFixture
) -> None:
    # The simulated crash skips the close-time checkpoint that would otherwise persist `foo` to the
    # data file. We need the `put` to be living only in the WAL so the torn-tail truncation actually
    # drops it.

    # GIVEN a DB with one un-checkpointed put, crashed, with its on-disk WAL torn by one byte
    db = DB(tmp_path)
    db.open()
    db.put(b"foo", b"\x01")
    faulty_files.crash_all()
    wal_path = tmp_path / WAL_FILENAME
    wal_path.write_bytes(wal_path.read_bytes()[:-1])

    # WHEN reopening the DB
    # THEN the torn record is dropped on replay and the key is gone
    with DB(tmp_path) as recovered:
        assert recovered.get(b"foo") is None


@given(
    ops=st.lists(_op_strategy, min_size=0, max_size=30),
    crash_at=st.integers(min_value=0, max_value=30),
)
@settings(
    max_examples=200,
    deadline=None,
    # Hypothesis dislikes function-scoped fixtures inside `@given` because the
    # fixture is set up once per test function, not once per example. We work
    # around this by managing the `FaultyFile` state manually inside the test
    # body. The `monkeypatch` fixture below is the only piece of pytest
    # plumbing we need, and it's safe to reuse across examples.
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_acknowledged_writes_survive_crash(
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
    ops: list[tuple],
    crash_at: int,
) -> None:
    # GIVEN a fresh temp directory and a fresh fault-injection fixture for this example
    tmp_path = tmp_path_factory.mktemp("crash_recovery_property")
    fixture = _install_fault_injection(monkeypatch)

    db = DB(tmp_path)
    db.open()

    # WHEN we apply a prefix of the operation list, recording each acknowledged
    # mutation in the expected dict
    expected: dict[bytes, bytes] = {}
    crash_index = min(crash_at, len(ops))
    for op in ops[:crash_index]:
        match op:
            case ("put", key, value):
                db.put(key, value)
                expected[key] = value
            case ("delete", key):
                db.delete(key)
                expected.pop(key, None)

    # WHEN the simulated crash fires
    fixture.crash_all()
    # NOTE: we deliberately do NOT call `db.close()`. A real crashed process
    # would have its file handles forcibly closed by the OS, not gracefully
    # flushed by application code.

    # WHEN we re-open the DB with the *real* `open` and `_fsync_fd`
    # (the monkeypatch is undone below by reusing a fresh fixture per example)
    monkeypatch.undo()
    with DB(tmp_path) as recovered:
        actual = dict(recovered.scan(None, None))

    # THEN every acknowledged write is recoverable and nothing else exists
    assert actual == expected


def _install_fault_injection(monkeypatch: pytest.MonkeyPatch) -> FaultyFileFixture:
    """Install the same patches as the `faulty_files` fixture, but inside a
    test that's already inside `@given` (where the fixture's once-per-function
    lifetime would otherwise leak state across Hypothesis examples)."""
    fixture = FaultyFileFixture()
    real_fsync_fd = _fsync_fd

    def faulty_open(path, mode, *args, **kwargs):
        # See `tests/crash/conftest.py` for why read-only opens bypass the
        # wrapper: a read-only FaultyFile would be crashed back to a stale
        # snapshot, clobbering writes made through the writable handle.
        if "w" not in mode and "a" not in mode and "+" not in mode:
            return open(path, mode, *args, **kwargs)
        faulty_file = FaultyFile(path, mode)
        fixture.register(faulty_file)
        return faulty_file

    def patched_fsync_fd(fd):
        if isinstance(fd, FaultyFile):
            fd.record_fsync()
        real_fsync_fd(fd)

    monkeypatch.setattr("bptreedb.wal.open", faulty_open, raising=False)
    monkeypatch.setattr("bptreedb.pager.open", faulty_open, raising=False)
    monkeypatch.setattr("bptreedb.fs._fsync_fd", patched_fsync_fd)
    return fixture
