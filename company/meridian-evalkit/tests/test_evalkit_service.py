"""The eval service at its HTTP interface and its MCP tools: its rules, a job's binding, seed and audit.

A stand-in for the central authority decides each call by the service's own manifest, as a deployment's does;
the engine tests the service under the real one.
"""

from __future__ import annotations

import hmac
import json
import socket
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http import HTTPStatus
from pathlib import Path
from typing import Any

import anyio
import pytest
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from mcp import Client
from mcp.types import CallToolResult, TextContent

from meridian_evalkit import service as service_package
from meridian_evalkit.service import EvalkitRegistry, build_evalkit_server, create_evalkit_api, load_seed

PRIYA, TOMAS, READER, JOB = "priya", "tomas", "reader", "job-7"
IDENTITIES = {
    PRIYA: b"priya-identity",
    TOMAS: b"tomas-identity",
    READER: b"reader-identity",
    JOB: b"job-7-identity",
}
RIGHTS = {PRIYA: ("read", "run", "curate", "publish"), TOMAS: ("read", "run"), READER: ("read",), JOB: ()}
GUESSED_IDENTITY = "guessed-identity"
STARTUP_SECONDS = 10.0
NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
MANIFEST = json.loads((Path(service_package.__file__).parent / "iam-manifest.json").read_text())
ARITHMETIC_RUN = {"eval_id": "arithmetic", "model_id": "aurora-7b-v3"}
RESULTS = {"metrics": {"accuracy": 0.95, "brier": 0.05}, "item_ids": ["ar-001", "ar-002"]}
SPELLING_ITEM = {
    "item_id": "sp-001",
    "prompt": "Spell 'necessary'.",
    "reference": "necessary",
    "category": "spelling",
}
SPELLING = {"eval_id": "spelling", "items": [SPELLING_ITEM], "metrics": ["accuracy"]}
# What the seed holds, read off seed.yaml: its last run, its results versions, and its boards' order.
LAST_SEEDED_RUN, SEEDED_VERSIONS = "run-0438", 5
SEEDED_BOARDS = {"arithmetic": ["run-0423", "run-0402"], "general-knowledge": ["run-0417", "run-0391"]}


class StandInAuthority:
    """Each caller's identity and rights, checked against the service's manifest before any handler runs."""

    def __init__(self, identities: dict[str, bytes], rights: dict[str, tuple[str, ...]]) -> None:
        """Admit ``identities``, each holding its ``rights``."""
        self._identities = identities
        self._rights = {caller: set(held) for caller, held in rights.items()}
        self.dependency: Callable[[Request], Awaitable[None]] = self._decide  # the protocol's attribute

    async def _decide(self, request: Request) -> None:
        """Name the operation and the caller, as a central authority does, and refuse what is not allowed."""
        operation = MANIFEST["http"][f"{request.method} {request.scope['route'].path}"]
        request.state.iam_operation = operation
        declared = MANIFEST["operations"][operation]
        if declared.get("public"):
            return
        sent = request.headers.get("Authorization", "").removeprefix("Bearer ").encode()
        known = self._identities.items()
        caller = next((name for name, identity in known if hmac.compare_digest(sent, identity)), None)
        if caller is None:
            raise HTTPException(HTTPStatus.UNAUTHORIZED, "send a known credential")
        request.state.iam_principal = caller
        permission = declared.get("permission")
        if permission is not None and permission not in self._rights.get(caller, ()):
            raise HTTPException(HTTPStatus.FORBIDDEN, f"needs {permission!r}")

    def verify_routes(self, app: FastAPI) -> None:
        """The deployment's authority checks the routes against the manifest; the engine tests that."""


@dataclass
class RunningService:
    """The service running on a local port, and the lines of its audit record so far."""

    url: str
    audit_lines: list[str] = field(default_factory=list)

    def call(self, method: str, path: str, identity: str | None, body: object = None) -> tuple[int, Any]:
        """The status and reply of ``method`` on ``path``, with ``identity`` sent as a bearer token."""
        headers = {"content-type": "application/json"}
        if identity is not None:
            headers["authorization"] = f"Bearer {identity}"
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
        return self.call(method, path, IDENTITIES[caller].decode(), body)

    def start_run(self, caller: str = PRIYA) -> str:
        """The id of the run ``caller`` starts on the arithmetic eval."""
        status, run = self.call_as(caller, "POST", "/runs", ARITHMETIC_RUN)
        assert status == HTTPStatus.CREATED, run
        return run["run_id"]

    def report(self, run_id: str, caller: str = JOB) -> tuple[int, Any]:
        """Bind ``caller`` to the run and report ``RESULTS`` as it."""
        assert self.call_as(caller, "POST", f"/runs/{run_id}/start")[0] == HTTPStatus.OK
        return self.call_as(caller, "POST", f"/runs/{run_id}/results", RESULTS)


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


@pytest.mark.parametrize("path", ["/", "/health", "/openapi.json"])
def test_the_description_health_check_and_schema_need_no_identity(service: RunningService, path: str) -> None:
    status, _ = service.call("GET", path, identity=None)

    assert status == HTTPStatus.OK


@pytest.mark.parametrize("identity", [None, GUESSED_IDENTITY], ids=["no identity", "a guessed identity"])
def test_a_request_without_a_known_identity_is_refused(service: RunningService, identity: str | None) -> None:
    status, _ = service.call("GET", "/evals", identity)

    assert status == HTTPStatus.UNAUTHORIZED


@pytest.mark.parametrize(
    ("caller", "method", "path", "body", "permission"),
    [
        (READER, "POST", "/runs", ARITHMETIC_RUN, "run"),
        (TOMAS, "POST", "/evals", SPELLING, "curate"),
        (TOMAS, "DELETE", "/evals/arithmetic", None, "curate"),
        (TOMAS, "POST", "/runs/run-0438/publish", None, "publish"),
    ],
)
def test_each_route_is_bound_to_the_permission_the_manifest_names(  # noqa: PLR0913 - one refusal per row
    service: RunningService,
    caller: str,
    method: str,
    path: str,
    body: object,
    permission: str,
) -> None:
    status, reply = service.call_as(caller, method, path, body)

    assert (status, reply["detail"]) == (HTTPStatus.FORBIDDEN, f"needs {permission!r}")


def test_the_service_starts_with_the_seeds_evals_runs_and_leaderboards(service: RunningService) -> None:
    _, evals = service.call_as(READER, "GET", "/evals")
    _, runs = service.call_as(READER, "GET", "/runs")
    _, boards = service.call_as(READER, "GET", "/leaderboard")

    assert [(found["eval_id"], found["item_count"]) for found in evals["evals"]] == [
        ("arithmetic", 10),
        ("general-knowledge", 12),
    ]
    assert [run["state"] for run in runs["runs"]] == ["reported"] * SEEDED_VERSIONS
    assert {board["eval_id"]: [row["run_id"] for row in board["ranking"]] for board in boards["boards"]} == (
        SEEDED_BOARDS
    )


def test_starting_a_run_records_it_and_hands_out_the_job_to_submit(service: RunningService) -> None:
    status, run = service.call_as(PRIYA, "POST", "/runs", ARITHMETIC_RUN)

    assert (status, run["run_id"], run["owner"], run["state"]) == (
        HTTPStatus.CREATED,
        "run-0439",
        PRIYA,
        "created",
    )
    assert run["job"] == {
        "name": "eval arithmetic aurora-7b-v3",
        "command": [
            "python",
            "-m",
            "meridian_evalkit.run",
            "--evalkit",
            "http://evalkit:8000",
            "--run",
            "run-0439",
        ],
        "cpu": 1.0,
        "memory_mb": 2048,
        "priority": 0,
        "depends_on": [],
        "labels": {"run_id": "run-0439", "eval_id": "arithmetic", "model_id": "aurora-7b-v3"},
    }
    assert service.call_as(READER, "GET", "/runs/run-0439")[1]["created_at"] == "2026-09-01T12:00:00Z"


@pytest.mark.parametrize(
    ("start", "status"),
    [
        ({"eval_id": "arithmetic", "model_id": "aurora-70b"}, HTTPStatus.UNPROCESSABLE_ENTITY),
        ({"eval_id": "spelling", "model_id": "aurora-7b-v3"}, HTTPStatus.NOT_FOUND),
    ],
    ids=["an unknown model", "an unknown eval"],
)
def test_a_run_on_an_unknown_model_or_eval_is_refused(
    service: RunningService,
    start: dict[str, str],
    status: HTTPStatus,
) -> None:
    assert service.call_as(PRIYA, "POST", "/runs", start)[0] == status


def test_a_retired_eval_takes_no_new_run_and_keeps_its_runs(service: RunningService) -> None:
    status, retired = service.call_as(PRIYA, "DELETE", "/evals/arithmetic")

    assert (status, retired["retired_at"]) == (HTTPStatus.OK, "2026-09-01T12:00:00Z")
    assert service.call_as(PRIYA, "POST", "/runs", ARITHMETIC_RUN)[0] == HTTPStatus.CONFLICT
    assert service.call_as(PRIYA, "DELETE", "/evals/arithmetic")[0] == HTTPStatus.CONFLICT
    assert service.call_as(READER, "GET", "/runs/run-0402")[1]["state"] == "reported"


def test_only_the_job_bound_to_a_run_reports_it_and_only_once(service: RunningService) -> None:
    run_id = service.start_run()

    status, started = service.call_as(JOB, "POST", f"/runs/{run_id}/start")
    owner_reports = service.call_as(PRIYA, "POST", f"/runs/{run_id}/results", RESULTS)[0]
    second_job_starts = service.call_as(TOMAS, "POST", f"/runs/{run_id}/start")[0]
    reported_status, reported = service.call_as(JOB, "POST", f"/runs/{run_id}/results", RESULTS)
    reports_again = service.call_as(JOB, "POST", f"/runs/{run_id}/results", RESULTS)[0]

    assert (status, started["state"], started["job_principal"]) == (HTTPStatus.OK, "started", JOB)
    assert (owner_reports, second_job_starts) == (HTTPStatus.FORBIDDEN, HTTPStatus.CONFLICT)
    assert (reported_status, reported["state"], reported["results_version"], reported["metrics"]) == (
        HTTPStatus.CREATED,
        "reported",
        SEEDED_VERSIONS + 1,
        RESULTS["metrics"],
    )
    assert reports_again == HTTPStatus.CONFLICT


@pytest.mark.parametrize(
    "results",
    [
        {"metrics": {"accuracy": 0.9}, "item_ids": ["ar-001"]},
        {"metrics": {"accuracy": 0.9, "brier": 0.1}, "item_ids": ["ar-001", "gk-001"]},
    ],
    ids=["a missing metric", "an item of another eval"],
)
def test_results_must_cover_the_evals_metrics_and_score_its_items(
    service: RunningService,
    results: dict[str, Any],
) -> None:
    run_id = service.start_run()
    service.call_as(JOB, "POST", f"/runs/{run_id}/start")

    status, _ = service.call_as(JOB, "POST", f"/runs/{run_id}/results", results)

    assert status == HTTPStatus.UNPROCESSABLE_ENTITY


def test_publishing_ranks_a_reported_run_by_the_headline_metric_once(service: RunningService) -> None:
    run_id = service.start_run()
    unreported = service.call_as(PRIYA, "POST", f"/runs/{run_id}/publish")[0]
    service.report(run_id)

    status, published = service.call_as(PRIYA, "POST", f"/runs/{run_id}/publish")
    _, boards = service.call_as(READER, "GET", "/leaderboard?eval_id=arithmetic")

    assert (unreported, status, published["published_at"]) == (
        HTTPStatus.CONFLICT,
        HTTPStatus.OK,
        "2026-09-01T12:00:00Z",
    )
    assert [(row["rank"], row["run_id"], row["score"]) for row in boards["boards"][0]["ranking"]] == [
        (1, "run-0423", 1.0),
        (2, run_id, 0.95),
        (3, "run-0402", 0.9),
    ]
    assert service.call_as(PRIYA, "POST", f"/runs/{run_id}/publish")[0] == HTTPStatus.CONFLICT


def test_a_diff_gives_the_metric_deltas_between_two_reported_runs(service: RunningService) -> None:
    status, diff = service.call_as(READER, "GET", "/runs/run-0402/diff/run-0423")

    assert status == HTTPStatus.OK
    assert {change["name"]: change["delta"] for change in diff["metric_deltas"]} == {
        "accuracy": pytest.approx(0.1),
        "brier": pytest.approx(-0.0628),
    }
    assert (diff["added_metrics"], diff["removed_metrics"], diff["added_items"]) == ([], [], [])
    assert service.call_as(READER, "GET", f"/runs/run-0402/diff/{LAST_SEEDED_RUN}")[0] == HTTPStatus.OK


def test_registering_an_eval_needs_metrics_the_harness_computes_and_a_new_id(service: RunningService) -> None:
    unknown_metric = service.call_as(PRIYA, "POST", "/evals", {**SPELLING, "metrics": ["bleu"]})[0]
    status, registered = service.call_as(PRIYA, "POST", "/evals", SPELLING)
    again = service.call_as(PRIYA, "POST", "/evals", SPELLING)[0]

    assert (unknown_metric, status, again) == (
        HTTPStatus.UNPROCESSABLE_ENTITY,
        HTTPStatus.CREATED,
        HTTPStatus.CONFLICT,
    )
    assert (registered["registered_by"], registered["items"]) == (PRIYA, [SPELLING_ITEM])
    started = service.call_as(TOMAS, "POST", "/runs", {"eval_id": "spelling", "model_id": "aurora-13b-v2"})
    assert started[0] == HTTPStatus.CREATED


def test_each_request_is_an_audit_line_naming_its_caller_and_operation(service: RunningService) -> None:
    service.call_as(READER, "POST", "/runs", ARITHMETIC_RUN)  # refused: no permission
    service.call_as(PRIYA, "POST", "/runs", ARITHMETIC_RUN)
    service.call("GET", "/evals", GUESSED_IDENTITY)

    lines = [json.loads(line) for line in service.audit_lines]

    assert [(line["caller"], line.get("operation"), line["status"]) for line in lines] == [
        (READER, "start_run", HTTPStatus.FORBIDDEN),
        (PRIYA, "start_run", HTTPStatus.CREATED),
        (None, "list_evals", HTTPStatus.UNAUTHORIZED),
    ]
    assert lines[1]["arguments"] == ARITHMETIC_RUN
    assert not any(
        identity.decode() in line for identity in IDENTITIES.values() for line in service.audit_lines
    )


def call_tool(
    registry: EvalkitRegistry,
    caller: str | None,
    tool: str,
    arguments: dict[str, Any],
) -> CallToolResult:
    """The result of ``caller`` calling ``tool`` in memory; a deployment's middleware decides calls there."""
    server = build_evalkit_server(registry, lambda: caller)

    async def call() -> CallToolResult:
        async with Client(server) as client:
            return await client.call_tool(tool, arguments)

    return anyio.run(call)


def test_a_tool_runs_its_operation_as_its_caller(registry: EvalkitRegistry) -> None:
    started = call_tool(registry, TOMAS, "start_run", ARITHMETIC_RUN)

    assert not started.is_error
    assert (started.structured_content or {})["owner"] == TOMAS
    assert registry.get_run("run-0439").owner == TOMAS


@pytest.mark.parametrize(
    ("caller", "tool", "arguments", "why"),
    [
        (None, "list_evals", {}, "no identity"),
        (PRIYA, "get_run", {"run_id": "run-9999"}, "no run 'run-9999'"),
        (PRIYA, "start_run", {"eval_id": "arithmetic", "model_id": "aurora-70b"}, "no model 'aurora-70b'"),
    ],
    ids=["no identity", "an unknown run", "an unknown model"],
)
def test_a_refused_tool_call_is_a_tool_error_naming_why(
    registry: EvalkitRegistry,
    caller: str | None,
    tool: str,
    arguments: dict[str, Any],
    why: str,
) -> None:
    refused = call_tool(registry, caller, tool, arguments)

    text = " ".join(block.text for block in refused.content if isinstance(block, TextContent))
    assert refused.is_error
    assert why in text
    assert len(registry.list_runs()) == SEEDED_VERSIONS


def test_the_tools_are_the_manifests_tools_by_name(registry: EvalkitRegistry) -> None:
    server = build_evalkit_server(registry, lambda: PRIYA)

    tools = {tool.name for tool in anyio.run(server.list_tools)}

    assert tools == set(MANIFEST["tools"])
