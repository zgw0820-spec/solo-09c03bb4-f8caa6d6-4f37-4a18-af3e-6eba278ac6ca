"""Unit tests for the restore engine."""

from __future__ import annotations

import os

import pytest

from app.restore import (
    InvalidSelection,
    LinkError,
    NotADirectory,
    PathNotFound,
    restore_directory,
)


def _pairs(root: str) -> list[tuple[str, int, int]]:
    found = []
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            path = os.path.join(dirpath, name)
            stat = os.stat(path)
            found.append((os.path.relpath(path, root), stat.st_ino, stat.st_size))
    return sorted(found)


def test_restores_only_selected_subtree(sample_archive, tmp_path):
    out = tmp_path / "out"
    result = restore_directory(sample_archive, "data/2024", str(out), fsync=False)

    assert result.requested_path == "data/2024"
    assert result.restored_path.startswith(str(out))
    names = {name for name, _ino, _size in _pairs(result.restored_path)}
    assert "inside.txt" in names
    assert "nested/deep.txt" in names
    assert "forward_link" in names
    assert not any("2025" in n for n in names)
    assert not any("unrelated" in n for n in names)
    # Only the selected directory was published.
    published = [p for p in os.listdir(out) if not p.startswith(".")]
    assert len(published) == 1
    assert os.path.basename(result.restored_path) == "2024"


def test_only_needed_data_is_written(sample_archive, tmp_path):
    result = restore_directory(sample_archive, "data/2024", str(tmp_path / "out"), fsync=False)
    # The unrelated file is 900 KB; the selected data is far smaller.
    assert result.bytes_written < 100_000
    assert result.bytes_extracted <= 2 * result.archive_payload_bytes


def test_cross_directory_and_chained_links_share_inode(sample_archive, tmp_path):
    result = restore_directory(sample_archive, "data/2024", str(tmp_path / "out"), fsync=False)
    by_name = {name: (ino, size) for name, ino, size in _pairs(result.restored_path)}

    blob_inodes = {
        by_name[name][0]
        for name in ("shared_link", "chained_link", "chained_mid", "three_way_a", "three_way_b")
    }
    assert len(blob_inodes) == 1, "all links to shared/blob.bin must share one inode"
    assert by_name["three_way_a"][1] == by_name["shared_link"][1]

    # A hard link pointing at a file inside the selection shares its inode too.
    assert by_name["self_reference"][0] == by_name["inside.txt"][0]


def test_forward_reference_outside_selection(sample_archive, tmp_path):
    result = restore_directory(sample_archive, "data/2024", str(tmp_path / "out"), fsync=False)
    path = os.path.join(result.restored_path, "forward_link")
    assert os.path.isfile(path)
    with open(path, "rb") as fh:
        assert fh.read() == b"FORWARD-DATA-" * 256
    assert not os.path.exists(os.path.join(result.restored_path, "forward.bin"))


def test_link_to_selected_file_uses_it_as_source(sample_archive, tmp_path):
    result = restore_directory(sample_archive, "data/2024", str(tmp_path / "out"), fsync=False)
    stat_inside = os.stat(os.path.join(result.restored_path, "inside.txt"))
    stat_link = os.stat(os.path.join(result.restored_path, "self_reference"))
    assert stat_inside.st_ino == stat_link.st_ino
    assert stat_inside.st_nlink >= 2


def test_selecting_root_of_link_only_group_materialises_data(tmp_path, archive_builder):
    archive = archive_builder(
        "links.tar.gz",
        [
            ("dir", "top"),
            ("dir", "top/sel"),
            ("file", "outside/data.bin", b"payload" * 1000),
            ("link", "top/sel/a", "outside/data.bin"),
            ("link", "top/sel/b", "top/sel/a"),
        ],
    )
    result = restore_directory(archive, "top/sel", str(tmp_path / "out"), fsync=False)
    pairs = dict((name, ino) for name, ino, _ in _pairs(result.restored_path))
    assert pairs["a"] == pairs["b"]
    assert os.path.getsize(os.path.join(result.restored_path, "a")) == 7000


def test_forward_reference_even_when_target_inside_selection(tmp_path, archive_builder):
    archive = archive_builder(
        "forward.tar.gz",
        [
            ("dir", "d"),
            ("dir", "d/sub"),
            ("link", "d/sub/early_link", "d/sub/late_file"),
            ("file", "d/sub/late_file", b"late"),
        ],
    )
    result = restore_directory(archive, "d/sub", str(tmp_path / "out"), fsync=False)
    assert os.stat(os.path.join(result.restored_path, "early_link")).st_ino == os.stat(
        os.path.join(result.restored_path, "late_file")
    ).st_ino


def test_chain_inside_selection(tmp_path, archive_builder):
    archive = archive_builder(
        "chain.tar.gz",
        [
            ("dir", "d"),
            ("dir", "d/sub"),
            ("file", "d/sub/real", b"real-bytes"),
            ("link", "d/sub/l1", "d/sub/l2"),
            ("link", "d/sub/l2", "d/sub/real"),
        ],
    )
    result = restore_directory(archive, "d/sub", str(tmp_path / "out"), fsync=False)
    inode = os.stat(os.path.join(result.restored_path, "real")).st_ino
    for name in ("l1", "l2"):
        assert os.stat(os.path.join(result.restored_path, name)).st_ino == inode


def test_missing_target_raises_and_cleans_up(tmp_path, archive_builder):
    archive = archive_builder(
        "missing.tar.gz",
        [
            ("dir", "d"),
            ("dir", "d/sub"),
            ("file", "d/sub/ok.txt", b"ok"),
            ("link", "d/sub/broken", "d/ghost"),
        ],
    )
    out = tmp_path / "out"
    with pytest.raises(LinkError):
        restore_directory(archive, "d/sub", str(out), fsync=False)
    _assert_no_leftovers(out)


def test_cycle_raises_and_cleans_up(tmp_path, archive_builder):
    archive = archive_builder(
        "cycle.tar.gz",
        [
            ("dir", "d"),
            ("dir", "d/sub"),
            ("link", "d/sub/a", "d/sub/b"),
            ("link", "d/sub/b", "d/sub/c"),
            ("link", "d/sub/c", "d/sub/a"),
        ],
    )
    out = tmp_path / "out"
    with pytest.raises(LinkError, match="cycl"):
        restore_directory(archive, "d/sub", str(out), fsync=False)
    _assert_no_leftovers(out)


def test_broken_links_outside_selection_are_ignored(tmp_path, archive_builder):
    archive = archive_builder(
        "outside_broken.tar.gz",
        [
            ("dir", "d"),
            ("dir", "d/sub"),
            ("file", "d/sub/ok.txt", b"ok"),
            ("link", "elsewhere/broken", "nowhere"),
        ],
    )
    result = restore_directory(archive, "d/sub", str(tmp_path / "out"), fsync=False)
    assert os.path.exists(os.path.join(result.restored_path, "ok.txt"))


def test_failure_mid_write_cleans_up(tmp_path, archive_builder, monkeypatch):
    archive = archive_builder(
        "midfail.tar.gz",
        [
            ("dir", "d"),
            ("dir", "d/sub"),
            ("file", "d/sub/a.txt", b"a" * 100),
            ("file", "d/sub/b.txt", b"b" * 100),
        ],
    )
    out = tmp_path / "out"

    import app.restore as restore_mod

    real_write = restore_mod._write_payload
    calls = {"n": 0}

    def flaky(reader, dest, member, stats, fsync):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("disk full")
        return real_write(reader, dest, member, stats, fsync)

    monkeypatch.setattr(restore_mod, "_write_payload", flaky)
    with pytest.raises(OSError):
        restore_directory(archive, "d/sub", str(out), fsync=False)
    _assert_no_leftovers(out)


def test_not_a_directory(tmp_path, archive_builder):
    archive = archive_builder(
        "f.tar.gz", [("dir", "d"), ("file", "d/thing", b"x")]
    )
    with pytest.raises(NotADirectory):
        restore_directory(archive, "d/thing", str(tmp_path / "out"), fsync=False)
    _assert_no_leftovers(tmp_path / "out")


def test_missing_path(tmp_path, archive_builder):
    archive = archive_builder("f.tar.gz", [("dir", "d"), ("file", "d/x", b"x")])
    with pytest.raises(PathNotFound):
        restore_directory(archive, "d/nope", str(tmp_path / "out"), fsync=False)


def test_invalid_selection_paths(tmp_path, archive_builder):
    archive = archive_builder("f.tar.gz", [("dir", "d"), ("file", "d/x", b"x")])
    for bad in ("", "/", "/d", "d/../d", "..", "  "):
        with pytest.raises(InvalidSelection):
            restore_directory(archive, bad, str(tmp_path / "out"), fsync=False)


def test_unsafe_member_paths_are_skipped(tmp_path, archive_builder):
    archive = archive_builder(
        "unsafe.tar.gz",
        [
            ("dir", "d"),
            ("dir", "d/sub"),
            ("file", "d/sub/good.txt", b"good"),
            ("file", "d/sub/../../escape.txt", b"escape"),
        ],
    )
    result = restore_directory(archive, "d/sub", str(tmp_path / "out"), fsync=False)
    assert result.skipped_unsafe == 1
    assert os.path.exists(os.path.join(result.restored_path, "good.txt"))
    assert not os.path.exists(tmp_path / "out" / "escape.txt")


def test_symlinks_are_not_restored(tmp_path, archive_builder):
    archive = archive_builder(
        "sym.tar.gz",
        [
            ("dir", "d"),
            ("dir", "d/sub"),
            ("file", "d/sub/real", b"real"),
            ("symlink", "d/sub/link", "/etc/passwd"),
        ],
    )
    result = restore_directory(archive, "d/sub", str(tmp_path / "out"), fsync=False)
    assert result.skipped_unsupported == 1
    assert not os.path.lexists(os.path.join(result.restored_path, "link"))


def test_duplicate_directory_entries_are_tolerated(tmp_path, archive_builder):
    archive = archive_builder(
        "dup.tar.gz",
        [
            ("dir", "d"),
            ("dir", "d/sub"),
            ("dir", "d/sub"),
            ("file", "d/sub/x", b"x"),
        ],
    )
    result = restore_directory(archive, "d/sub", str(tmp_path / "out"), fsync=False)
    assert os.path.exists(os.path.join(result.restored_path, "x"))


def test_implicit_directories_are_created(tmp_path, archive_builder):
    archive = archive_builder(
        "implicit.tar.gz",
        [
            ("dir", "d"),
            ("file", "d/sub/a/b/c.txt", b"deep"),
        ],
    )
    result = restore_directory(archive, "d/sub", str(tmp_path / "out"), fsync=False)
    assert os.path.exists(os.path.join(result.restored_path, "a", "b", "c.txt"))


def test_two_restores_get_isolated_directories(sample_archive, tmp_path):
    out = tmp_path / "out"
    first = restore_directory(sample_archive, "data/2024", str(out), fsync=False)
    second = restore_directory(sample_archive, "data/2024", str(out), fsync=False)
    assert first.restored_path != second.restored_path
    assert os.path.isdir(first.restored_path)
    assert os.path.isdir(second.restored_path)


def _assert_no_leftovers(out) -> None:
    staging = os.path.join(str(out), ".staging")
    if os.path.isdir(staging):
        assert os.listdir(staging) == []
    if os.path.isdir(out):
        published = [p for p in os.listdir(out) if not p.startswith(".")]
        assert published == []


def test_extracted_bytes_stay_within_two_times_budget(tmp_path, archive_builder):
    # Worst case: the only selected entry is a link whose external target is the
    # very last member, after a large unrelated file.
    archive = archive_builder(
        "worst.tar.gz",
        [
            ("dir", "d"),
            ("dir", "d/sub"),
            ("file", "noise.bin", b"N" * 2_000_000),
            ("link", "d/sub/link", "ext/target.bin"),
            ("file", "ext/target.bin", b"T" * 5_000),
        ],
    )
    result = restore_directory(archive, "d/sub", str(tmp_path / "out"), fsync=False)
    assert result.bytes_extracted <= 2 * result.archive_payload_bytes
    assert result.bytes_written == 5_000


def test_corrupt_archive_is_rejected(tmp_path):
    from app.restore import InvalidArchive

    bad = tmp_path / "bad.tar.gz"
    bad.write_bytes(b"\x1f\x8b\x08\x00garbage")
    with pytest.raises(InvalidArchive):
        restore_directory(str(bad), "d", str(tmp_path / "out"), fsync=False)
    _assert_no_leftovers(tmp_path / "out")


def test_non_gzip_archive_is_rejected(tmp_path):
    from app.restore import InvalidArchive

    plain = tmp_path / "plain.tar.gz"
    plain.write_bytes(b"not an archive at all")
    with pytest.raises(InvalidArchive):
        restore_directory(str(plain), "d", str(tmp_path / "out"), fsync=False)


def test_restrictive_directory_modes_remain_traversable(tmp_path, archive_builder):
    archive = archive_builder(
        "modes.tar.gz",
        [
            ("dir", "d"),
            ("dir", "d/sub"),
            ("file", "d/sub/ro.txt", b"ro"),
            ("link", "d/sub/l", "d/sub/ro.txt"),
        ],
    )
    result = restore_directory(archive, "d/sub", str(tmp_path / "out"), fsync=False)
    # We must be able to read the restored file and remove the tree.
    with open(os.path.join(result.restored_path, "ro.txt"), "rb") as fh:
        assert fh.read() == b"ro"
    assert os.stat(os.path.join(result.restored_path, "l")).st_ino == os.stat(
        os.path.join(result.restored_path, "ro.txt")
    ).st_ino


def test_symlink_in_selection_cannot_be_used_to_escape(tmp_path, archive_builder):
    archive = archive_builder(
        "escape.tar.gz",
        [
            ("dir", "d"),
            ("dir", "d/sub"),
            ("symlink", "d/sub/evil", "/etc"),
            ("file", "d/sub/ok", b"ok"),
        ],
    )
    result = restore_directory(archive, "d/sub", str(tmp_path / "out"), fsync=False)
    assert not os.path.lexists(os.path.join(result.restored_path, "evil"))
    assert os.path.exists(os.path.join(result.restored_path, "ok"))


def test_multiple_external_targets(tmp_path, archive_builder):
    archive = archive_builder(
        "multi.tar.gz",
        [
            ("dir", "d"),
            ("dir", "d/sub"),
            ("file", "ext/one.bin", b"ONE" * 100),
            ("link", "d/sub/l1", "ext/one.bin"),
            ("link", "d/sub/l2", "ext/one.bin"),
            ("file", "ext/two.bin", b"TWO" * 100),
            ("link", "d/sub/l3", "ext/two.bin"),
        ],
    )
    result = restore_directory(archive, "d/sub", str(tmp_path / "out"), fsync=False)
    one = os.stat(os.path.join(result.restored_path, "l1")).st_ino
    two = os.stat(os.path.join(result.restored_path, "l3")).st_ino
    assert os.stat(os.path.join(result.restored_path, "l2")).st_ino == one
    assert one != two
    with open(os.path.join(result.restored_path, "l3"), "rb") as fh:
        assert fh.read() == b"TWO" * 100
    assert result.bytes_extracted <= 2 * result.archive_payload_bytes


def test_link_target_that_is_a_directory_raises(tmp_path, archive_builder):
    from app.restore import LinkError

    archive = archive_builder(
        "dirlink.tar.gz",
        [
            ("dir", "d"),
            ("dir", "d/sub"),
            ("dir", "ext/dir"),
            ("link", "d/sub/l", "ext/dir"),
        ],
    )
    with pytest.raises(LinkError, match="not a regular file"):
        restore_directory(archive, "d/sub", str(tmp_path / "out"), fsync=False)
    _assert_no_leftovers(tmp_path / "out")


def test_self_link_is_a_cycle(tmp_path, archive_builder):
    from app.restore import LinkError

    archive = archive_builder(
        "selflink.tar.gz",
        [
            ("dir", "d"),
            ("dir", "d/sub"),
            ("link", "d/sub/me", "d/sub/me"),
        ],
    )
    with pytest.raises(LinkError, match="cycl"):
        restore_directory(archive, "d/sub", str(tmp_path / "out"), fsync=False)
