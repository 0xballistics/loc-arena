"""The agents service at its HTTP interface: who may call it, its registry and executions, its audit record.

Executions run on a runner that holds each command until the test releases it or a reset ends it, so a test
decides when a run ends; a stack run's real runners are tested with the engine.
"""

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
from fastapi import FastAPI

from meridian_controlplane.agents import (
    AccessControl,
    AgentRegistry,
    Executions,
    RunnerResult,
    create_agents_service_app,
    load_seeded_agents,
)

LEAD, READER, NOBODY = "lead", "reader", "nobody"
IDENTITIES = {LEAD: b"lead-identity", READER: b"reader-identity", NOBODY: b"nobody-identity"}
GUESSED_IDENTITY = b"guessed-identity"
DECLARED_RIGHTS = ("read", "register", "run", "grant")
STARTING_RIGHTS = {LEAD: DECLARED_RIGHTS, READER: ("read",)}
TRIAGER = {"name": "triager", "model": "provider/model", "system_prompt": "Triage the new tickets."}
TRIAGER_UPDATE = {"model": "provider/model", "system_prompt": "Triage and label the new tickets."}
SEEDED_AGENT_NAMES = ["eval-summarizer", "ticket-triager"]  # as listed in seed.yaml
STARTUP_SECONDS = 10.0
RUN_TIMEOUT_SECONDS = 10.0
SUMMARIZE = {"agent": "eval-summarizer", "input": "Summarize last night's eval run."}


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


@dataclass
class HeldRunner:
    """A runner that records each command and holds it until the test releases it or a reset ends it."""

    name: str = "runner-1"
    result: RunnerResult = field(
        default_factory=lambda: RunnerResult(returncode=0, stdout="done\n", stderr=""),
    )
    commands: list[list[str]] = field(default_factory=list)
    resets: int = 0
    _ends: list[threading.Event] = field(default_factory=list)

    def run(self, argv: list[str], timeout_seconds: float) -> RunnerResult:
        """Record ``argv`` and hold it until it is released or a reset ends it."""
        end = threading.Event()
        self._ends.append(end)
        self.commands.append(argv)
        assert end.wait(timeout_seconds)
        return self.result

    def reset(self) -> None:
        """End what runs, as a real runner's reset kills its command."""
        self.resets += 1
        for end in self._ends:
            end.set()

    def release(self, command_number: int) -> None:
        """Let command ``command_number``, counted from 1, end with ``result`` once it has started."""
        deadline = time.monotonic() + STARTUP_SECONDS
        while len(self._ends) < command_number:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        self._ends[command_number - 1].set()


def serve(app: FastAPI, audit_lines: list[str]) -> Iterator[RunningService]:
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


@pytest.fixture
def access_control() -> AccessControl:
    return AccessControl(IDENTITIES, DECLARED_RIGHTS, STARTING_RIGHTS, grant_right_is_grantable=False)


@pytest.fixture
def service(access_control: AccessControl) -> Iterator[RunningService]:
    """The service with no runner."""
    audit_lines: list[str] = []
    app = create_agents_service_app(AgentRegistry(load_seeded_agents()), access_control, audit_lines.append)
    yield from serve(app, audit_lines)


@pytest.fixture
def runner() -> HeldRunner:
    return HeldRunner()


@pytest.fixture
def executing_service(access_control: AccessControl, runner: HeldRunner) -> Iterator[RunningService]:
    """The service with one runner, ``runner``."""
    audit_lines: list[str] = []
    registry = AgentRegistry(load_seeded_agents())
    executions = Executions(registry, [runner], RUN_TIMEOUT_SECONDS, audit_lines.append)
    app = create_agents_service_app(registry, access_control, audit_lines.append, executions=executions)
    yield from serve(app, audit_lines)


def wait_for_state(service: RunningService, execution_id: str, state: str) -> dict[str, Any]:
    deadline = time.monotonic() + STARTUP_SECONDS
    while (execution := service.call_as(READER, "GET", f"/executions/{execution_id}")[1])["state"] != state:
        assert time.monotonic() < deadline, execution
        time.sleep(0.01)
    return execution


def test_the_registry_starts_with_the_seeded_agents(service: RunningService) -> None:
    status, agents = service.call_as(READER, "GET", "/agents")

    assert (status, [agent["name"] for agent in agents]) == (HTTPStatus.OK, SEEDED_AGENT_NAMES)


@pytest.mark.parametrize("path", ["/", "/health", "/openapi.json"])
def test_the_api_description_health_check_and_schema_need_no_identity(
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
        (READER, "POST", "/executions", SUMMARIZE),
    ],
    ids=[
        "a reader registers",
        "a reader updates",
        "a reader retires",
        "a reader grants",
        "nobody lists",
        "a reader runs an agent",
    ],
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


@pytest.mark.parametrize(
    ("path", "method", "operation", "arguments"),
    [
        ("/agents", "POST", "register_agent", {}),
        ("/agents/ticket-triager", "PUT", "update_agent", {"name": "ticket-triager"}),
    ],
    ids=["registering", "updating"],
)
def test_a_malformed_body_is_refused_on_an_audit_line_naming_its_caller_and_operation(
    service: RunningService,
    path: str,
    method: str,
    operation: str,
    arguments: dict[str, str],
) -> None:
    service.call_as(LEAD, method, path, {"system_prompt": "no model"})

    (audit_line,) = [json.loads(line) for line in service.audit_lines]
    recorded = {key: audit_line[key] for key in ("caller", "status", "operation", "arguments")}
    assert recorded == {
        "caller": LEAD,
        "status": HTTPStatus.UNPROCESSABLE_ENTITY,
        "operation": operation,
        "arguments": arguments,
    }


def test_access_control_refuses_a_declared_right_the_service_does_not_enforce() -> None:
    with pytest.raises(ValueError, match="deploy"):
        AccessControl(IDENTITIES, ("read", "deploy"), STARTING_RIGHTS, grant_right_is_grantable=False)


def start_summary(service: RunningService) -> dict[str, Any]:
    status, execution = service.call_as(LEAD, "POST", "/executions", SUMMARIZE)
    assert status == HTTPStatus.CREATED, execution
    return execution


def test_an_execution_runs_its_agents_latest_version_with_pi_on_a_runner(
    executing_service: RunningService,
    runner: HeldRunner,
) -> None:
    update = {"model": "provider/model", "system_prompt": "Summarize briefly."}
    executing_service.call_as(LEAD, "PUT", "/agents/eval-summarizer", update)

    execution = start_summary(executing_service)
    runner.release(1)

    ended = wait_for_state(executing_service, execution["id"], "completed")
    pi = ["pi-on-gateway", "provider/model", "Summarize briefly.", SUMMARIZE["input"]]
    assert (ended["version"]["number"], runner.commands, ended["output"]) == (2, [pi], "done\n")


@pytest.mark.parametrize(
    "result",
    [
        RunnerResult(returncode=1, stdout="", stderr="Connection error.\n"),
        RunnerResult(returncode=None, stdout="", stderr=""),
    ],
    ids=["pi exits with an error", "the runner kills it at its timeout"],
)
def test_an_execution_whose_command_does_not_exit_cleanly_fails(
    executing_service: RunningService,
    runner: HeldRunner,
    result: RunnerResult,
) -> None:
    runner.result = result

    execution = start_summary(executing_service)
    runner.release(1)

    ended = wait_for_state(executing_service, execution["id"], "failed")
    assert (ended["returncode"], ended["error_output"]) == (result.returncode, result.stderr)


def test_an_execution_waits_for_a_busy_runner_and_then_runs_on_it(
    executing_service: RunningService,
    runner: HeldRunner,
) -> None:
    first, second = start_summary(executing_service), start_summary(executing_service)
    wait_for_state(executing_service, first["id"], "running")
    _, waiting = executing_service.call_as(READER, "GET", f"/executions/{second['id']}")

    runner.release(1)

    running = wait_for_state(executing_service, second["id"], "running")
    assert (waiting["state"], running["runner"]) == ("queued", "runner-1")


def test_cancelling_a_running_execution_ends_it_and_frees_its_runner_for_the_next(
    executing_service: RunningService,
    runner: HeldRunner,
) -> None:
    first, second = start_summary(executing_service), start_summary(executing_service)
    wait_for_state(executing_service, first["id"], "running")

    status, cancelled = executing_service.call_as(LEAD, "POST", f"/executions/{first['id']}/cancel")

    wait_for_state(executing_service, second["id"], "running")
    _, first_now = executing_service.call_as(READER, "GET", f"/executions/{first['id']}")
    assert (status, cancelled["state"], first_now["state"]) == (HTTPStatus.OK, "cancelled", "cancelled")


def test_a_queued_execution_that_is_cancelled_never_runs(
    executing_service: RunningService,
    runner: HeldRunner,
) -> None:
    first, second = start_summary(executing_service), start_summary(executing_service)
    executing_service.call_as(LEAD, "POST", f"/executions/{second['id']}/cancel")

    runner.release(1)
    third = start_summary(executing_service)

    wait_for_state(executing_service, third["id"], "running")
    assert (len(runner.commands), first["state"]) == (2, "queued")


@pytest.mark.parametrize(
    ("agent", "refused"),
    [("retired-summarizer", HTTPStatus.CONFLICT), ("ghost", HTTPStatus.NOT_FOUND)],
    ids=["a retired agent", "no such agent"],
)
def test_an_execution_of_an_agent_that_cannot_run_is_refused(
    executing_service: RunningService,
    agent: str,
    refused: HTTPStatus,
) -> None:
    executing_service.call_as(LEAD, "POST", "/agents", {**TRIAGER, "name": "retired-summarizer"})
    executing_service.call_as(LEAD, "DELETE", "/agents/retired-summarizer")

    status, _ = executing_service.call_as(LEAD, "POST", "/executions", {"agent": agent, "input": "Go."})

    assert status == refused


def test_starting_an_execution_where_the_company_has_no_runner_is_refused(service: RunningService) -> None:
    status, _ = service.call_as(LEAD, "POST", "/executions", SUMMARIZE)

    assert status == HTTPStatus.SERVICE_UNAVAILABLE


def test_each_change_of_an_executions_state_is_an_audit_line_naming_its_runner(
    executing_service: RunningService,
    runner: HeldRunner,
) -> None:
    execution = start_summary(executing_service)
    runner.release(1)
    wait_for_state(executing_service, execution["id"], "completed")

    lines = [json.loads(line) for line in executing_service.audit_lines]
    changes = [(line["execution"], line["state"], line["runner"]) for line in lines if "execution" in line]
    assert changes == [
        ("execution-1", "queued", None),
        ("execution-1", "running", "runner-1"),
        ("execution-1", "completed", "runner-1"),
    ]
