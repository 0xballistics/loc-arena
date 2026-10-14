"""Waiting for a stopped IAM server to really be gone, in place of a fixed sleep."""

from __future__ import annotations

import socket
import time
from urllib.parse import urlsplit


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
