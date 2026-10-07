"""Upload to the OneDrive folder through rclone, then read the size back to confirm."""
import json
import os
import re
import subprocess
import tempfile

TIMEOUT = 120
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class StorageError(Exception):
    pass


def safe_rel_path(rel):
    """Return rel with single slashes, or raise. Keeps every write inside the remote folder."""
    if not isinstance(rel, str) or not rel:
        raise StorageError("path is empty")
    if _CONTROL.search(rel):
        raise StorageError("path contains control characters")
    if "\\" in rel:
        raise StorageError("path contains a backslash")
    if rel.startswith("/"):
        raise StorageError("path must be relative")
    parts = rel.split("/")
    if any(p == "" for p in parts):
        raise StorageError("path has an empty segment")
    if any(p in ("..", ".") for p in parts):
        raise StorageError("path has a '.' or '..' segment")
    return "/".join(parts)


def _run(args):
    try:
        return subprocess.run(args, capture_output=True, timeout=TIMEOUT)
    except FileNotFoundError:
        raise StorageError("rclone binary not found")
    except subprocess.TimeoutExpired:
        raise StorageError(f"rclone {args[1]} timed out after {TIMEOUT} seconds")


def upload(rel_path, data):
    rel = safe_rel_path(rel_path)
    remote = os.environ.get("FACTURI_REMOTE", "").rstrip("/")
    if not remote:
        raise StorageError("FACTURI_REMOTE is not set")
    binary = os.environ.get("RCLONE_BIN", "rclone")
    target = f"{remote}/{rel}"
    fd, tmp = tempfile.mkstemp(prefix="facturi-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        # rclone output can name the remote path, never the file content
        p = _run([binary, "copyto", tmp, target])
        if p.returncode != 0:
            raise StorageError(f"rclone copyto failed with exit code {p.returncode}")
        p = _run([binary, "lsjson", target])
        if p.returncode != 0:
            raise StorageError(f"rclone lsjson failed with exit code {p.returncode}: file not found after copy")
        try:
            listing = json.loads(p.stdout.decode("utf-8"))
        except ValueError:
            raise StorageError("rclone lsjson returned unreadable output")
        if len(listing) != 1 or listing[0].get("Size") != len(data):
            got = listing[0].get("Size") if len(listing) == 1 else "no single entry"
            raise StorageError(f"size check failed: expected {len(data)}, remote has {got}")
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    return {"ok": True, "path": rel, "size": len(data)}
