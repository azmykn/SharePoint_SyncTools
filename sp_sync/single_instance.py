"""Prevent two Unified Sync Tool instances from running at once."""

from __future__ import annotations

import atexit
import os
import socket
import time
from typing import Optional, Tuple

from sp_sync.paths import project_root

_DEFAULT_LOCK_NAME = "app.instance.lock"
_held_lock_path: Optional[str] = None


def default_lock_path() -> str:
    root = project_root()
    data = os.path.join(root, "data")
    os.makedirs(data, exist_ok=True)
    return os.path.join(data, _DEFAULT_LOCK_NAME)


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Exists but not owned by us — treat as alive.
        return True
    except OSError:
        return False
    return True


def _read_lock_pid(lock_path: str) -> Optional[int]:
    try:
        with open(lock_path, encoding="utf-8") as f:
            raw = (f.read() or "").strip().splitlines()[0:1]
        if not raw:
            return None
        return int(raw[0].strip())
    except (OSError, ValueError, IndexError):
        return None


def port_in_use(host: str, port: int, timeout: float = 0.35) -> bool:
    """True if something accepts TCP connections on host:port."""
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def _write_lock(lock_path: str, pid: int) -> None:
    parent = os.path.dirname(os.path.abspath(lock_path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = lock_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(f"{pid}\n")
        f.write(f"started={time.strftime('%Y-%m-%d %H:%M:%S')}\n")
    os.replace(tmp, lock_path)


def release_app_lock(lock_path: Optional[str] = None) -> None:
    """Remove the lock file if it belongs to this process."""
    global _held_lock_path
    path = lock_path or _held_lock_path
    if not path:
        return
    try:
        pid = _read_lock_pid(path)
        if pid is None or pid == os.getpid():
            if os.path.exists(path):
                os.remove(path)
    except OSError:
        pass
    if _held_lock_path == path:
        _held_lock_path = None


def acquire_app_lock(
    *,
    lock_path: Optional[str] = None,
    host: str = "127.0.0.1",
    port: Optional[int] = None,
    check_port: bool = True,
) -> Tuple[bool, str]:
    """Acquire a single-instance lock.

    Returns ``(True, message)`` on success, ``(False, clear_error)`` if another
    live instance holds the lock or the configured port is already in use.
    Stale locks (dead PID) are removed automatically.
    """
    global _held_lock_path
    path = os.path.abspath(lock_path or default_lock_path())

    if check_port and port is not None:
        if port_in_use(host, int(port)):
            return (
                False,
                f"Another Sync Tool instance appears to be running on {host}:{port}. "
                f"Stop it first (or choose a different SP_SYNC_PORT).",
            )

    if os.path.exists(path):
        existing = _read_lock_pid(path)
        if existing is not None and existing == os.getpid():
            _held_lock_path = path
            return True, f"Already holding instance lock {path}"
        if existing is not None and _pid_is_alive(existing):
            return (
                False,
                f"Another Sync Tool instance is already running (PID {existing}, lock {path}). "
                f"Stop that process before starting a second copy.",
            )
        # Stale or unreadable lock — take over.
        try:
            os.remove(path)
        except OSError:
            return False, f"Could not remove stale lock file: {path}"

    try:
        _write_lock(path, os.getpid())
    except OSError as exc:
        return False, f"Could not create lock file {path}: {exc}"

    # Confirm we won (simple race check).
    owner = _read_lock_pid(path)
    if owner != os.getpid():
        return False, f"Lost lock race for {path} (owner PID {owner})."

    _held_lock_path = path
    atexit.register(release_app_lock, path)
    return True, f"Acquired instance lock {path}"
