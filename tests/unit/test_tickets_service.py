"""The tickets service as a stack run serves it: tools and HTTP API on one app over one seeded store.

A real central IAM decides every call.
"""

from __future__ import annotations

import dataclasses
import json
import socket
import threading
import time
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from typing import Any

import anyio
import httpx2
import pytest
import uvicorn
from loc_arena.config import load_run_config
from loc_arena.iam_seed import REPOSITORY
from loc_arena.recorded_events import _record_line_parser
from loc_arena.settings import TicketsSettings
from loc_arena.tickets_service import build_tickets_app
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from starlette.applications import Starlette

from meridian_iam.client import IamClient
from meridian_iam.contracts import ManifestError, load_manifest
from meridian_iam.core import Iam
from meridian_iam.integration import Guard
from meridian_iam.service import create_app

CFG = load_run_config("configs/aurora-efficiency.iam.yaml")
TICKETS = next(service for service in CFG.live_services if service.name == "tickets")
MANIFEST = REPOSITORY / "company/meridian-controlplane/meridian_controlplane/tickets/iam-manifest.json"
SERVICE = TICKETS.name
URL = f"http://{SERVICE}:{TICKETS.port}"
WRITER, READER = "serving-agent", "eval-agent"
AGENTS = [agent.id for agent in CFG.agents]

Exchange = Callable[[httpx2.AsyncClient, Client], Awaitable[Any]]


def serve(app: Any) -> tuple[uvicorn.Server, str]:  # noqa: ANN401
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
    listener = socket.create_server(("127.0.0.1", 0))
    threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True).start()
    while not server.started:
        time.sleep(0.01)
    return server, f"http://127.0.0.1:{listener.getsockname()[1]}"


@dataclass
class World:
    """Central IAM on a local port, credentials of its principals, and the tickets app a stack run serves."""

    iam: Iam
    iam_url: str
    tokens: dict[str, str]
    guard: Guard
    config: Any  # noqa: ANN401 - the run config, its database in the test's directory
    lines: list[str]
    app: Starlette

    def grant(self, who: str, permissions: list[str]) -> None:
        self.iam.grant(self.tokens["admin"], who, SERVICE, permissions, None, f"{who}-{permissions}")

    def run(self, *turns: tuple[str, Exchange]) -> list[Any]:
        """Each turn's result over the app, in order, all in one lifespan of the app, which starts only once.

        A turn's exchange runs through an HTTP and an MCP client with its agent's credential.
        """

        async def play() -> list[Any]:
            results = []
            async with self.app.router.lifespan_context(self.app):
                for agent, exchange in turns:
                    headers = {"Authorization": f"Bearer {self.tokens[agent]}"}
                    transport = httpx2.ASGITransport(app=self.app)
                    async with (
                        httpx2.AsyncClient(transport=transport, headers=headers, base_url=URL) as http,
                        Client(streamable_http_client(f"{URL}/mcp", http_client=http)) as client,
                    ):
                        results.append(await exchange(http, client))
            return results

        return anyio.run(play)


@pytest.fixture
def world(tmp_path: Path) -> Iterator[World]:
    iam = Iam(tmp_path / "iam.db", lambda: 1_000_000)
    for principal in (*AGENTS, "lead"):
        iam.add_principal(principal, "agent")
    iam.add_principal("tickets-svc", "service")
    iam.add_member("lead", "platform", "owner", {SERVICE: ["read", "write"]})
    iam.register_resource(
        load_manifest(json.loads(MANIFEST.read_text())),
        owner_team="platform",
        approver_role="owner",
        max_seconds=3600,
        allow_ongoing=True,
    )
    iam.bind_service("tickets-svc", SERVICE, SERVICE)
    tokens = {agent: iam.issue_credential(agent, [SERVICE, f"iam:{SERVICE}"]) for agent in AGENTS}
    tokens["admin"] = iam.issue_credential("lead", ["iam"])
    tokens["svc"] = iam.issue_credential("tickets-svc", ["iam"])
    iam_server, iam_url = serve(create_app(iam))
    client = IamClient(iam_url, tokens["svc"], timeout=1)
    guard = Guard(load_manifest(json.loads(MANIFEST.read_text())), client)
    settings = CFG.settings.model_copy(update={"tickets": TicketsSettings(database=tmp_path / "tickets.db")})
    config = dataclasses.replace(CFG, settings=settings)
    lines: list[str] = []
    app = build_tickets_app(config, SERVICE, guard, lines.append)
    yield World(iam, iam_url, tokens, guard, config, lines, app)
    iam_server.should_exit = True


def test_a_ticket_filed_with_a_tool_is_read_over_http_as_its_callers_after_the_seeds(world: World) -> None:
    world.grant(WRITER, ["read", "write"])

    async def exchange(http: httpx2.AsyncClient, client: Client) -> tuple[Any, Any]:
        filed = await client.call_tool("create_ticket", {"project": "AURORA", "title": "Profile the sampler"})
        read = await http.get(f"/tickets/{(filed.structured_content or {})['id']}")
        return filed.structured_content, read.json()

    [(filed, read)] = world.run((WRITER, exchange))

    assert ((filed or {})["id"], read["creator"], read["status"]) == ("AURORA-6", WRITER, "open")


def test_an_unknown_credential_is_refused_by_the_tools_and_the_api(world: World) -> None:
    initialize = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    guessed = {"Authorization": "Bearer guessed"}

    async def exchange(http: httpx2.AsyncClient, _: Client) -> tuple[int, int]:
        accept = {"Accept": "application/json, text/event-stream"}
        tools = await http.post("/mcp", json=initialize, headers={**accept, **guessed})
        api = await http.get("/projects", headers=guessed)
        return tools.status_code, api.status_code

    assert world.run((WRITER, exchange)) == [(HTTPStatus.UNAUTHORIZED, HTTPStatus.UNAUTHORIZED)]


def test_a_reader_reads_over_http_and_mcp_and_is_refused_every_change(world: World) -> None:
    world.grant(READER, ["read"])

    async def exchange(http: httpx2.AsyncClient, client: Client) -> tuple[int, int, bool, bool]:
        read = await http.get("/projects")
        created = await http.post("/projects", json={"key": "OPS", "name": "Ops"})
        listed = await client.call_tool("list_projects", {})
        filed = await client.call_tool("create_ticket", {"project": "AURORA", "title": "Mine now"})
        return read.status_code, created.status_code, listed.is_error, filed.is_error

    assert world.run((READER, exchange)) == [(HTTPStatus.OK, HTTPStatus.FORBIDDEN, False, True)]


def test_each_change_or_refusal_is_an_action_on_its_ticket_and_an_answered_read_none(world: World) -> None:
    world.grant(WRITER, ["read", "write"])
    world.grant(READER, ["read"])

    async def work(http: httpx2.AsyncClient, client: Client) -> None:
        await client.call_tool("edit_ticket", {"ticket": "AURORA-2", "status": "in_progress"})
        await client.call_tool("get_ticket", {"ticket": "AURORA-2"})
        await http.post("/tickets/AURORA-2/comments", json={"body": "Batching by length now."})
        await http.get("/tickets/AURORA-2")

    async def overreach(_: httpx2.AsyncClient, client: Client) -> None:
        await client.call_tool("comment_on_ticket", {"ticket": "AURORA-3", "body": "Mine now."})

    world.run((WRITER, work), (READER, overreach))

    parse = _record_line_parser(TICKETS, world.config)
    events = [event for line in world.lines if (parsed := parse(line, AGENTS)) for event in parsed.events]
    assert [(e.kind, e.tool, e.actor_uid, e.target_id, e.payload["blocked"]) for e in events] == [
        ("action", "edit_ticket", WRITER, "AURORA-2", False),
        ("action", "comment_on_ticket", WRITER, "AURORA-2", False),
        ("action", "comment_on_ticket", READER, "AURORA-3", True),
    ]
    assert all("iam.decision.id" in line or '"decision"' in line for line in world.lines)


def test_startup_refuses_a_manifest_that_binds_no_tool_to_a_served_one(world: World) -> None:
    declared = json.loads(MANIFEST.read_text())
    del declared["tools"]["comment_on_ticket"]
    guard = Guard(load_manifest(declared), world.guard.client)

    with pytest.raises(ManifestError, match="undeclared tool comment_on_ticket"):
        build_tickets_app(world.config, SERVICE, guard, print)
