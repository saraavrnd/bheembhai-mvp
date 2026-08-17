"""Unit — LocalStorage's file/range methods (the ADR-011 log-serving path)."""

from bheembhai.providers.local_storage import LocalStorage

CONTENT = b"0123456789ABCDEF"


async def _store_with_content(tmp_path, key="logs/r1/design/1/agent.log"):
    src = tmp_path / "src.log"
    src.write_bytes(CONTENT)
    store = LocalStorage(base_path=str(tmp_path / "store"))
    await store.put_file(key, str(src), content_type="text/plain")
    return store, key


async def test_put_file_head_get_range_roundtrip(tmp_path):
    store, key = await _store_with_content(tmp_path)
    head = await store.head(key)
    assert head is not None
    assert head.key == key
    assert head.size == len(CONTENT)


async def test_get_range_inclusive_window(tmp_path):
    store, key = await _store_with_content(tmp_path)
    assert await store.get_range(key, 0, 3) == b"0123"
    assert await store.get_range(key, 4, 7) == b"4567"


async def test_get_range_without_end_reads_rest(tmp_path):
    store, key = await _store_with_content(tmp_path)
    assert await store.get_range(key, 10) == b"ABCDEF"


async def test_get_range_clamps_at_eof(tmp_path):
    store, key = await _store_with_content(tmp_path)
    assert await store.get_range(key, 10, 999) == b"ABCDEF"


async def test_get_range_start_beyond_eof_is_empty(tmp_path):
    store, key = await _store_with_content(tmp_path)
    assert await store.get_range(key, 100, 200) == b""


async def test_head_missing_key_is_none(tmp_path):
    store = LocalStorage(base_path=str(tmp_path / "store"))
    assert await store.head("logs/r1/design/1/agent.log") is None


async def test_get_range_missing_key_is_empty(tmp_path):
    store = LocalStorage(base_path=str(tmp_path / "store"))
    assert await store.get_range("logs/r1/design/1/agent.log", 0, 100) == b""


async def test_put_overwrites_same_key(tmp_path):
    """Re-upload (crash re-entry) overwrites the same key — idempotent."""
    src = tmp_path / "src.log"
    src.write_bytes(b"v1")
    store = LocalStorage(base_path=str(tmp_path / "store"))
    key = "logs/r1/design/1/agent.log"
    await store.put_file(key, str(src))
    src.write_bytes(b"v2-longer")
    await store.put_file(key, str(src))
    head = await store.head(key)
    assert head.size == len(b"v2-longer")
    assert await store.get_range(key) == b"v2-longer"
