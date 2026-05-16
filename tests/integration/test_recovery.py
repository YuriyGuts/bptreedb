from bptreedb.db import DB


def test_reopen_after_clean_close_is_no_op(tmp_path):
    # GIVEN a DB closed cleanly after 5 puts (close runs a final checkpoint)
    with DB(tmp_path) as db:
        for i in range(5):
            db.put(bytes([i]), b"v")

    # WHEN reopening
    with DB(tmp_path) as recovered:
        # THEN the WAL has only the CHECKPOINT marker, no PUTs to replay
        assert recovered.wal.stats.records_replayed == 1
        for i in range(5):
            assert recovered.get(bytes([i])) == b"v"


def test_reopen_after_dirty_shutdown_replays_writes(tmp_path):
    # GIVEN 5 puts with no close (models a process death before the close-time checkpoint)
    db = DB(tmp_path)
    db.open()
    for i in range(5):
        db.put(bytes([i]), b"v")

    # WHEN reopening
    with DB(tmp_path) as recovered:
        # THEN all 5 puts are recovered from the WAL tail
        for i in range(5):
            assert recovered.get(bytes([i])) == b"v"


def test_reopen_after_checkpoint_then_dirty_writes(tmp_path):
    # GIVEN a checkpoint between two batches of puts, with no close after the second batch
    db = DB(tmp_path)
    db.open()
    for i in range(5):
        db.put(bytes([i]), b"first")
    db.checkpoint()
    for i in range(5, 10):
        db.put(bytes([i]), b"second")

    # WHEN reopening
    with DB(tmp_path) as recovered:
        # THEN the first batch survives via the data file and the second via WAL replay
        for i in range(5):
            assert recovered.get(bytes([i])) == b"first"
        for i in range(5, 10):
            assert recovered.get(bytes([i])) == b"second"


def test_reopen_after_puts_and_deletes(tmp_path):
    # GIVEN 5 puts and 2 deletes with no close
    db = DB(tmp_path)
    db.open()
    for i in range(5):
        db.put(bytes([i]), b"v")
    db.delete(bytes([1]))
    db.delete(bytes([3]))

    # WHEN reopening
    with DB(tmp_path) as recovered:
        # THEN only the surviving keys are present
        keys = [k for k, _ in recovered.scan(None, None)]
        assert keys == [bytes([0]), bytes([2]), bytes([4])]
