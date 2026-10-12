"""The agents service at its HTTP interface: who may call it, the registry's versions, its audit record."""

from __future__ import annotations

import json
import socket
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass, field
from http import HTTPStatus
from typing import Any

import pytest
import uvicorn

from meridian_controlplane.agents import (
    AccessControl,
    AgentRegistry,
    create_agents_service_app,
    load_seeded_agents,
)

LEAD, READER, NOBODY = "lead", "reader", "nobody"
IDENTITIES = {LEAD: b"lead-identity", READER: b"reader-identity", NOBODY: b"nobody-identity"}
GUESSED_IDENTITY = b"guessed-identity"
DECLARED_RIGHTS = ("read", "register", "grant")
STARTING_RIGHTS = {LEAD: DECLARED_RIGHTS, READER: ("read",)}
TRIAGER = {"name": "triager", "model": "provider/model", "system_prompt": "Triage the new tickets."}
TRIAGER_UPDATE = {"model": "provider/model", "system_prompt": "Triage and label the new tickets."}
SEEDED_AGENT_NAMES = ["eval-summarizer", "ticket-triager"]  # as listed in seed.yaml
STARTUP_SECONDS = 10.0


@dataclass
class RunningService:
    """The service running on a local port, and the lines of its audit record so far."""

    url: str
    audit_lines: list[str] = field(default_factory=list)

    def call(self, method: str, path: str, identity: bytes | None, body: object = None) -> tuple[int, Any]:
        """The status and JSON reply of ``method`` on ``path``, with ``identity`` sent as a bearer token."""
        headers = {"content-type": "application/json"}
        if identity is not None:
            headers["authorization"] = f"Bearer {identity.decode()}"
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(f"{self.url}{path}", data, headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=STARTUP_SECONDS) as response:
                reply = response.read()
                is_json = response.headers.get_content_type() == "application/json"
                return response.status, json.loads(reply) if is_json else reply.decode()
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def call_as(self, caller: str, method: str, path: str, body: object = None) -> tuple[int, Any]:
        """``call`` with the identity of ``caller``."""
        return self.call(method, path, IDENTITIES[caller], body)


@pytest.fixture
def service() -> Iterator[RunningService]:
    audit_lines: list[str] = []
    access_control = AccessControl(
        IDENTITIES,
        DECLARED_RIGHTS,
        STARTING_RIGHTS,
        grant_right_is_grantable=False,
    )
    app = create_agents_service_app(AgentRegistry(load_seeded_agents()), access_control, audit_lines.append)
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


def test_the_registry_starts_with_the_seeded_agents(service: RunningService) -> None:
    status, agents = service.call_as(READER, "GET", "/agents")

    assert (status, [agent["name"] for agent in agents]) == (HTTPStatus.OK, SEEDED_AGENT_NAMES)


@pytest.mark.parametrize("path", ["/", "/health"])
def test_the_api_description_and_the_health_check_need_no_identity(
    service: RunningService,
    path: str,
) -> None:
    status, _ = service.call("GET", path, identity=None)

    assert status == HTTPStatus.OK


@pytest.mark.parametrize("identity", [None, GUESSED_IDENTITY], ids=["no identity", "a guessed identity"])
def test_a_request_without_a_known_identity_is_refused(
    service: RunningService,
    identity: bytes | None,
) -> None:
    status, _ = service.call("POST", "/agents", identity, TRIAGER)

    assert status == HTTPStatus.UNAUTHORIZED


@pytest.mark.parametrize(
    ("caller", "method", "path", "body"),
    [
        (READER, "POST", "/agents", TRIAGER),
        (READER, "PUT", "/agents/ticket-triager", TRIAGER_UPDATE),
        (READER, "DELETE", "/agents/ticket-triager", None),
        (READER, "POST", "/grants", {"agent": NOBODY, "rights": ["read"]}),
        (NOBODY, "GET", "/agents", None),
    ],
    ids=["a reader registers", "a reader updates", "a reader retires", "a reader grants", "nobody lists"],
)
def test_a_caller_without_the_right_is_refused(
    service: RunningService,
    caller: str,
    method: str,
    path: str,
    body: object,
) -> None:
    status, _ = service.call_as(caller, method, path, body)

    assert status == HTTPStatus.FORBIDDEN


def test_a_registered_agent_is_owned_by_its_registrant_at_version_1(service: RunningService) -> None:
    status, agent = service.call_as(LEAD, "POST", "/agents", TRIAGER)

    version = agent["latest_version"]
    assert (status, agent["owner"], version["number"], version["created_by"]) == (
        HTTPStatus.CREATED,
        LEAD,
        1,
        LEAD,
    )


def test_an_update_makes_the_next_version_and_keeps_the_earlier_one(service: RunningService) -> None:
    service.call_as(LEAD, "POST", "/agents", TRIAGER)
    service.call_as(LEAD, "PUT", "/agents/triager", TRIAGER_UPDATE)

    _, versions = service.call_as(READER, "GET", "/agents/triager/versions")

    assert [(version["number"], version["system_prompt"]) for version in versions] == [
        (1, "Triage the new tickets."),
        (2, "Triage and label the new tickets."),
    ]


def test_a_retired_agent_is_listed_as_retired(service: RunningService) -> None:
    service.call_as(LEAD, "DELETE", "/agents/ticket-triager")

    _, agents = service.call_as(READER, "GET", "/agents")

    assert {agent["name"]: agent["retired"] for agent in agents} == {
        "eval-summarizer": False,
        "ticket-triager": True,
    }


@pytest.mark.parametrize(
    ("method", "path", "body", "expected"),
    [
        ("POST", "/agents", {**TRIAGER, "name": "ticket-triager"}, HTTPStatus.CONFLICT),
        ("POST", "/agents", {**TRIAGER, "name": "Not A Name"}, HTTPStatus.UNPROCESSABLE_ENTITY),
        ("GET", "/agents/ghost", None, HTTPStatus.NOT_FOUND),
        ("PUT", "/agents/ghost", TRIAGER_UPDATE, HTTPStatus.NOT_FOUND),
        ("PUT", "/agents/retired-triager", TRIAGER_UPDATE, HTTPStatus.CONFLICT),
        ("DELETE", "/agents/retired-triager", None, HTTPStatus.CONFLICT),
    ],
    ids=[
        "a taken name",
        "an invalid name",
        "reading no agent",
        "updating no agent",
        "updating a retired agent",
        "retiring a retired agent",
    ],
)
def test_a_change_the_registry_cannot_make_is_refused_with_its_reason(
    service: RunningService,
    method: str,
    path: str,
    body: object,
    expected: HTTPStatus,
) -> None:
    service.call_as(LEAD, "POST", "/agents", {**TRIAGER, "name": "retired-triager"})
    service.call_as(LEAD, "DELETE", "/agents/retired-triager")

    status, _ = service.call_as(LEAD, method, path, body)

    assert status == expected


def test_a_granted_right_lets_its_holder_use_it(service: RunningService) -> None:
    service.call_as(LEAD, "POST", "/grants", {"agent": READER, "rights": ["register"]})

    status, _ = service.call_as(READER, "POST", "/agents", TRIAGER)

    assert status == HTTPStatus.CREATED


def test_a_revoked_right_no_longer_lets_its_holder_use_it(service: RunningService) -> None:
    service.call_as(LEAD, "DELETE", "/grants", {"agent": READER, "rights": ["read"]})

    status, _ = service.call_as(READER, "GET", "/agents")

    assert status == HTTPStatus.FORBIDDEN


@pytest.mark.parametrize(
    ("granter", "change", "expected"),
    [
        (READER, {"agent": NOBODY, "rights": ["read"]}, HTTPStatus.FORBIDDEN),
        (LEAD, {"agent": READER, "rights": ["grant"]}, HTTPStatus.FORBIDDEN),
        (LEAD, {"agent": "ghost", "rights": ["read"]}, HTTPStatus.NOT_FOUND),
        (LEAD, {"agent": READER, "rights": ["deploy"]}, HTTPStatus.BAD_REQUEST),
    ],
    ids=["without the grant right", "the grant right, not grantable", "to no caller", "an undeclared right"],
)
def test_a_grant_the_service_refuses_changes_no_right(
    service: RunningService,
    granter: str,
    change: dict[str, object],
    expected: HTTPStatus,
) -> None:
    status, _ = service.call_as(granter, "POST", "/grants", change)

    _, rights = service.call_as(LEAD, "GET", "/grants")
    assert (status, rights) == (expected, {LEAD: sorted(DECLARED_RIGHTS), NOBODY: [], READER: ["read"]})


@pytest.mark.parametrize(
    ("identity", "caller", "status"),
    [
        (IDENTITIES[LEAD], LEAD, HTTPStatus.CREATED),
        (IDENTITIES[READER], READER, HTTPStatus.FORBIDDEN),
        (GUESSED_IDENTITY, None, HTTPStatus.UNAUTHORIZED),
    ],
    ids=["registered", "refused", "unknown identity"],
)
def test_each_request_is_one_audit_line_naming_its_caller_operation_and_arguments(
    service: RunningService,
    identity: bytes,
    caller: str | None,
    status: HTTPStatus,
) -> None:
    service.call("POST", "/agents", identity, TRIAGER)

    (audit_line,) = [json.loads(line) for line in service.audit_lines]
    recorded = {
        key: audit_line[key] for key in ("caller", "method", "path", "status", "operation", "arguments")
    }
    assert recorded == {
        "caller": caller,
        "method": "POST",
        "path": "/agents",
        "status": status,
        "operation": "register_agent",
        "arguments": TRIAGER,
    }


def test_access_control_refuses_a_declared_right_the_service_does_not_enforce() -> None:
    with pytest.raises(ValueError, match="deploy"):
        AccessControl(IDENTITIES, ("read", "deploy"), STARTING_RIGHTS, grant_right_is_grantable=False)
