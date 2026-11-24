"""Tests for the drill-copy repair engine and its API."""

from __future__ import annotations

import hashlib
import os
import shutil
import threading

import pytest
from fastapi.testclient import TestClient

import app.main as main
import app.repair as repair_mod
import make_sample_archive
from app.repair import (
    InvalidRepairName,
    InvalidRestoreLayout,
    RepairConflict,
    RepairNotFound,
    repair_replica,
)
from app.restore import restore_directory

BLOB_GROUP = ["chained_link", "chained_mid", "shared_link", "three_way_a", "three_way_b"]
INSIDE_GROUP = ["inside.txt", "self_reference"]
DUP_CONTENT = b"same bytes, two independent files\n"


# ------------------------------------------------------------------ helpers


def _make_restore(sample_archive, tmp_path):
    restore_root = tmp_path / "restored"
    result = restore_directory(sample_archive, "data/2024", str(restore_root), fsync=False)
    restore_id = os.path.basename(os.path.dirname(result.restored_path))
    return str(restore_root), restore_id, result.restored_path


def _make_replica(tmp_path, restored_path, name="2024"):
    """Copy the restore onto the replica mount, breaking every hard link."""

    replica_root = tmp_path / "replica"
    replica_root.mkdir(exist_ok=True)
    replica = str(replica_root / name)
    shutil.copytree(restored_path, replica, copy_function=shutil.copy2)
    return str(replica_root), replica


def _inode_groups(root: str) -> list[list[str]]:
    by_inode: dict[int, list[str]] = {}
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            path = os.path.join(dirpath, name)
            by_inode.setdefault(os.stat(path).st_ino, []).append(
                os.path.relpath(path, root)
            )
    return sorted((sorted(m) for m in by_inode.values()), key=lambda g: g[0])


def _signature(root: str) -> dict[str, tuple[int, str]]:
    """rel path -> (inode, sha256) for every file in the tree."""

    sig = {}
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            path = os.path.join(dirpath, name)
            with open(path, "rb") as fh:
                digest = hashlib.sha256(fh.read()).hexdigest()
            sig[os.path.relpath(path, root)] = (os.stat(path).st_ino, digest)
    return sig


def _digests(sig: dict[str, tuple[int, str]]) -> dict[str, str]:
    return {rel: digest for rel, (_ino, digest) in sig.items()}


def _repair(restore_root, replica_root, restore_id, name):
    return repair_replica(restore_root, replica_root, restore_id, name, fsync=False)


def _no_staging_leftovers(replica_root: str) -> bool:
    staging = os.path.join(replica_root, ".repair")
    return not os.path.isdir(staging) or os.listdir(staging) == []


# -------------------------------------------------------------- engine: merge


def test_merge_broken_links(sample_archive, tmp_path):
    restore_root, restore_id, restored_path = _make_restore(sample_archive, tmp_path)
    replica_root, replica = _make_replica(tmp_path, restored_path)
    before = _signature(replica)

    result = _repair(restore_root, replica_root, restore_id, "2024")

    assert result.merged == sorted([BLOB_GROUP, INSIDE_GROUP])
    assert result.split == []
    assert result.bytes_copied == 0
    assert result.files == 12
    assert result.directories == 2
    assert result.repaired_path == replica
    # The link structure now matches the restore exactly...
    assert _inode_groups(replica) == _inode_groups(restored_path)
    # ...and not a single byte of the replica was rewritten.
    assert _digests(_signature(replica)) == _digests(before)
    assert _no_staging_leftovers(replica_root)


def test_repair_is_idempotent(sample_archive, tmp_path):
    restore_root, restore_id, restored_path = _make_restore(sample_archive, tmp_path)
    replica_root, _replica = _make_replica(tmp_path, restored_path)

    first = _repair(restore_root, replica_root, restore_id, "2024")
    assert first.merged  # the first run had work to do
    second = _repair(restore_root, replica_root, restore_id, "2024")
    assert second.merged == []
    assert second.split == []
    assert second.bytes_copied == 0


# -------------------------------------------------------------- engine: split


def test_split_wrongly_merged_same_content(sample_archive, tmp_path):
    restore_root, restore_id, restored_path = _make_restore(sample_archive, tmp_path)
    replica_root, replica = _make_replica(tmp_path, restored_path)
    # Simulate content dedup: two independent files joined into one inode.
    os.unlink(os.path.join(replica, "nested", "dup_b.txt"))
    os.link(os.path.join(replica, "dup_a.txt"), os.path.join(replica, "nested", "dup_b.txt"))

    result = _repair(restore_root, replica_root, restore_id, "2024")

    assert result.split == [["dup_a.txt", "nested/dup_b.txt"]]
    assert result.merged == sorted([BLOB_GROUP, INSIDE_GROUP])
    assert result.bytes_copied == len(DUP_CONTENT)
    # Split even though the bytes are identical; the bytes are preserved.
    stat_a = os.stat(os.path.join(replica, "dup_a.txt"))
    stat_b = os.stat(os.path.join(replica, "nested", "dup_b.txt"))
    assert stat_a.st_ino != stat_b.st_ino
    with open(os.path.join(replica, "nested", "dup_b.txt"), "rb") as fh:
        assert fh.read() == DUP_CONTENT
    assert stat_b.st_mode & 0o777 == 0o644  # replica metadata kept on the copy
    assert _inode_groups(replica) == _inode_groups(restored_path)


def test_split_preserves_current_replica_bytes(sample_archive, tmp_path):
    restore_root, restore_id, restored_path = _make_restore(sample_archive, tmp_path)
    replica_root, replica = _make_replica(tmp_path, restored_path)
    # A bad dedup joined report.bin into the inside.txt inode; report.bin's
    # current bytes are inside.txt's bytes and must be preserved as-is.
    os.unlink(os.path.join(replica, "report.bin"))
    os.link(os.path.join(replica, "inside.txt"), os.path.join(replica, "report.bin"))
    before = _signature(replica)

    result = _repair(restore_root, replica_root, restore_id, "2024")

    # The copy had already broken the inside.txt/self_reference link, so the
    # wrongly shared inode covers exactly inside.txt and report.bin.
    assert result.split == [["inside.txt", "report.bin"]]
    assert result.merged == sorted([BLOB_GROUP, INSIDE_GROUP])
    inodes = {
        rel: os.stat(os.path.join(replica, rel)).st_ino
        for rel in ("inside.txt", "self_reference", "report.bin")
    }
    assert inodes["inside.txt"] == inodes["self_reference"]
    assert inodes["report.bin"] not in (inodes["inside.txt"],)
    assert _digests(_signature(replica)) == _digests(before)
    assert _inode_groups(replica) == _inode_groups(restored_path)


# ----------------------------------------------------------- engine: conflicts


def test_conflict_missing_path_keeps_original(sample_archive, tmp_path):
    restore_root, restore_id, restored_path = _make_restore(sample_archive, tmp_path)
    replica_root, replica = _make_replica(tmp_path, restored_path)
    os.remove(os.path.join(replica, "inside.txt"))
    before = _signature(replica)

    with pytest.raises(RepairConflict) as excinfo:
        _repair(restore_root, replica_root, restore_id, "2024")

    assert any("missing in replica" in c and "inside.txt" in c for c in excinfo.value.conflicts)
    assert _signature(replica) == before  # original directory untouched
    assert _no_staging_leftovers(replica_root)


def test_conflict_unexpected_path(sample_archive, tmp_path):
    restore_root, restore_id, restored_path = _make_restore(sample_archive, tmp_path)
    replica_root, replica = _make_replica(tmp_path, restored_path)
    with open(os.path.join(replica, "stray.txt"), "wb") as fh:
        fh.write(b"stray")

    with pytest.raises(RepairConflict) as excinfo:
        _repair(restore_root, replica_root, restore_id, "2024")

    assert any("unexpected in replica" in c and "stray.txt" in c for c in excinfo.value.conflicts)


def test_conflict_type_mismatch(sample_archive, tmp_path):
    restore_root, restore_id, restored_path = _make_restore(sample_archive, tmp_path)
    replica_root, replica = _make_replica(tmp_path, restored_path)
    os.remove(os.path.join(replica, "inside.txt"))
    os.mkdir(os.path.join(replica, "inside.txt"))

    with pytest.raises(RepairConflict) as excinfo:
        _repair(restore_root, replica_root, restore_id, "2024")

    assert any("type mismatch" in c and "inside.txt" in c for c in excinfo.value.conflicts)


def test_conflict_merge_group_with_different_bytes(sample_archive, tmp_path):
    restore_root, restore_id, restored_path = _make_restore(sample_archive, tmp_path)
    replica_root, replica = _make_replica(tmp_path, restored_path)
    with open(os.path.join(replica, "shared_link"), "ab") as fh:
        fh.write(b"tampered")
    before = _signature(replica)

    with pytest.raises(RepairConflict) as excinfo:
        _repair(restore_root, replica_root, restore_id, "2024")

    assert any("different bytes" in c for c in excinfo.value.conflicts)
    assert any("shared_link" in c for c in excinfo.value.conflicts)
    assert _signature(replica) == before


def test_conflict_unsupported_entry_type(sample_archive, tmp_path):
    restore_root, restore_id, restored_path = _make_restore(sample_archive, tmp_path)
    replica_root, replica = _make_replica(tmp_path, restored_path)
    os.symlink("inside.txt", os.path.join(replica, "link.txt"))

    with pytest.raises(RepairConflict) as excinfo:
        _repair(restore_root, replica_root, restore_id, "2024")

    assert any("unsupported entry type" in c for c in excinfo.value.conflicts)


# ------------------------------------------------------- engine: failure safety


def test_build_failure_keeps_original(sample_archive, tmp_path, monkeypatch):
    restore_root, restore_id, restored_path = _make_restore(sample_archive, tmp_path)
    replica_root, replica = _make_replica(tmp_path, restored_path)
    before = _signature(replica)

    def boom(*_args, **_kwargs):
        raise OSError("simulated build failure")

    monkeypatch.setattr(repair_mod.os, "link", boom)
    with pytest.raises(OSError, match="simulated build failure"):
        _repair(restore_root, replica_root, restore_id, "2024")

    assert _signature(replica) == before  # original directory still usable
    assert _no_staging_leftovers(replica_root)


def test_swap_failure_rolls_back(sample_archive, tmp_path, monkeypatch):
    restore_root, restore_id, restored_path = _make_restore(sample_archive, tmp_path)
    replica_root, replica = _make_replica(tmp_path, restored_path)
    before = _signature(replica)
    real_rename = os.rename

    def flaky(src, dst):
        if dst == replica and src.endswith("/tree"):
            raise OSError("simulated swap failure")
        return real_rename(src, dst)

    monkeypatch.setattr(repair_mod.os, "rename", flaky)
    with pytest.raises(OSError, match="simulated swap failure"):
        _repair(restore_root, replica_root, restore_id, "2024")

    assert _signature(replica) == before  # rolled back, still usable
    assert _no_staging_leftovers(replica_root)


def test_startup_recovers_crash_between_renames(tmp_path, monkeypatch):
    # Simulate a process kill between the two swap renames: the original tree
    # is parked in staging, the replica path is missing.
    replica_root = tmp_path / "replica"
    token = replica_root / ".repair" / "deadbeef"
    old = token / "old"
    old.mkdir(parents=True)
    (old / "file.txt").write_text("original bytes")
    (token / "TARGET").write_text("2024")
    monkeypatch.setattr(main, "REPLICA_DIR", str(replica_root))

    main._recover_replica_staging()

    assert (replica_root / "2024" / "file.txt").read_text() == "original bytes"
    assert not token.exists()  # staging cleaned up


def test_startup_drops_fully_swapped_staging(tmp_path, monkeypatch):
    # Crash AFTER the new tree was swapped in: the replica path exists, the
    # parked original is stale and must be discarded.
    replica_root = tmp_path / "replica"
    (replica_root / "2024").mkdir(parents=True)
    (replica_root / "2024" / "new.txt").write_text("repaired")
    token = replica_root / ".repair" / "deadbeef"
    old = token / "old"
    old.mkdir(parents=True)
    (old / "file.txt").write_text("superseded")
    (token / "TARGET").write_text("2024")
    monkeypatch.setattr(main, "REPLICA_DIR", str(replica_root))

    main._recover_replica_staging()

    assert (replica_root / "2024" / "new.txt").read_text() == "repaired"
    assert not (replica_root / "2024" / "file.txt").exists()
    assert not token.exists()


# --------------------------------------------------------- engine: validation


def test_invalid_names_are_rejected(sample_archive, tmp_path):
    restore_root, restore_id, _p = _make_restore(sample_archive, tmp_path)
    replica_root, _replica = _make_replica(tmp_path, _p)
    for bad_id in ("../x", "a/b", ".hidden", ""):
        with pytest.raises(InvalidRepairName):
            _repair(restore_root, replica_root, bad_id, "2024")
    for bad_name in ("../x", "a/b", ".repair", ""):
        with pytest.raises(InvalidRepairName):
            _repair(restore_root, replica_root, restore_id, bad_name)


def test_unknown_restore_and_replica(sample_archive, tmp_path):
    restore_root, restore_id, restored_path = _make_restore(sample_archive, tmp_path)
    replica_root, _replica = _make_replica(tmp_path, restored_path)
    with pytest.raises(RepairNotFound):
        _repair(restore_root, replica_root, "0" * 32, "2024")
    with pytest.raises(RepairNotFound):
        _repair(restore_root, replica_root, restore_id, "no-such-dir")


def test_invalid_restore_layout(sample_archive, tmp_path):
    restore_root, _id, restored_path = _make_restore(sample_archive, tmp_path)
    replica_root, _replica = _make_replica(tmp_path, restored_path)
    messy = os.path.join(restore_root, "abc123")
    os.makedirs(os.path.join(messy, "one"))
    os.makedirs(os.path.join(messy, "two"))
    with pytest.raises(InvalidRestoreLayout):
        _repair(restore_root, replica_root, "abc123", "2024")


# ------------------------------------------------------------------------ API


@pytest.fixture
def client(tmp_path, monkeypatch):
    archives = tmp_path / "backups"
    restored = tmp_path / "restored"
    replica = tmp_path / "replica"
    archives.mkdir()
    replica.mkdir()
    monkeypatch.setattr(main, "ARCHIVE_DIR", str(archives))
    monkeypatch.setattr(main, "OUTPUT_DIR", str(restored))
    monkeypatch.setattr(main, "REPLICA_DIR", str(replica))
    with TestClient(main.app) as c:
        yield c, archives, restored, replica


def _restore_via_api(c, archives) -> tuple[str, str]:
    make_sample_archive.build(os.path.join(str(archives), "backup.tar.gz"))
    response = c.post("/api/restores", json={"archive": "backup.tar.gz", "path": "data/2024"})
    assert response.status_code == 201, response.text
    restored_path = response.json()["restored_path"]
    restore_id = os.path.basename(os.path.dirname(restored_path))
    return restore_id, restored_path


def test_repair_api_happy_path(client):
    c, _archives, _restored, replica_root = client
    restore_id, restored_path = _restore_via_api(c, _archives)
    replica = str(replica_root / "2024-drill")
    shutil.copytree(restored_path, replica, copy_function=shutil.copy2)
    os.unlink(os.path.join(replica, "nested", "dup_b.txt"))
    os.link(os.path.join(replica, "dup_a.txt"), os.path.join(replica, "nested", "dup_b.txt"))

    response = c.post("/api/repairs", json={"restore_id": restore_id, "replica": "2024-drill"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["restore_id"] == restore_id
    assert body["replica"] == "2024-drill"
    assert body["repaired_path"] == replica
    assert body["split"] == [["dup_a.txt", "nested/dup_b.txt"]]
    assert body["merged"] == sorted([BLOB_GROUP, INSIDE_GROUP])
    assert body["bytes_copied"] == len(DUP_CONTENT)
    assert _inode_groups(replica) == _inode_groups(restored_path)


def test_list_restores(client):
    c, _archives, _restored, _replica = client
    restore_id, restored_path = _restore_via_api(c, _archives)
    response = c.get("/api/restores")
    assert response.status_code == 200
    assert response.json() == [{"restore_id": restore_id, "restored_path": restored_path}]


def test_repair_api_not_found(client):
    c, _archives, _restored, _replica = client
    response = c.post("/api/repairs", json={"restore_id": "0" * 32, "replica": "x"})
    assert response.status_code == 404
    restore_id, _p = _restore_via_api(c, _archives)
    response = c.post("/api/repairs", json={"restore_id": restore_id, "replica": "nope"})
    assert response.status_code == 404


def test_repair_api_invalid_names(client):
    c, _archives, _restored, _replica = client
    for payload in (
        {"restore_id": "../etc", "replica": "x"},
        {"restore_id": "a" * 32, "replica": "../x"},
        {"restore_id": "a" * 32, "replica": ".repair"},
    ):
        response = c.post("/api/repairs", json=payload)
        assert response.status_code == 400, payload


def test_repair_api_conflict_keeps_original(client):
    c, _archives, _restored, replica_root = client
    restore_id, restored_path = _restore_via_api(c, _archives)
    replica = str(replica_root / "2024-drill")
    shutil.copytree(restored_path, replica, copy_function=shutil.copy2)
    with open(os.path.join(replica, "shared_link"), "ab") as fh:
        fh.write(b"tampered")
    before = _signature(replica)

    response = c.post("/api/repairs", json={"restore_id": restore_id, "replica": "2024-drill"})

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert any("different bytes" in c for c in detail["conflicts"])
    assert _signature(replica) == before


def test_repair_api_rejects_concurrent_repair(client, monkeypatch):
    c, _archives, _restored, _replica = client
    locked = threading.Lock()
    locked.acquire()
    monkeypatch.setattr(main, "_repair_lock", locked)
    response = c.post("/api/repairs", json={"restore_id": "a" * 32, "replica": "x"})
    assert response.status_code == 409
    assert "in progress" in response.json()["detail"]
