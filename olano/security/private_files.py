"""Owner-only file writes for credential material.

A token file used to be written with the process umask and ``chmod``ed to
``0600`` a moment later, which left a window in which another local user could
read it. ``write_private_text`` creates the temporary file ``0600`` from the
first byte (``tempfile.mkstemp``, which also makes the name unique per call),
fsyncs, and renames it into place, so the final file is never
observable in a readable or partially written state.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path


def write_private_text(path: Path, content: str, *, encoding: str = "utf-8") -> None:
    """Atomically write ``content`` to ``path`` with mode ``0600``."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # mkstemp creates the file 0600 and with a name unique per call, so two
    # threads writing the same path never share a temp file.
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(content)
            f.flush()
            with contextlib.suppress(OSError):
                os.fsync(f.fileno())
        tmp.replace(path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
    # The new inode already carries 0600; the chmod covers filesystems that
    # ignore the mode argument to ``open`` (some FUSE mounts).
    with contextlib.suppress(OSError):
        path.chmod(0o600)


def private_dir(path: Path) -> Path:
    """``mkdir -p`` ``path`` as ``0700`` (tightening an existing directory too)."""
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    with contextlib.suppress(OSError):
        path.chmod(0o700)
    return path
