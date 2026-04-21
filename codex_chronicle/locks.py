"""File-lock helpers.

Two locks:
- Singleton daemon lock: fcntl flock on ~/.codex-chronicle/daemon.pid.
- Processing lock: fcntl flock on ~/.codex-chronicle/processing.lock.
  Held by the daemon during _process_batch and by `codex-chronicle process`
  for its whole run. Prevents duplicate LLM calls and chronicle.md write races.
"""
from __future__ import annotations

import contextlib
import fcntl
import os
from typing import Iterator, Optional

from .config import pid_file, processing_lock_path


_daemon_lock_fd: Optional[int] = None


def acquire_daemon_lock() -> bool:
    global _daemon_lock_fd
    pid_file().parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(pid_file()), os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.write(fd, str(os.getpid()).encode())
        os.ftruncate(fd, len(str(os.getpid())))
        _daemon_lock_fd = fd
        return True
    except (OSError, IOError):
        os.close(fd)
        return False


def daemon_lock_still_valid() -> bool:
    if _daemon_lock_fd is None:
        return False
    try:
        fd_stat = os.fstat(_daemon_lock_fd)
        path_stat = os.stat(str(pid_file()))
        return (fd_stat.st_ino == path_stat.st_ino
                and fd_stat.st_dev == path_stat.st_dev)
    except OSError:
        return False


def daemon_is_running() -> tuple[bool, Optional[int]]:
    if not pid_file().exists():
        return False, None
    try:
        pid = int(pid_file().read_text().strip())
        os.kill(pid, 0)
        return True, pid
    except (ValueError, OSError):
        return False, None


def _reset_daemon_lock_for_tests() -> None:
    global _daemon_lock_fd
    if _daemon_lock_fd is not None:
        try:
            os.close(_daemon_lock_fd)
        except OSError:
            pass
    _daemon_lock_fd = None


@contextlib.contextmanager
def processing_lock(blocking: bool = True) -> Iterator[bool]:
    processing_lock_path().parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(processing_lock_path()), os.O_CREAT | os.O_WRONLY, 0o600)
    flags = fcntl.LOCK_EX if blocking else (fcntl.LOCK_EX | fcntl.LOCK_NB)
    acquired = False
    try:
        try:
            fcntl.flock(fd, flags)
            acquired = True
        except (OSError, IOError):
            acquired = False
        yield acquired
    finally:
        if acquired:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
        os.close(fd)


def processing_lock_held() -> bool:
    if not processing_lock_path().exists():
        return False
    fd = os.open(str(processing_lock_path()), os.O_WRONLY)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, IOError):
            return True
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
            return False
    finally:
        os.close(fd)
