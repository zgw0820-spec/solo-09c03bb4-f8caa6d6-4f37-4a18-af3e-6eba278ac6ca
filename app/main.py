"""FastAPI service exposing path-scoped restores from read-only tar.gz backups."""

from __future__ import annotations

import logging
import os
import shutil
import threading
from contextlib import asynccontextmanager
from dataclasses import asdict

from fastapi import FastAPI, HTTPException, status
from pydantic import BaseModel, Field

from . import repair
from .repair import (
    InvalidRepairName,
    InvalidRestoreLayout,
    RepairConflict,
    RepairError,
    RepairNotFound,
    repair_replica,
)
from .restore import (
    InvalidArchive,
    InvalidSelection,
    LinkError,
    MalformedArchive,
    NotADirectory,
    PathNotFound,
    RestoreError,
    restore_directory,
)

logger = logging.getLogger("restore")
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

ARCHIVE_DIR = os.environ.get("RESTORE_ARCHIVE_DIR", "/backups")
OUTPUT_DIR = os.environ.get("RESTORE_OUTPUT_DIR", "/restored")
REPLICA_DIR = os.environ.get("RESTORE_REPLICA_DIR", "/replica")
MAX_CONCURRENT = max(1, int(os.environ.get("RESTORE_MAX_CONCURRENT", "2")))
STALE_STAGING_MAX_AGE = 24 * 60 * 60  # seconds

_restore_slots = threading.BoundedSemaphore(MAX_CONCURRENT)
# Serialises repairs: while one runs, no other repair touches either tree.
# Published restore trees are immutable by design and replica trees are only
# ever written by repairs, so holding this lock keeps both trees quiescent.
_repair_lock = threading.Lock()


class ArchiveInfo(BaseModel):
    name: str
    size_bytes: int
    modified_at: float


class RestoreRequest(BaseModel):
    archive: str = Field(..., description="Archive file name, as returned by GET /api/archives")
    path: str = Field(
        ...,
        description="Directory to restore, relative to the archive root (e.g. 'data/2024')",
        examples=["data/2024"],
    )


class RestoreResponse(BaseModel):
    requested_path: str
    restored_path: str
    directories: int
    files: int
    hardlinks: int
    bytes_written: int
    bytes_extracted: int
    archive_payload_bytes: int
    skipped_unsafe: int
    skipped_unsupported: int


class RestoreInfo(BaseModel):
    restore_id: str
    restored_path: str


class RepairRequest(BaseModel):
    restore_id: str = Field(
        ...,
        description="Published restore ID, as returned by POST /api/restores",
        examples=["9c0b1f2a4d5e4f3a9b8c7d6e5f4a3b2c"],
    )
    replica: str = Field(
        ...,
        description="Directory name on the replica mount (RESTORE_REPLICA_DIR)",
        examples=["2024-drill"],
    )


class RepairResponse(BaseModel):
    restore_id: str
    replica: str
    repaired_path: str
    directories: int
    files: int
    merged: list[list[str]] = Field(
        description="Groups of replica-relative paths re-linked into one shared inode"
    )
    split: list[list[str]] = Field(
        description="Groups of replica-relative paths that shared an inode and were separated"
    )
    bytes_copied: int


def _archive_path(name: str) -> str:
    """Resolve an archive name to a path inside the configured backup mount."""

    if not name or name != os.path.basename(name) or name in (".", ".."):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid archive name")
    path = os.path.join(ARCHIVE_DIR, name)
    if not os.path.isfile(path):
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"archive {name!r} not found")
    return path


def _cleanup_stale_staging(now: float | None = None) -> None:
    """Remove staging directories left behind by a killed process.

    Normal failures clean up after themselves; this only catches hard kills
    where the process never got to run its ``finally`` block.
    """

    import time

    staging = os.path.join(OUTPUT_DIR, ".staging")
    if not os.path.isdir(staging):
        return
    now = time.time() if now is None else now
    for entry in os.scandir(staging):
        try:
            if now - entry.stat().st_mtime > STALE_STAGING_MAX_AGE:
                shutil.rmtree(entry.path, ignore_errors=True)
                logger.warning("removed stale staging directory %s", entry.path)
        except OSError:
            continue


def _recover_replica_staging() -> None:
    """Roll back or discard repair staging left behind by a killed process.

    A repair swaps the rebuilt tree into place with two renames.  If the
    process died between them, the original tree is still parked inside the
    staging directory: move it back so the replica path is usable again.
    Anything else (never swapped, or fully swapped) is simply removed.
    """

    staging = os.path.join(REPLICA_DIR, repair.STAGING_DIRNAME)
    if not os.path.isdir(staging):
        return
    for entry in os.scandir(staging):
        if not entry.is_dir(follow_symlinks=False):
            continue
        old = os.path.join(entry.path, "old")
        target = ""
        try:
            with open(os.path.join(entry.path, "TARGET"), encoding="utf-8") as fh:
                target = fh.read().strip()
        except OSError:
            pass
        if target and repair.is_valid_name(target) and os.path.isdir(old):
            replica_path = os.path.join(REPLICA_DIR, target)
            if not os.path.exists(replica_path):
                try:
                    os.rename(old, replica_path)
                    logger.warning("recovered original replica directory %s", replica_path)
                except OSError:
                    logger.error("could not recover %s from %s", replica_path, old)
                    continue  # keep the staging directory for manual inspection
        shutil.rmtree(entry.path, ignore_errors=True)


@asynccontextmanager
async def lifespan(_: FastAPI):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    os.makedirs(REPLICA_DIR, exist_ok=True)
    _cleanup_stale_staging()
    _recover_replica_staging()
    logger.info("archive dir=%s output dir=%s replica dir=%s", ARCHIVE_DIR, OUTPUT_DIR, REPLICA_DIR)
    yield


app = FastAPI(
    title="Archive Subdirectory Restore",
    version="1.0.0",
    description=(
        "Restore a single directory (including hard links that reach outside it) "
        "from a read-only tar.gz backup, without unpacking the whole archive."
    ),
    lifespan=lifespan,
)


@app.get("/health", tags=["ops"])
def health() -> dict:
    return {"status": "ok"}


@app.get("/api/archives", response_model=list[ArchiveInfo], tags=["archives"])
def list_archives() -> list[ArchiveInfo]:
    try:
        entries = list(os.scandir(ARCHIVE_DIR))
    except FileNotFoundError:
        return []
    archives = []
    for entry in entries:
        if not entry.is_file():
            continue
        if not (entry.name.endswith(".tar.gz") or entry.name.endswith(".tgz")):
            continue
        stat = entry.stat()
        archives.append(
            ArchiveInfo(name=entry.name, size_bytes=stat.st_size, modified_at=stat.st_mtime)
        )
    return sorted(archives, key=lambda a: a.name)


@app.get("/api/restores", response_model=list[RestoreInfo], tags=["restore"])
def list_restores() -> list[RestoreInfo]:
    """List published restores (one entry per restore id)."""

    try:
        entries = list(os.scandir(OUTPUT_DIR))
    except FileNotFoundError:
        return []
    published = []
    for entry in entries:
        if entry.name.startswith(".") or not entry.is_dir(follow_symlinks=False):
            continue
        children = [c for c in os.scandir(entry.path) if not c.name.startswith(".")]
        dirs = [c for c in children if c.is_dir(follow_symlinks=False)]
        if len(children) != 1 or len(dirs) != 1:
            continue
        published.append(RestoreInfo(restore_id=entry.name, restored_path=dirs[0].path))
    return sorted(published, key=lambda r: r.restore_id)


@app.post(
    "/api/restores",
    response_model=RestoreResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["restore"],
    summary="Restore one subdirectory",
)
def create_restore(request: RestoreRequest) -> RestoreResponse:
    archive_path = _archive_path(request.archive)
    if not _restore_slots.acquire(blocking=False):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "too many concurrent restores, retry shortly",
        )
    try:
        result = restore_directory(archive_path, request.path, OUTPUT_DIR)
    except InvalidSelection as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    except NotADirectory as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except PathNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except LinkError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    except MalformedArchive as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    except InvalidArchive as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except RestoreError as exc:
        logger.exception("restore failed")
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, str(exc)) from exc
    except OSError as exc:
        logger.exception("restore failed with OS error")
        raise HTTPException(status.HTTP_507_INSUFFICIENT_STORAGE, str(exc)) from exc
    finally:
        _restore_slots.release()

    logger.info(
        "restored %s from %s -> %s (%d files, %d links, %d bytes)",
        request.path,
        request.archive,
        result.restored_path,
        result.files,
        result.hardlinks,
        result.bytes_written,
    )
    return RestoreResponse(**asdict(result))


@app.post(
    "/api/repairs",
    response_model=RepairResponse,
    tags=["repair"],
    summary="Repair hard links in a drill copy",
    description=(
        "Rebuild the hard-link structure of a drill copy on the replica mount so "
        "it matches a published restore. The restore decides which paths share an "
        "inode; replica bytes are never rewritten (a file is copied only when a "
        "split needs a new inode). The whole tree is swapped into the replica's "
        "original path in one go; conflicts (path/type mismatches, merge groups "
        "with different bytes, unsupported entry types) abort the repair and "
        "leave the original directory untouched."
    ),
)
def create_repair(request: RepairRequest) -> RepairResponse:
    if not _repair_lock.acquire(blocking=False):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "another repair is in progress, retry shortly",
        )
    try:
        result = repair_replica(OUTPUT_DIR, REPLICA_DIR, request.restore_id, request.replica)
    except InvalidRepairName as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except RepairNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except InvalidRestoreLayout as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except RepairConflict as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={"message": str(exc), "conflicts": exc.conflicts[: repair.MAX_CONFLICTS]},
        ) from exc
    except RepairError as exc:
        logger.exception("repair failed")
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, str(exc)) from exc
    except OSError as exc:
        logger.exception("repair failed with OS error")
        raise HTTPException(status.HTTP_507_INSUFFICIENT_STORAGE, str(exc)) from exc
    finally:
        _repair_lock.release()

    logger.info(
        "repaired replica %s against restore %s -> %s (%d merged, %d split, %d bytes copied)",
        request.replica,
        request.restore_id,
        result.repaired_path,
        len(result.merged),
        len(result.split),
        result.bytes_copied,
    )
    return RepairResponse(**asdict(result))
