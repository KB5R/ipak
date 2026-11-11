"""Minimal interaction diagnostics; never record field values or credentials."""

from datetime import datetime, timezone
import os


def trace(event: str) -> None:
    # Callers pass only fixed event names, widget IDs and result counts.
    try:
        descriptor = os.open("ipak-actions.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        try:
            os.write(descriptor, f"{datetime.now(timezone.utc).isoformat()} pid={os.getpid()} {event}\n".encode())
        finally:
            os.close(descriptor)
    except OSError:
        pass  # Diagnostics must never prevent an operation.
