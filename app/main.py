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
MAX_CONCURRENT = max(1, int(os.environ.get("RESTORE_MAX_CONCURRENT", "2")))
STALE_STAGING_MAX_AGE = 24 * 60 * 60  # seconds

_restore_slots = threading.BoundedSemaphore(MAX_CONCURRENT)


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


@asynccontextmanager
async def lifespan(_: FastAPI):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    _cleanup_stale_staging()
    logger.info("archive dir=%s output dir=%s", ARCHIVE_DIR, OUTPUT_DIR)
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
