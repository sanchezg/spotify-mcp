"""Private, atomic operation records and nonblocking process locks (macOS/Linux)."""

import contextlib
import fcntl
import json
import os
import re
import stat
import tempfile
import time
from pathlib import Path


class Busy(RuntimeError):
    pass


def private_directory(path: Path):
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise RuntimeError("State directory must be owned by you and not be a symlink")
    path.chmod(0o700)


def identifier(value: str) -> str:
    if not re.fullmatch(r"[a-f0-9]{32}", value):
        raise ValueError("Invalid operation ID")
    return value


class Store:
    def __init__(self, root: Path):
        self.root = root
        private_directory(root)

    def path(self, key):
        return self.root / (identifier(key) + ".json")

    @contextlib.contextmanager
    def lock(self, key):
        path = self.root / (identifier(key) + ".lock")
        fd = os.open(
            path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600
        )
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise RuntimeError("Unsafe lock file")
            os.fchmod(fd, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise Busy("Operation already running") from exc
            yield
        finally:
            os.close(fd)
        # Keep the lock inode: unlinking here permits races with another opener.

    def read(self, key):
        fd = os.open(self.path(key), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd) as f:
            info = os.fstat(f.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise RuntimeError("Unsafe state file")
            os.fchmod(f.fileno(), 0o600)
            data = json.load(f)
        if data.get("id") != key:
            raise ValueError("Invalid operation record")
        return data

    def write(self, data):
        destination = self.path(data["id"])
        fd, temporary = tempfile.mkstemp(prefix=".operation-", dir=self.root)
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, ensure_ascii=True)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temporary, destination)
            directory = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def purge(self, key):
        with self.lock(key):
            self.path(key).unlink(missing_ok=True)

    def cleanup(self):
        for path in self.root.glob("*.json"):
            try:
                with self.lock(path.stem):
                    data = self.read(path.stem)
                    if (
                        data["status"]
                        in ("ready", "needs_resolution", "creation_rejected")
                        and data["expires_at"] < time.time()
                        or data["status"] == "complete"
                        and data["receipt_expires_at"] < time.time()
                    ):
                        path.unlink()
            except (Busy, FileNotFoundError):
                continue
