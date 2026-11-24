"""Shared test fixtures."""

from __future__ import annotations

import io
import os
import sys
import tarfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

import make_sample_archive  # noqa: E402


def _add_dir(tar: tarfile.TarFile, name: str) -> None:
    info = tarfile.TarInfo(name)
    info.type = tarfile.DIRTYPE
    info.mode = 0o755
    info.mtime = 1_700_000_000
    tar.addfile(info)


def _add_file(tar: tarfile.TarFile, name: str, data: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mode = 0o644
    info.mtime = 1_700_000_000
    tar.addfile(info, io.BytesIO(data))


def _add_link(tar: tarfile.TarFile, name: str, target: str) -> None:
    info = tarfile.TarInfo(name)
    info.type = tarfile.LNKTYPE
    info.linkname = target
    info.mtime = 1_700_000_000
    tar.addfile(info)


def _add_symlink(tar: tarfile.TarFile, name: str, target: str) -> None:
    info = tarfile.TarInfo(name)
    info.type = tarfile.SYMTYPE
    info.linkname = target
    info.mtime = 1_700_000_000
    tar.addfile(info)


@pytest.fixture
def archive_builder(tmp_path):
    """Return a function that builds a tar.gz from a member spec list."""

    def build(name: str, members: list[tuple]) -> str:
        path = tmp_path / name
        with tarfile.open(path, mode="w:gz", format=tarfile.GNU_FORMAT) as tar:
            for member in members:
                kind = member[0]
                if kind == "dir":
                    _add_dir(tar, member[1])
                elif kind == "file":
                    _add_file(tar, member[1], member[2])
                elif kind == "link":
                    _add_link(tar, member[1], member[2])
                elif kind == "symlink":
                    _add_symlink(tar, member[1], member[2])
                else:  # pragma: no cover
                    raise ValueError(kind)
        return str(path)

    return build


@pytest.fixture
def sample_archive(tmp_path) -> str:
    path = tmp_path / "sample.tar.gz"
    make_sample_archive.build(str(path))
    return str(path)
