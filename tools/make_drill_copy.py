"""Copy a restored drill tree onto the replica mount, the lossy way.

The copy deliberately breaks hard links (every file gets its own inode, like
a plain ``cp -r``); optionally it can also simulate a content-based
de-duplication pass that joins unrelated files with identical bytes into one
inode (``--dedup``), or corrupt one file so the repair API refuses the
replica (``--corrupt REL``).

Usage:
    python tools/make_drill_copy.py SRC DST [--dedup] [--corrupt REL]

Example (against the compose mounts):
    python tools/make_drill_copy.py restored/<restore-id>/2024 replica/2024-drill --dedup
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sys

CHUNK_SIZE = 1024 * 1024


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _dedup(root: str) -> list[list[str]]:
    """Hard-link files with identical content together; return joined groups."""

    by_content: dict[tuple[int, str], list[str]] = {}
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            path = os.path.join(dirpath, name)
            by_content.setdefault((os.path.getsize(path), _sha256(path)), []).append(path)
    joined = []
    for paths in by_content.values():
        if len(paths) < 2:
            continue
        paths.sort()
        first = paths[0]
        for other in paths[1:]:
            os.unlink(other)
            os.link(first, other)
        joined.append([os.path.relpath(p, root) for p in paths])
    return joined


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Lossy copy of a restored drill tree onto the replica mount."
    )
    parser.add_argument("src", help="restored tree to copy (e.g. restored/<id>/2024)")
    parser.add_argument("dst", help="destination on the replica mount (must not exist)")
    parser.add_argument(
        "--dedup",
        action="store_true",
        help="after copying, hard-link files with identical content together",
    )
    parser.add_argument(
        "--corrupt",
        metavar="REL",
        help="append bytes to the given file (relative to DST) to simulate tampering",
    )
    args = parser.parse_args(argv[1:])

    if os.path.exists(args.dst):
        print(f"error: {args.dst} already exists", file=sys.stderr)
        return 2
    # copy2 per file: content and metadata survive, hard links do not.
    shutil.copytree(args.src, args.dst, copy_function=shutil.copy2)
    print(f"copied {args.src} -> {args.dst} (hard links broken)")

    if args.dedup:
        for group in _dedup(args.dst):
            print("dedup joined: " + ", ".join(group))
    if args.corrupt:
        target = os.path.join(args.dst, args.corrupt)
        with open(target, "ab") as fh:
            fh.write(b"\n# corrupted after the copy\n")
        print(f"corrupted {args.corrupt}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
