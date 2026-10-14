"""Test support shared by the IAM tests: a notes-like manifest and a fake clock."""

from __future__ import annotations

import socket
import time
from typing import Any
from urllib.parse import urlsplit

NOTES: dict[str, Any] = {
    "schema_version": 1,
    "resource": "notes",
    "operations": {
        "read_note": {"permission": "read", "requestable": True, "target": "key"},
        "write_note": {"permission": "write", "requestable": True, "target": "key"},
        "admin": {"permission": "manage"},
        "health": {"public": True},
    },
    "http": {
        "GET /notes/{key}": "read_note",
        "PUT /notes/{key}": "write_note",
        "POST /admin": "admin",
        "GET /health": "health",
    },
}


class Clock:
    """Mutable integer-second clock."""

    def __init__(self) -> None:
        """Start at a fixed epoch."""
        self.now = 1000

    def __call__(self) -> int:
        """Return the current fake time."""
        return self.now


def wait_until_refused(url: str, timeout: float = 10.0) -> None:
    """Block until nothing accepts connections at ``url``, so an outage test does not race the shutdown."""
    parts = urlsplit(url)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            socket.create_connection((parts.hostname or "127.0.0.1", parts.port or 80), 0.2).close()
        except OSError:
            return
        time.sleep(0.02)
    raise AssertionError(f"{url} still accepts connections after {timeout} seconds")
