"""Build the example backup archive used by the demo and the test suite.

Layout (deliberately exercises every hard-link case the service supports):

    data/2024/inside.txt              regular file, inside the selection
    data/2024/report.bin              regular file, inside the selection
    data/2024/nested/deep.txt         regular file two levels down
    data/2024/shared_link             hard link -> ../shared/blob.bin   (outside, appears earlier)
    data/2024/forward_link            hard link -> ../shared/forward.bin (outside, appears LATER)
    data/2024/chained_link            hard link -> chained_mid
    data/2024/chained_mid             hard link -> ../shared/blob.bin  (chained + outside)
    data/2024/three_way_a             hard link -> ../shared/blob.bin
    data/2024/three_way_b             hard link -> three_way_a        (links to a link)
    data/2024/self_reference          hard link -> data/2024/inside.txt (archive-root path)
    data/2025/...                     a sibling year, must NOT be restored
    shared/blob.bin                   referenced by links, outside the selection
    shared/forward.bin                appears AFTER the link that needs it

Usage:  python tools/make_sample_archive.py OUT.tar.gz
"""

from __future__ import annotations

import io
import os
import sys
import tarfile

MTIME = 1_700_000_000


def _add_dir(tar: tarfile.TarFile, name: str, mode: int = 0o755) -> None:
    info = tarfile.TarInfo(name)
    info.type = tarfile.DIRTYPE
    info.mode = mode
    info.mtime = MTIME
    tar.addfile(info)


def _add_file(tar: tarfile.TarFile, name: str, data: bytes, mode: int = 0o644) -> None:
    info = tarfile.TarInfo(name)
    info.mode = mode
    info.mtime = MTIME
    info.size = len(data)
    tar.addfile(info, io.BytesIO(data))


def _add_link(tar: tarfile.TarFile, name: str, target: str) -> None:
    info = tarfile.TarInfo(name)
    info.type = tarfile.LNKTYPE
    info.linkname = target
    info.mtime = MTIME
    tar.addfile(info)


def build(path: str) -> None:
    blob = b"BLOB-CONTENT-" * 512  # ~6.5 KiB, larger than one standard block
    forward = b"FORWARD-DATA-" * 256
    report = bytes(range(256)) * 16
    with tarfile.open(path, mode="w:gz", format=tarfile.GNU_FORMAT) as tar:
        _add_dir(tar, "data")
        _add_dir(tar, "data/2024")
        _add_dir(tar, "data/2024/nested")
        _add_dir(tar, "data/2025")
        _add_dir(tar, "shared")

        # The external original is written BEFORE the links that use it.
        _add_file(tar, "shared/blob.bin", blob)

        _add_file(tar, "data/2024/inside.txt", b"inside the selected directory\n")
        _add_file(tar, "data/2024/report.bin", report)
        _add_file(tar, "data/2024/nested/deep.txt", b"deep file\n")

        _add_link(tar, "data/2024/shared_link", "shared/blob.bin")
        _add_link(tar, "data/2024/chained_mid", "shared/blob.bin")
        _add_link(tar, "data/2024/chained_link", "data/2024/chained_mid")
        _add_link(tar, "data/2024/three_way_a", "shared/blob.bin")
        _add_link(tar, "data/2024/three_way_b", "data/2024/three_way_a")
        _add_link(tar, "data/2024/self_reference", "data/2024/inside.txt")

        # This link points at a file that only appears later in the archive.
        _add_link(tar, "data/2024/forward_link", "shared/forward.bin")
        _add_file(tar, "shared/forward.bin", forward)

        # Unrelated sibling data: must be skipped entirely.
        _add_file(tar, "data/2025/other.txt", b"do not restore me\n" * 4096)
        _add_file(tar, "unrelated.bin", b"UNRELATED" * 100_000)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    out = argv[1]
    build(out)
    print(f"wrote {out} ({os.path.getsize(out)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
