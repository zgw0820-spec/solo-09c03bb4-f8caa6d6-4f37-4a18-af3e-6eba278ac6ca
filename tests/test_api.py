"""API-level tests for the FastAPI service."""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    import app.main as main

    archives = tmp_path / "backups"
    restored = tmp_path / "restored"
    archives.mkdir()
    monkeypatch.setattr(main, "ARCHIVE_DIR", str(archives))
    monkeypatch.setattr(main, "OUTPUT_DIR", str(restored))
    # Re-create the app-independent state used by lifespan.
    with TestClient(main.app) as c:
        yield c, archives, restored


def _make_archive(directory, name="backup.tar.gz"):
    import sys

    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
    import make_sample_archive

    path = os.path.join(str(directory), name)
    make_sample_archive.build(path)
    return name


def test_health(client):
    c, _a, _r = client
    assert c.get("/health").json() == {"status": "ok"}


def test_list_archives(client):
    c, archives, _r = client
    _make_archive(archives)
    response = c.get("/api/archives")
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["name"] == "backup.tar.gz"
    assert body[0]["size_bytes"] > 0


def test_restore_via_api(client):
    c, archives, restored = client
    name = _make_archive(archives)
    response = c.post("/api/restores", json={"archive": name, "path": "data/2024"})
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["requested_path"] == "data/2024"
    assert body["files"] >= 3
    assert body["hardlinks"] >= 5
    restored_path = body["restored_path"]
    assert restored_path.startswith(str(restored))
    assert os.path.isfile(os.path.join(restored_path, "inside.txt"))
    assert os.path.isfile(os.path.join(restored_path, "nested", "deep.txt"))
    # Links that were in the archive are real hard links sharing an inode.
    a = os.stat(os.path.join(restored_path, "shared_link")).st_ino
    b = os.stat(os.path.join(restored_path, "chained_link")).st_ino
    assert a == b
    # Sibling year not restored.
    assert not os.path.exists(os.path.join(restored_path, "..", "2025"))


def test_restore_missing_archive(client):
    c, _a, _r = client
    response = c.post("/api/restores", json={"archive": "nope.tar.gz", "path": "data/2024"})
    assert response.status_code == 404


def test_restore_rejects_archive_traversal(client):
    c, _a, _r = client
    response = c.post("/api/restores", json={"archive": "../etc/passwd", "path": "data"})
    assert response.status_code == 400


def test_restore_missing_subdirectory(client):
    c, archives, _r = client
    name = _make_archive(archives)
    response = c.post("/api/restores", json={"archive": name, "path": "data/1999"})
    assert response.status_code == 404


def test_restore_missing_hardlink_target(client, tmp_path):
    c, archives, _r = client
    import tarfile

    path = archives / "broken.tar.gz"
    with tarfile.open(path, "w:gz") as tar:
        base = tarfile.TarInfo("data/2024")
        base.type = tarfile.DIRTYPE
        tar.addfile(base)
        link = tarfile.TarInfo("data/2024/broken")
        link.type = tarfile.LNKTYPE
        link.linkname = "data/ghost"
        tar.addfile(link)
    response = c.post("/api/restores", json={"archive": "broken.tar.gz", "path": "data/2024"})
    assert response.status_code == 422
    assert "missing" in response.json()["detail"].lower()


def test_restore_invalid_selection(client):
    c, archives, _r = client
    name = _make_archive(archives)
    response = c.post("/api/restores", json={"archive": name, "path": "../etc"})
    assert response.status_code == 422


def test_concurrent_restores_get_separate_directories(client):
    import threading

    c, archives, restored = client
    name = _make_archive(archives)
    results = []
    lock = threading.Lock()

    def worker():
        response = c.post("/api/restores", json={"archive": name, "path": "data/2024"})
        with lock:
            results.append(response)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    codes = sorted(r.status_code for r in results)
    assert all(code in (201, 429) for code in codes), codes
    assert 201 in codes
    paths = {r.json()["restored_path"] for r in results if r.status_code == 201}
    assert len(paths) == len([r for r in results if r.status_code == 201])
