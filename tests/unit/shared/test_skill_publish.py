"""Unit — deterministic skill bundles for S3 delivery (bheembhai/skill_publish.py).

The bundle is content-addressed (`skills/<name>/<sha256>.tar.gz`) and must pack
to identical bytes for identical content: re-publishing is a head-check no-op,
and concurrent engines that both miss the head check still land the same object
at the same key. Exercises pack determinism, path-safety, and the
put-once/head-skip upload path against an in-memory fake store.
"""

import gzip
import hashlib
import io
import tarfile
import uuid

from bheembhai.models.skill import Skill, SkillFile
from bheembhai.skill_publish import (
    BUNDLE_CONTENT_TYPE,
    pack_skill,
    publish_skill,
    skill_object_key,
)
from bheembhai.protocols.storage import StoredHead


def _skill(name="story-design", paths=None) -> Skill:
    skill = Skill(name=name, description=f"test {name}")
    skill.files = [
        SkillFile(skill_id=uuid.uuid4(), path=p, content=c)
        for p, c in (paths or {}).items()
    ]
    return skill


def _unpack(data: bytes) -> dict:
    """tar.gz bytes -> {archive member name: content}, plus header attrs."""
    out = {}
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tar:
        for m in tar.getmembers():
            out[m.name] = {
                "content": (tar.extractfile(m).read().decode("utf-8")
                            if m.isfile() else None),
                "mode": m.mode, "mtime": m.mtime,
                "uid": m.uid, "gid": m.gid,
                "uname": m.uname, "gname": m.gname,
            }
    return out


class _MemoryStore:
    """In-memory ObjectStorage: put records order + content_type, head is exact."""

    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.content_types: dict[str, str] = {}
        self.puts: list[str] = []

    async def head(self, key):
        if key not in self.objects:
            return None
        return StoredHead(key=key, size=len(self.objects[key]))

    async def put(self, key, data, content_type=None):
        self.puts.append(key)
        self.objects[key] = data
        self.content_types[key] = content_type


# ── pack_skill ───────────────────────────────────────────────────────────────


def test_pack_is_deterministic():
    paths = {"SKILL.md": "# s\n", "references/context.md": "ref\n"}
    assert pack_skill(_skill(paths=paths)) == pack_skill(_skill(paths=paths))


def test_pack_is_sorted_by_path_not_insertion_order():
    forward = _skill(paths={"SKILL.md": "a", "references/context.md": "b"})
    backward = _skill(paths={"references/context.md": "b", "SKILL.md": "a"})
    assert pack_skill(forward) == pack_skill(backward)
    # and the archive itself lists entries in path order
    members = [m.name for m in tarfile.open(
        fileobj=io.BytesIO(pack_skill(forward)), mode="r:*").getmembers()]
    assert members == ["story-design/SKILL.md", "story-design/references/context.md"]


def test_pack_zeroes_mtime_owner_and_mode():
    data = pack_skill(_skill(paths={"SKILL.md": "x"}))
    entry = _unpack(data)["story-design/SKILL.md"]
    assert entry["mtime"] == 0
    assert entry["mode"] == 0o644
    assert (entry["uid"], entry["gid"], entry["uname"], entry["gname"]) == (0, 0, "", "")


def test_pack_zeroes_gzip_header_mtime():
    data = pack_skill(_skill(paths={"SKILL.md": "x"}))
    gz = gzip.GzipFile(fileobj=io.BytesIO(data))
    gz.read(1)   # forces the header read
    assert gz.mtime == 0


def test_pack_skips_paths_escaping_the_skill_dir():
    skill = _skill(paths={
        "SKILL.md": "ok",
        "../escaped.md": "must not land",
        "/absolute.md": "must not land",
        "a/../../deep.md": "must not land",
    })
    tree = _unpack(pack_skill(skill))
    assert list(tree) == ["story-design/SKILL.md"]


def test_pack_roundtrip_layout_skill_name_prefixes_members():
    data = pack_skill(_skill(name="code-review", paths={
        "SKILL.md": "# review",
        "references/rubric.md": "rubric",
    }))
    tree = _unpack(data)
    assert tree["code-review/SKILL.md"]["content"] == "# review"
    assert tree["code-review/references/rubric.md"]["content"] == "rubric"


def test_pack_empty_skill_archives_to_no_members():
    data = pack_skill(_skill(name="empty"))
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tar:
        assert tar.getmembers() == []


# ── skill_object_key ─────────────────────────────────────────────────────────


def test_object_key_naming():
    sha = "a" * 64
    assert skill_object_key("story-design", sha) == f"skills/story-design/{sha}.tar.gz"


# ── publish_skill ────────────────────────────────────────────────────────────


async def test_publish_returns_key_and_sha_and_uploads_once():
    store = _MemoryStore()
    skill = _skill(paths={"SKILL.md": "content"})
    key, sha = await publish_skill(store, skill)

    data = pack_skill(skill)
    assert sha == hashlib.sha256(data).hexdigest()
    assert key == skill_object_key("story-design", sha)
    assert store.objects[key] == data
    assert store.content_types[key] == BUNDLE_CONTENT_TYPE
    assert store.puts == [key]


async def test_publish_skips_put_when_object_exists():
    store = _MemoryStore()
    skill = _skill(paths={"SKILL.md": "content"})
    key1, sha1 = await publish_skill(store, skill)
    key2, sha2 = await publish_skill(store, skill)

    assert (key1, sha1) == (key2, sha2)
    assert store.puts == [key1]        # head-check made the second call a no-op


async def test_publish_different_content_lands_different_keys():
    store = _MemoryStore()
    v1 = _skill(paths={"SKILL.md": "v1"})
    v2 = _skill(paths={"SKILL.md": "v2"})
    key1, _ = await publish_skill(store, v1)
    key2, _ = await publish_skill(store, v2)

    assert key1 != key2
    assert set(store.objects) == {key1, key2}
