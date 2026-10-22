"""The eval service in a run: rights without IAM, one app for its tools and API, and its records as events."""

from __future__ import annotations

import dataclasses
import json

import anyio
import httpx2
from loc_arena.config import load_run_config
from loc_arena.evalkit_service import rights_by_caller, serving_app
from loc_arena.recorded_events import _record_line_parser

from meridian_evalkit.service import (
    Callers,
    EvalkitRegistry,
    build_evalkit_server,
    create_evalkit_api,
    load_seed,
)

CONFIG = load_run_config("configs/aurora-efficiency.deterministic.yaml")
EVALKIT = next(service for service in CONFIG.live_services if service.name == "evalkit")
AGENTS = tuple(agent.id for agent in CONFIG.agents)


def test_without_iam_a_tool_an_agent_holds_grants_the_right_its_route_needs() -> None:
    first = CONFIG.agents[0]
    sandbox = dataclasses.replace(first.sandbox, rights={"evalkit": ()}, tools={"evalkit": ("start_run",)})
    config = dataclasses.replace(
        CONFIG,
        agents=(dataclasses.replace(first, sandbox=sandbox), *CONFIG.agents[1:]),
    )

    rights = rights_by_caller(config, "evalkit")

    # Every other agent's rights are read off configs/aurora-efficiency.yaml's sandbox blocks.
    assert (rights[first.id], rights["eval-agent"], rights["controlplane-agent"]) == (
        {"run"},
        {"read", "run", "curate"},
        {"read"},
    )


def test_a_service_with_tools_and_rights_records_tool_calls_and_requests_both() -> None:
    parse = _record_line_parser(EVALKIT, CONFIG)
    tool_call = {
        "time": "2026-09-01T12:00:00+00:00",
        "gen_ai.tool.name": "start_run",
        "gen_ai.tool.call.arguments": {"eval_id": "arithmetic", "model_id": "aurora-7b-v3"},
        "gen_ai.tool.call.result": {"run_id": "run-0439"},
        "gen_ai.agent.name": "agent-main",
    }
    request = {
        "wall_ts": "2026-09-01T12:00:01+00:00",
        "caller": "agent-main",
        "method": "POST",
        "path": "/runs/run-0439/publish",
        "status": 200,
        "operation": "publish_run",
        "arguments": {"run_id": "run-0439"},
    }
    read = {**request, "method": "GET", "path": "/runs/run-0439", "operation": "get_run"}

    parsed = [parse(json.dumps(line), AGENTS) for line in (tool_call, request, read)]

    assert [[(e.kind, e.tool, e.actor_uid, e.target_id) for e in p.events] if p else p for p in parsed] == [
        [("action", "start_run", "agent-main", "arithmetic")],
        [("action", "publish_run", "agent-main", "run-0439")],
        [],
    ]


def test_the_tools_and_the_api_are_served_as_one_app() -> None:
    registry = EvalkitRegistry(load_seed())
    api = create_evalkit_api(
        registry,
        print,
        callers=Callers({"priya": b"priya-identity"}, {"priya": ["read"]}),
    )
    app = serving_app(build_evalkit_server(registry, lambda: None), api, "evalkit")

    async def read() -> tuple[int, str, int]:
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://evalkit") as http:
            health = await http.get("/health")
            evals = await http.get("/evals", headers={"Authorization": "Bearer priya-identity"})
            return health.status_code, health.text, evals.status_code

    assert anyio.run(read) == (200, "ok", 200)
    assert "/mcp" in {getattr(route, "path", None) for route in app.routes}
