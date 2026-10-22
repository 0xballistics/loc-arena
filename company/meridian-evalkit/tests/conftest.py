"""The eval service's fixtures: a registry from the seed, and the service running on a local port."""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator

import pytest
import uvicorn
from service_support import IDENTITIES, NOW, RIGHTS, STARTUP_SECONDS, RunningService, StandInAuthority

from meridian_evalkit.service import EvalkitRegistry, create_evalkit_api, load_seed


@pytest.fixture
def registry() -> EvalkitRegistry:
    return EvalkitRegistry(load_seed(), clock=lambda: NOW)


@pytest.fixture
def service(registry: EvalkitRegistry) -> Iterator[RunningService]:
    audit_lines: list[str] = []
    app = create_evalkit_api(registry, audit_lines.append, StandInAuthority(IDENTITIES, RIGHTS))
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
    listener = socket.create_server(("127.0.0.1", 0))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + STARTUP_SECONDS
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.started
    yield RunningService(f"http://127.0.0.1:{listener.getsockname()[1]}", audit_lines)
    server.should_exit = True
    thread.join()
