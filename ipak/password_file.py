"""Explicit password-reset output, separate from application logs and CSV drafts."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import stat


class PasswordFile:
    def __init__(self, path=None):
        self.path = Path(path) if path is not None else Path.cwd() / "yyy"

    @contextmanager
    def open(self):
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK
        descriptor = os.open(self.path, flags, 0o600)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
                raise OSError("Password output must be a regular file owned by the current user")
            if stat.S_IMODE(info.st_mode) & 0o077:
                raise OSError("Password output must have owner-only permissions (chmod 600)")
            yield descriptor
        finally:
            os.close(descriptor)

    def append(self, descriptor, result):
        record = {"time_utc": datetime.now(timezone.utc).isoformat(),
                  "login": result.target, "temporary_password": result.secret}
        payload = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        try:
            while payload:
                written = os.write(descriptor, payload)
                if not written:
                    raise OSError("Password output write was incomplete")
                payload = payload[written:]
            os.fsync(descriptor)
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
