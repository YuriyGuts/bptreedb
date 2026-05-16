from bptreedb.db import DB
from bptreedb.entities import WALCheckpointRecord


def test_manual_checkpoint_truncates_wal(tmp_path):
    # GIVEN a DB with several writes after the open-time checkpoint
    with DB(tmp_path) as db:
        for i in range(5):
            db.put(bytes([i]), b"v")
        size_before = db.wal.size_bytes

        # WHEN manually checkpointing
        db.checkpoint()

        # THEN the WAL shrinks to just the new CHECKPOINT marker
        assert db.wal.size_bytes < size_before
        records = []
        db.wal.replay(records.append)
        assert len(records) == 1
        assert isinstance(records[0], WALCheckpointRecord)


def test_auto_checkpoint_by_wal_size(tmp_path):
    # GIVEN a DB with a tiny WAL-size auto-checkpoint trigger
    with DB(tmp_path, checkpoint_wal_size_bytes=200) as db:
        # WHEN doing enough writes to repeatedly cross the threshold
        sizes = []
        for i in range(20):
            db.put(bytes([i]), b"x" * 50)
            sizes.append(db.wal.size_bytes)

        # THEN the WAL must have shrunk at some point
        assert any(sizes[i] < sizes[i - 1] for i in range(1, len(sizes)))


def test_auto_checkpoint_by_dirty_ratio(tmp_path):
    # Tiny pages so puts cause leaf splits quickly, tiny cache so the dirty ratio crosses
    # 0.25 after just a couple of dirty pages, and a huge WAL-size threshold so the
    # WAL-size trigger can't muddy the test.
    with DB(
        tmp_path,
        page_size_bytes=256,
        cache_capacity_pages=4,
        checkpoint_dirty_page_ratio=0.25,
        checkpoint_wal_size_bytes=1024 * 1024 * 1024,
    ) as db:
        # WHEN doing enough writes for splits to dirty multiple pages
        sizes = []
        for i in range(50):
            db.put(bytes([i]), b"x" * 5)
            sizes.append(db.wal.size_bytes)

        # THEN the WAL must have shrunk at some point
        assert any(sizes[i] < sizes[i - 1] for i in range(1, len(sizes)))


def test_checkpoint_persists_tree_state(tmp_path):
    # GIVEN a DB with enough writes to force tree splits (tiny pages keep node count up)
    with DB(tmp_path, page_size_bytes=256) as db:
        for i in range(100):
            db.put(f"key{i:03d}".encode(), b"v" * 10)
        db.checkpoint()
        expected_root = db.pager.get_meta().root_page_id
        expected_next = db.pager.get_meta().next_page_id

    # WHEN reopening
    with DB(tmp_path, page_size_bytes=256) as reopened:
        # THEN the persisted root and page allocator match what was in memory at checkpoint time
        meta = reopened.pager.get_meta()
        assert meta.root_page_id == expected_root
        assert meta.next_page_id == expected_next
