"""Repair the hard links of a drill copy so they match a published restore.

Scenario
--------
A restored drill directory was copied onto the replica mount with a plain
copy (which silently breaks hard links into independent inodes) and/or run
through content-based de-duplication (which joins unrelated files that happen
to have identical bytes into one inode).  This module rebuilds the copy's
hard-link structure so it matches the published restore exactly.

Guarantees
----------
* The published restore tree is the authority for *structure*: the set of
  paths, each path's type (directory / regular file) and which paths share
  one inode.  Only directories and regular files are supported; anything
  else (symlinks, devices, fifos, ...) is a conflict and aborts the repair.
* Replica bytes are never rewritten.  Merge groups whose replica members
  have different bytes are conflicts and abort the repair.  Files are only
  copied when a split needs a fresh inode; every other name is re-created
  with ``os.link``, so no byte of the replica is ever modified.
* The repaired tree is assembled in a staging directory on the same mount
  and swapped into the replica's original path with renames.  Any failure
  before the swap leaves the original directory untouched and usable; a
  crash between the two renames is rolled back on the next service start
  (see ``main._recover_replica_staging``).
* Both trees are quiescent while a repair runs: the API serialises repairs
  with a process-wide lock, published restore trees are immutable by design
  and replica trees are only ever written by repairs.
"""

from __future__ import annotations

import logging
import os
import shutil
import uuid
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

CHUNK_SIZE = 1024 * 1024  # 1 MiB
MAX_CONFLICTS = 100  # cap on the conflict list returned to the caller

DIR = "dir"
FILE = "file"

STAGING_DIRNAME = ".repair"  # staging root inside the replica mount


class RepairError(Exception):
    """Base class for all repair failures."""


class InvalidRepairName(RepairError):
    """The restore id or replica name is not a safe single path component."""


class RepairNotFound(RepairError):
    """The restore id or the replica directory does not exist."""


class InvalidRestoreLayout(RepairError):
    """The restore id does not point at one published restore directory."""


class RepairConflict(RepairError):
    """The replica cannot be reconciled with the restore; nothing changed."""

    def __init__(self, conflicts: list[str]) -> None:
        self.conflicts = conflicts
        super().__init__(
            f"replica conflicts with the published restore ({len(conflicts)} conflict(s))"
        )


@dataclass
class RepairResult:
    restore_id: str
    replica: str
    repaired_path: str = ""
    directories: int = 0
    files: int = 0
    merged: list[list[str]] = field(default_factory=list)
    split: list[list[str]] = field(default_factory=list)
    bytes_copied: int = 0


def is_valid_name(value: str) -> bool:
    """A single, non-hidden path component (no separators, no dotfiles)."""

    return (
        isinstance(value, str)
        and bool(value)
        and value == os.path.basename(value)
        and not value.startswith(".")
    )


def _validate_name(value: str, what: str) -> None:
    if not is_valid_name(value):
        raise InvalidRepairName(f"invalid {what}: {value!r}")


@dataclass
class _Tree:
    """Structural snapshot of one tree: kinds, inodes and metadata per path."""

    root: str
    kinds: dict[str, str]  # rel path -> DIR/FILE (the root itself is "")
    inodes: dict[str, tuple[int, int]]  # rel path -> (st_dev, st_ino), files only
    stats: dict[str, os.stat_result]  # rel path -> metadata snapshot

    def path(self, rel: str) -> str:
        if not rel:
            return self.root
        return os.path.join(self.root, *rel.split("/"))


def _scan_tree(root: str, label: str, conflicts: list[str]) -> _Tree:
    """Walk ``root`` without following symlinks; record structure and inodes."""

    tree = _Tree(root=root, kinds={"": DIR}, inodes={}, stats={"": os.stat(root)})
    stack = [""]
    while stack:
        rel = stack.pop()
        with os.scandir(tree.path(rel)) as entries:
            for entry in entries:
                child = entry.name if not rel else f"{rel}/{entry.name}"
                try:
                    is_dir = entry.is_dir(follow_symlinks=False)
                    is_file = entry.is_file(follow_symlinks=False)
                except OSError as exc:
                    conflicts.append(f"{label}: cannot inspect {child!r}: {exc}")
                    continue
                if is_dir:
                    tree.kinds[child] = DIR
                    tree.stats[child] = entry.stat(follow_symlinks=False)
                    stack.append(child)
                elif is_file:
                    stat = entry.stat(follow_symlinks=False)
                    tree.kinds[child] = FILE
                    tree.inodes[child] = (stat.st_dev, stat.st_ino)
                    tree.stats[child] = stat
                else:
                    conflicts.append(
                        f"{label}: unsupported entry type (only directories and "
                        f"regular files are supported): {child!r}"
                    )
    return tree


def _inode_groups(tree: _Tree) -> list[list[str]]:
    """Sorted list of sorted path lists, one per inode used in the tree."""

    by_inode: dict[tuple[int, int], list[str]] = {}
    for rel, key in tree.inodes.items():
        by_inode.setdefault(key, []).append(rel)
    return sorted((sorted(members) for members in by_inode.values()), key=lambda g: g[0])


def _compare_structures(restore: _Tree, replica: _Tree, conflicts: list[str]) -> None:
    for rel, kind in sorted(restore.kinds.items()):
        other = replica.kinds.get(rel)
        if other is None:
            conflicts.append(f"missing in replica: {rel!r} ({kind} in the restore)")
        elif other != kind:
            conflicts.append(
                f"type mismatch: {rel!r} is a {kind} in the restore"
                f" but a {other} in the replica"
            )
    for rel, kind in sorted(replica.kinds.items()):
        if rel not in restore.kinds:
            conflicts.append(
                f"unexpected in replica: {rel!r} ({kind}, not present in the restore)"
            )


def _same_bytes(first: str, second: str) -> bool:
    with open(first, "rb") as one, open(second, "rb") as two:
        while True:
            chunk_one = one.read(CHUNK_SIZE)
            chunk_two = two.read(CHUNK_SIZE)
            if chunk_one != chunk_two:
                return False
            if not chunk_one:
                return True


def _check_merge_groups(restore: _Tree, replica: _Tree, conflicts: list[str]) -> None:
    """Every restore group must be mergeable without changing replica bytes."""

    for group in _inode_groups(restore):
        if len(group) < 2:
            continue
        if len({replica.stats[rel].st_size for rel in group}) != 1:
            conflicts.append(
                "merge group members have different bytes (sizes differ): "
                + ", ".join(group)
            )
            continue
        first = replica.path(group[0])
        if any(not _same_bytes(first, replica.path(rel)) for rel in group[1:]):
            conflicts.append(
                "merge group members have different bytes: " + ", ".join(group)
            )


@dataclass
class _GroupPlan:
    paths: list[str]  # one restore group, sorted
    source: str  # replica rel path providing the inode (or the bytes to copy)
    copy: bool  # True when a fresh inode must be created for this group


def _plan_groups(restore: _Tree, replica: _Tree) -> list[_GroupPlan]:
    """Assign every restore group one source inode from the replica.

    A replica inode may be reused (hard-linked into the repaired tree) by at
    most one restore group; every other group that was wrongly joined to it
    gets a fresh inode by copying the bytes of one of its members.  Groups
    whose members were split across several replica inodes reuse the first
    unclaimed one and are re-linked without copying a single byte.
    """

    claimed: set[tuple[int, int]] = set()
    plans = []
    for group in _inode_groups(restore):
        inodes: list[tuple[int, int]] = []
        for rel in group:
            key = replica.inodes[rel]
            if key not in inodes:
                inodes.append(key)
        source_inode = next((key for key in inodes if key not in claimed), None)
        copy = source_inode is None
        if copy:
            source_inode = inodes[0]
        else:
            claimed.add(source_inode)
        source = next(rel for rel in group if replica.inodes[rel] == source_inode)
        plans.append(_GroupPlan(paths=group, source=source, copy=copy))
    return plans


def _merged_groups(restore: _Tree, replica: _Tree) -> list[list[str]]:
    """Restore groups whose replica members were spread over several inodes."""

    return [
        group
        for group in _inode_groups(restore)
        if len(group) > 1 and len({replica.inodes[rel] for rel in group}) > 1
    ]


def _split_groups(restore: _Tree, replica: _Tree) -> list[list[str]]:
    """Replica inodes wrongly shared by paths of several restore groups."""

    group_of: dict[str, int] = {}
    for index, group in enumerate(_inode_groups(restore)):
        for rel in group:
            group_of[rel] = index
    by_inode: dict[tuple[int, int], list[str]] = {}
    for rel, key in replica.inodes.items():
        by_inode.setdefault(key, []).append(rel)
    return sorted(
        (
            sorted(members)
            for members in by_inode.values()
            if len({group_of[rel] for rel in members}) > 1
        ),
        key=lambda g: g[0],
    )


def _fsync_dir(path: str) -> None:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _file_mode(mode: int) -> int:
    # Keep the replica's permissions but always let the owner read/write, so
    # the copied file stays usable and can be cleaned up on failure.
    return (mode & 0o777) | 0o600


def _dir_mode(mode: int) -> int:
    return (mode & 0o777) | 0o700


def _copy_file(src: str, dst: str, stat: os.stat_result, fsync: bool) -> int:
    """Copy one file onto a fresh inode, preserving mode and timestamps."""

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(dst, flags, 0o600)
    written = 0
    with os.fdopen(fd, "wb") as out, open(src, "rb") as inp:
        while True:
            chunk = inp.read(CHUNK_SIZE)
            if not chunk:
                break
            out.write(chunk)
            written += len(chunk)
        out.flush()
        os.fchmod(out.fileno(), _file_mode(stat.st_mode))
        if fsync:
            os.fsync(out.fileno())
    os.utime(dst, (stat.st_atime, stat.st_mtime))
    return written


def _build_tree(
    staged: str,
    restore: _Tree,
    replica: _Tree,
    plans: list[_GroupPlan],
    fsync: bool,
) -> int:
    """Assemble the repaired tree under ``staged``; return copied byte count."""

    bytes_copied = 0
    dirs = sorted(
        (rel for rel, kind in restore.kinds.items() if kind == DIR and rel),
        key=lambda rel: rel.count("/"),
    )
    for rel in dirs:
        os.mkdir(os.path.join(staged, *rel.split("/")), 0o700)

    for plan in plans:
        anchor = plan.paths[0]
        anchor_dest = os.path.join(staged, *anchor.split("/"))
        if plan.copy:
            bytes_copied += _copy_file(
                replica.path(plan.source), anchor_dest, replica.stats[plan.source], fsync
            )
        else:
            # Reuse the replica's inode: its bytes and metadata are kept as-is.
            os.link(replica.path(plan.source), anchor_dest)
        for rel in plan.paths[1:]:
            os.link(anchor_dest, os.path.join(staged, *rel.split("/")))

    # Directory metadata last, deepest first (creating files inside a
    # directory touches its mtime, and linked files keep their inode's
    # metadata untouched on purpose).
    for rel in sorted(dirs, key=lambda r: r.count("/"), reverse=True) + [""]:
        stat = replica.stats[rel]
        path = os.path.join(staged, *rel.split("/")) if rel else staged
        try:
            os.chmod(path, _dir_mode(stat.st_mode))
            os.utime(path, (stat.st_atime, stat.st_mtime))
            if fsync:
                _fsync_dir(path)
        except OSError as exc:  # pragma: no cover - unusual filesystem
            logger.warning("could not finalise directory %s: %s", rel or ".", exc)
    return bytes_copied


def _verify(staged_root: str, restore: _Tree) -> None:
    """Structural self-check: the staged tree must match the restore exactly."""

    problems: list[str] = []
    rebuilt = _scan_tree(staged_root, "repaired tree", problems)
    if problems:
        raise RepairError(
            "internal error while verifying the repaired tree: " + "; ".join(problems)
        )
    if rebuilt.kinds != restore.kinds:
        raise RepairError("internal error: repaired tree structure diverged from the restore")
    if _inode_groups(rebuilt) != _inode_groups(restore):
        raise RepairError("internal error: repaired tree link groups diverged from the restore")


def _restore_tree_root(restore_root: str, restore_id: str) -> str:
    base = os.path.join(restore_root, restore_id)
    if os.path.islink(base) or not os.path.isdir(base):
        raise RepairNotFound(f"restore {restore_id!r} not found")
    try:
        entries = list(os.scandir(base))
    except FileNotFoundError:
        raise RepairNotFound(f"restore {restore_id!r} not found") from None
    if len(entries) != 1 or not entries[0].is_dir(follow_symlinks=False):
        raise InvalidRestoreLayout(
            f"restore {restore_id!r} does not contain exactly one restored directory"
        )
    return entries[0].path


def repair_replica(
    restore_root: str,
    replica_root: str,
    restore_id: str,
    name: str,
    *,
    fsync: bool | None = None,
) -> RepairResult:
    """Rebuild ``name``'s hard links to match the published ``restore_id``.

    The replica directory keeps its own bytes; only its link structure is
    rewritten, in one swap.  Raises before touching anything on conflict.
    """

    _validate_name(restore_id, "restore id")
    _validate_name(name, "replica directory name")
    if fsync is None:
        fsync = os.environ.get("RESTORE_FSYNC", "1").lower() not in ("0", "false", "no")

    restore_root = os.path.abspath(restore_root)
    replica_root = os.path.abspath(replica_root)
    restore_tree_root = _restore_tree_root(restore_root, restore_id)

    if not os.path.isdir(replica_root):
        raise RepairNotFound(f"replica mount {replica_root!r} is not available")
    replica_path = os.path.join(replica_root, name)
    if os.path.islink(replica_path):
        raise InvalidRepairName(f"replica {name!r} must be a real directory, not a symlink")
    if not os.path.isdir(replica_path):
        raise RepairNotFound(f"replica directory {name!r} not found on the replica mount")

    # Phase 1: read-only comparison.  Any conflict aborts before one write.
    conflicts: list[str] = []
    restore = _scan_tree(restore_tree_root, "restore", conflicts)
    replica = _scan_tree(replica_path, "replica", conflicts)
    if not conflicts:
        _compare_structures(restore, replica, conflicts)
    if not conflicts:
        _check_merge_groups(restore, replica, conflicts)
    if conflicts:
        raise RepairConflict(conflicts)

    merged = _merged_groups(restore, replica)
    split = _split_groups(restore, replica)
    plans = _plan_groups(restore, replica)

    # Phase 2: assemble the repaired tree on the same mount (rename-able).
    staging_root = os.path.join(replica_root, STAGING_DIRNAME)
    os.makedirs(staging_root, mode=0o700, exist_ok=True)
    token_dir = os.path.join(staging_root, uuid.uuid4().hex)
    staged = os.path.join(token_dir, "tree")
    old = os.path.join(token_dir, "old")
    os.makedirs(staged, mode=0o700)
    try:
        bytes_copied = _build_tree(staged, restore, replica, plans, fsync)
        _verify(staged, restore)

        # Phase 3: swap.  The original stays in place until the repaired tree
        # is complete and verified; if swapping fails it is renamed back.
        marker = os.path.join(token_dir, "TARGET")
        with open(marker, "w", encoding="utf-8") as fh:
            fh.write(name)
            fh.flush()
            os.fsync(fh.fileno())
        os.rename(replica_path, old)
        try:
            os.rename(staged, replica_path)
        except BaseException:
            os.rename(old, replica_path)  # roll back: original stays usable
            raise
        shutil.rmtree(old, ignore_errors=True)  # drop the superseded tree
        if fsync:
            _fsync_dir(replica_root)
    except BaseException:
        # Never delete the original tree while the replica path is empty.
        if os.path.isdir(old) and not os.path.exists(replica_path):
            try:
                os.rename(old, replica_path)
            except OSError:
                logger.error("could not put back original replica %s", replica_path)
        shutil.rmtree(token_dir, ignore_errors=True)
        raise
    shutil.rmtree(token_dir, ignore_errors=True)

    logger.info(
        "repaired replica %s against restore %s: %d merged group(s), %d split group(s), %d byte(s) copied",
        name,
        restore_id,
        len(merged),
        len(split),
        bytes_copied,
    )
    return RepairResult(
        restore_id=restore_id,
        replica=name,
        repaired_path=replica_path,
        directories=sum(1 for kind in restore.kinds.values() if kind == DIR),
        files=len(restore.inodes),
        merged=merged,
        split=split,
        bytes_copied=bytes_copied,
    )
