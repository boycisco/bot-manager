r"""Safe ZIP extraction.

Rejects:
  - absolute paths
  - "zip slip" entries (../../etc/passwd)
  - symlinks
  - Windows drive letters (C:\...)
"""

from __future__ import annotations

import shutil
import zipfile
from pathlib import Path


class UnsafeArchiveError(Exception):
    """Raised when a zip contains a path we refuse to extract."""


def safe_extract(zip_path: Path, dest: Path) -> None:
    """Extract zip_path into dest, refusing anything suspicious."""
    dest.mkdir(parents=True, exist_ok=True)
    dest_resolved = dest.resolve()

    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.infolist():
            _check_member(member, dest_resolved)
        # All members validated — now extract.
        zf.extractall(dest)


def _check_member(member: zipfile.ZipInfo, dest_resolved: Path) -> None:
    name = member.filename

    # Reject symlinks (mode bits in the high word)
    mode = member.external_attr >> 16
    if mode & 0o170000 == 0o120000:
        raise UnsafeArchiveError(f"symlink not allowed: {name}")

    # Reject absolute paths and Windows drive letters
    if name.startswith("/") or name.startswith("\\") or ":" in name.split("/")[0]:
        raise UnsafeArchiveError(f"absolute path not allowed: {name}")

    # Resolve and confirm it stays inside dest
    target = (dest_resolved / name).resolve()
    if target != dest_resolved and dest_resolved not in target.parents:
        raise UnsafeArchiveError(f"path escapes target dir: {name}")


def flatten_single_root(root: Path) -> None:
    """If everything is wrapped in one folder, hoist its contents up one level.

    Turns this:                into this:
      project/                   main.py
      project/main.py            requirements.txt
      project/requirements.txt
    """
    ignore = {"__MACOSX", ".DS_Store", "Thumbs.db"}
    entries = [p for p in root.iterdir() if p.name not in ignore]
    if len(entries) != 1 or not entries[0].is_dir():
        return

    inner = entries[0]
    for item in list(inner.iterdir()):
        shutil.move(str(item), str(root / item.name))
    inner.rmdir()


def cleanup_extras(root: Path) -> None:
    """Remove junk files that Macs sometimes add to zips."""
    for junk in ("__MACOSX", ".DS_Store", "Thumbs.db"):
        target = root / junk
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
        elif target.exists():
            target.unlink(missing_ok=True)