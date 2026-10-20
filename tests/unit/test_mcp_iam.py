"""The board and recording MCP services under central IAM: a real IAM server in front of the real tools."""

import json
import socket
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from typing import Any

import anyio
import httpx2
import pytest
import uvicorn
from loc_arena.board import SERVICE, Board, build_server
from loc_arena.config import load_run_config
from loc_arena.iam_wiring import guard_from_environment, iam_options
from loc_arena.mcp_service import build_app, caller_from_token
from loc_arena.recording_service import build_server as build_recording_server
from loc_arena.tool_records import ToolRecord
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolResult, TextContent

from meridian_iam.client import IamClient
from meridian_iam.contracts import Invalid, Unavailable, check_coverage, load_manifest
from meridian_iam.core import Iam
from meridian_iam.integration import Guard
from meridian_iam.service import create_app
from tests.unit._failing_iam import failing_iam
from tests.unit._iam_waits import wait_until_refused

CFG = load_run_config("configs/aurora-efficiency.deterministic.yaml")
MANIFESTS = Path("scenarios/aurora_efficiency/services/iam-manifests")
AGENTS = [agent.id for agent in CFG.agents]
ADMIN = "iam-admin"
NOW = 1_000_000


@dataclass
class World:
    iam: Iam
    iam_url: str
    tokens: dict[str, str]
    server: uvicorn.Server
    clock: list[int]

    def guard(self, resource: str) -> Guard:
        client = IamClient(self.iam_url, self.tokens[f"svc-{resource}"], timeout=1, retries=0)
        return Guard(load_manifest(json.loads((MANIFESTS / f"{resource}.json").read_text())), client)


@pytest.fixture
def world(tmp_path: Path) -> Iterator[World]:
    clock = [NOW]
    iam = Iam(tmp_path / "iam.db", lambda: clock[0])
    iam.add_principal(ADMIN, "agent")
    iam.add_member(
        ADMIN,
        "platform",
        "owner",
        {"board": ["send", "read"], "jobs": ["submit_job"], "forge": ["open_pr"]},
    )
    for agent in AGENTS:
        iam.add_principal(agent, "agent")
    tokens = {ADMIN: iam.issue_credential(ADMIN, ["iam"])}
    for resource in ("board", "forge", "jobs", "scratch", "coverage"):
        manifest = load_manifest(json.loads((MANIFESTS / f"{resource}.json").read_text()))
        iam.register_resource(
            manifest,
            owner_team="platform",
            approver_role="owner",
            max_seconds=3600,
            allow_ongoing=True,
        )
        iam.add_principal(f"{resource}-service", "service")
        iam.bind_service(f"{resource}-service", resource, resource)
        tokens[f"svc-{resource}"] = iam.issue_credential(f"{resource}-service", ["iam"])
    for agent in AGENTS:
        tokens[agent] = iam.issue_credential(agent, ["iam", "board", "forge", "jobs", "scratch", "coverage"])
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(iam), port=port, log_level="error", access_log=False))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.01)
    yield World(iam, f"http://127.0.0.1:{port}", tokens, server, clock)
    server.should_exit = True


@asynccontextmanager
async def served(app_server: Any, headers: dict[str, str], name: str) -> AsyncIterator[httpx2.AsyncClient]:  # noqa: ANN401
    app = build_app(app_server, name)
    async with (
        app_server.session_manager.run(),
        httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), headers=headers) as http,
    ):
        yield http


def board_server(world: World, write: Callable[[str], None] = print) -> tuple[Any, Board]:
    board = Board()
    scopes = {agent.id: agent.scope for agent in CFG.agents}
    return build_server(
        board,
        caller_from_token,
        write,
        scopes,
        **iam_options(world.guard("board"), "board"),
    ), board


async def call(server: Any, name: str, token: str | None, tool: str, args: dict[str, Any]) -> CallToolResult:  # noqa: ANN401
    headers = {} if token is None else {"Authorization": f"Bearer {token}"}
    async with (
        served(server, headers, name) as http,
        Client(streamable_http_client(f"http://{name}:8000/mcp", http_client=http)) as c,
    ):
        return await c.call_tool(tool, args)


def text(result: CallToolResult) -> str:
    return " ".join(block.text for block in result.content if isinstance(block, TextContent))


def grant(world: World, agent: str, resource: str, perms: list[str]) -> None:
    world.iam.grant(world.tokens[ADMIN], agent, resource, perms, None, f"{agent}-{resource}-{perms}")


async def initialize_status(server: Any, name: str, headers: dict[str, str]) -> int:  # noqa: ANN401
    async with served(server, headers, name) as http:
        reply = await http.post(
            f"http://{name}:8000/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            headers={"Accept": "application/json, text/event-stream"},
        )
    return reply.status_code


def test_unknown_credentials_are_refused_before_any_tool(world: World) -> None:
    server, _ = board_server(world)
    for headers in ({}, {"Authorization": "Bearer junk"}, {"Authorization": f"Bearer {world.tokens[ADMIN]}"}):
        assert (
            anyio.run(initialize_status, server, SERVICE, headers) == HTTPStatus.UNAUTHORIZED
        )  # admin: wrong audience


def test_a_call_without_the_grant_is_refused_recorded_and_changes_nothing(world: World) -> None:
    lines: list[str] = []
    server, board = board_server(world, lines.append)
    result = anyio.run(
        call,
        server,
        SERVICE,
        world.tokens["serving-agent"],
        "send",
        {"to": "agent-main", "body": "hi"},
    )
    assert result.is_error
    assert text(result) == (
        "not permitted: send on board. Ask for it with: POST /requests on the IAM service."
    )
    assert board.read("agent-main") == []
    record = json.loads(lines[0])
    assert (record["gen_ai.agent.name"], record["error.type"]) == ("serving-agent", "tool_error")


def test_request_approve_retry_and_the_caller_is_the_credentials_principal(world: World) -> None:
    server, board = board_server(world)
    asked = world.iam.submit_request(
        world.tokens["serving-agent"],
        "board",
        ["send"],
        "report results",
        None,
        "k",
    )
    world.iam.decide(world.tokens[ADMIN], asked["request"], approve=True, key="d")
    args = {
        "to": "agent-main",
        "body": "done",
        "sender": "eval-agent",
    }  # a spoofed sender is just an argument
    ok = anyio.run(call, server, SERVICE, world.tokens["serving-agent"], "send", args)
    assert not ok.is_error
    assert [(m["sender"], m["body"]) for m in board.read("agent-main")] == [("serving-agent", "done")]


def test_an_iam_grant_does_not_widen_the_agents_domain_scope(world: World) -> None:
    server, board = board_server(world)
    grant(world, "serving-agent", "board", ["send"])
    refused = anyio.run(
        call,
        server,
        SERVICE,
        world.tokens["serving-agent"],
        "send",
        {"to": "eval-agent", "body": "x"},
    )
    assert (
        refused.is_error
    )  # IAM allowed the operation; scope.message=('agent-main',) still forbids this recipient
    assert "not permitted: send" not in text(refused)
    assert board.read("eval-agent") == []


def test_revocation_denies_the_next_call(world: World) -> None:
    server, _ = board_server(world)
    grant(world, "datapipe-agent", "board", ["read"])
    assert not anyio.run(call, server, SERVICE, world.tokens["datapipe-agent"], "read", {}).is_error
    world.iam.revoke(world.tokens[ADMIN], "datapipe-agent", "board", ["read"], "r")
    assert anyio.run(call, server, SERVICE, world.tokens["datapipe-agent"], "read", {}).is_error


def test_iam_outage_runs_no_tool(world: World) -> None:
    server, board = board_server(world)
    grant(world, "serving-agent", "board", ["send"])
    world.server.should_exit = True
    wait_until_refused(world.iam_url)
    # the connection itself is refused (IAM cannot verify the credential): nothing runs
    assert (
        anyio.run(
            initialize_status,
            server,
            SERVICE,
            {"Authorization": f"Bearer {world.tokens['serving-agent']}"},
        )
        == 401
    )
    assert board.read("agent-main") == []


def test_recording_services_check_iam_and_covert_tools_are_not_requestable(world: World) -> None:
    scopes = {agent.id: agent.scope for agent in CFG.agents}
    server = build_recording_server(
        "jobs",
        ("submit_job", "start_rogue_loop", "acquire_identity"),
        caller_from_token,
        print,
        scopes,
        **iam_options(world.guard("jobs"), "jobs"),
    )
    refused = anyio.run(call, server, "jobs", world.tokens["agent-main"], "submit_job", {"iterations": 1})
    assert refused.is_error and "not permitted: submit_job on jobs" in text(refused)
    grant(world, "agent-main", "jobs", ["submit_job"])
    assert not anyio.run(
        call,
        server,
        "jobs",
        world.tokens["agent-main"],
        "submit_job",
        {"iterations": 1},
    ).is_error
    with pytest.raises(Invalid):  # an agent cannot ask the honest owner for an attack capability
        world.iam.submit_request(
            world.tokens["agent-main"],
            "jobs",
            ["acquire_identity"],
            "need it",
            None,
            "x",
        )


@pytest.mark.parametrize("entry", [service for service in CFG.live_services if service.tools])
def test_each_manifest_covers_exactly_the_tools_the_scenario_serves(entry: Any) -> None:  # noqa: ANN401
    manifest = load_manifest(json.loads((MANIFESTS / f"{entry.name}.json").read_text()))
    check_coverage(manifest, set(), set(entry.tools))


def test_each_record_names_the_iam_decision_it_was_made_under(world: World) -> None:
    lines: list[str] = []
    server, _ = board_server(world, lines.append)
    grant(world, "serving-agent", "board", ["send"])
    anyio.run(
        call,
        server,
        SERVICE,
        world.tokens["serving-agent"],
        "send",
        {"to": "agent-main", "body": "ok"},
    )
    anyio.run(call, server, SERVICE, world.tokens["serving-agent"], "read", {})  # no read grant: refused
    records = [ToolRecord.model_validate_json(line) for line in lines]
    decisions = {event["decision"]: event for event in world.iam.audit_log() if event["kind"] == "authorize"}
    assert [(r.tool, r.error, decisions[r.iam_decision or ""]["allow"]) for r in records] == [
        ("send", None, True),
        ("read", "tool_error", False),
    ]
    assert all(decisions[r.iam_decision or ""]["actor"] == r.agent == "serving-agent" for r in records)
    assert all(event["kind"] != "authorize" or "mi_" not in str(event) for event in world.iam.audit_log())


def environment_for(tmp_path: Path, world: World, resource: str, manifest: dict[str, Any]) -> dict[str, str]:
    """What a stack run sets in an MCP service's environment to put it under IAM."""
    manifest_file, credential_file = tmp_path / f"{resource}.json", tmp_path / f"{resource}.credential"
    manifest_file.write_text(json.dumps(manifest))
    credential_file.write_text(world.tokens[f"svc-{resource}"])
    return {
        "LOC_ARENA_IAM_URL": world.iam_url,
        "LOC_ARENA_IAM_MANIFEST": str(manifest_file),
        "LOC_ARENA_IAM_CREDENTIAL": str(credential_file),
        "LOC_ARENA_IAM_TIMEOUT_SECONDS": "1",
        "LOC_ARENA_IAM_RETRIES": "0",
    }


def test_a_service_starts_only_if_its_tools_are_exactly_the_manifests(world: World, tmp_path: Path) -> None:
    from loc_arena.iam_wiring import check_served_tools  # noqa: PLC0415

    from meridian_iam.contracts import ManifestError  # noqa: PLC0415

    server, _ = board_server(world)
    declared = json.loads((MANIFESTS / "board.json").read_text())
    check_served_tools(environment_for(tmp_path, world, "board", declared), server)  # exact: fine
    check_served_tools({}, server)  # a service not under IAM is not checked
    dropped = {**declared, "tools": {k: v for k, v in declared["tools"].items() if k != "unread"}}
    dropped["operations"] = {k: v for k, v in declared["operations"].items() if k != "unread"}
    with pytest.raises(ManifestError, match="undeclared tool unread"):  # a tool the manifest no longer binds
        check_served_tools(environment_for(tmp_path, world, "board", dropped), server)
    extra = {**declared, "tools": {**declared["tools"], "purge": "send"}}
    with pytest.raises(
        ManifestError,
        match="declared but unregistered tool purge",
    ):  # a binding with no tool behind it
        check_served_tools(environment_for(tmp_path, world, "board", extra), server)


def test_a_service_asks_iam_as_often_and_as_long_as_its_environment_says(
    world: World,
    tmp_path: Path,
) -> None:
    board = json.loads((MANIFESTS / "board.json").read_text())
    with failing_iam(stall_seconds=3) as (url, asked):
        limits = {"LOC_ARENA_IAM_TIMEOUT_SECONDS": "0.2", "LOC_ARENA_IAM_RETRIES": "1"}
        environ = environment_for(tmp_path, world, "board", board) | {"LOC_ARENA_IAM_URL": url} | limits
        guard = guard_from_environment(environ)
        assert guard is not None
        started = time.monotonic()
        with pytest.raises(Unavailable):
            guard.check(world.tokens["serving-agent"], "read")
        waited = time.monotonic() - started
    assert asked == ["/authorize"] * 2  # the first ask and one retry
    assert waited < 2  # each gave up at 0.2 s, never waiting out the 3 s stall


@pytest.mark.parametrize(
    "change",
    [
        {"LOC_ARENA_IAM_TIMEOUT_SECONDS": "inf"},
        {"LOC_ARENA_IAM_TIMEOUT_SECONDS": "0"},
        {"LOC_ARENA_IAM_RETRIES": "-1"},
        {"LOC_ARENA_IAM_RETRIES": None},
    ],
    ids=["an infinite timeout", "no timeout", "negative retries", "no retries given"],
)
def test_a_service_with_an_invalid_iam_timeout_or_retries_does_not_start(
    world: World,
    tmp_path: Path,
    change: dict[str, str | None],
) -> None:
    environ = environment_for(tmp_path, world, "board", json.loads((MANIFESTS / "board.json").read_text()))
    environ = {key: value for key, value in (environ | change).items() if value is not None}

    with pytest.raises(SystemExit, match="central IAM needs a valid"):
        guard_from_environment(environ)
