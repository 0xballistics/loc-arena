"""A real IAM HTTP server (uvicorn on a free port, real SQLite) for the integration tests."""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

import uvicorn

from meridian_iam.core import Iam
from meridian_iam.service import create_app


@contextmanager
def serve(iam: Iam) -> Iterator[tuple[str, uvicorn.Server]]:
    """Serve ``iam`` until the block ends (or ``server.should_exit`` is set); yields (base_url, server)."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(iam), port=port, log_level="error", access_log=False))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.01)
    try:
        yield f"http://127.0.0.1:{port}", server
    finally:
        server.should_exit = True
        thread.join(timeout=5)
