"""A stand-in for an IAM that cannot answer: it stalls, then answers every request 503, and counts them."""

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


@contextmanager
def failing_iam(stall_seconds: float = 0.0) -> Iterator[tuple[str, list[str]]]:
    """Serve one at the yielded URL; the list holds the path of each request it was sent."""
    asked: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - http.server API
            asked.append(self.path)
            time.sleep(stall_seconds)
            self.send_error(HTTPStatus.SERVICE_UNAVAILABLE)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002, ANN401 - http.server API
            """Log nothing."""

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", asked
    finally:
        server.shutdown()
        server.server_close()
