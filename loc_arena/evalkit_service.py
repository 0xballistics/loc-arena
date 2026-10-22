"""The eval service as a stack run's live service: Meridian's evals, runs and leaderboards, as company code.

A stack run starts it as the scenario's ``evalkit`` entry: ``python -m loc_arena.evalkit_service <run config>
<entry name>``. It serves ``meridian_evalkit.service`` on one port: the MCP tools at ``/mcp``, which agents
reach through the tools gateway, and the HTTP API beside them, which they call from their sandbox with their
identity on the service. Under central IAM, IAM decides every call; otherwise each agent's identity is mounted
in the secrets directory and its rights are the run config's: its ``sandbox: rights:`` on the service and the
permission of each tool its ``sandbox: tools:`` lists there, as the IAM seed reads them, so a tool enforces
the same right as its route in every run. Each HTTP request is one line of the audit record and each tool
call one line of the tool-call record, both on stdout. The registry starts from the package's seed, and
agents' edits to the package do not go live.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, ConfigDict
from starlette.applications import Starlette
from starlette.routing import Mount

import meridian_evalkit.service
from loc_arena.config import RunConfig, load_run_config
from loc_arena.iam_wiring import (
    check_served_tools,
    guard_from_environment,
    load_iam_authority,
    serving_options,
)
from loc_arena.mcp_service import ALL_ADDRESSES, caller_from_token, read_identities
from loc_arena.tool_records import tool_call_recorder
from meridian_evalkit.service import (
    Callers,
    EvalkitRegistry,
    JobResources,
    build_evalkit_server,
    create_evalkit_api,
    load_seed,
)

MANIFEST = Path(meridian_evalkit.service.__file__).with_name("iam-manifest.json")


class _Operation(BaseModel):
    model_config = ConfigDict(extra="ignore")

    permission: str | None = None


class _Manifest(BaseModel):
    """What the service's IAM manifest says of its tools: the permission each one's operation needs."""

    model_config = ConfigDict(extra="ignore")

    operations: dict[str, _Operation]
    tools: dict[str, str]

    def permission_of_tool(self, tool: str) -> str:
        permission = self.operations[self.tools[tool]].permission
        if permission is None:
            raise ValueError(f"tool {tool!r} is bound to an operation with no permission")
        return permission


def rights_by_caller(config: RunConfig, service: str) -> dict[str, set[str]]:
    """Each agent's rights on ``service`` without IAM: its starting rights and the permission of each tool."""
    manifest = _Manifest.model_validate_json(MANIFEST.read_text())
    return {
        agent.id: {
            *agent.sandbox.rights.get(service, ()),
            *(manifest.permission_of_tool(tool) for tool in agent.sandbox.tools.get(service, ())),
        }
        for agent in config.agents
    }


def serving_app(server: MCPServer, api: FastAPI, service: str) -> Starlette:
    """One app on one port: the tools at ``/mcp`` and the HTTP API, for requests addressed to ``service``."""
    security = TransportSecuritySettings(allowed_hosts=[service, f"{service}:*"])
    app = server.streamable_http_app(transport_security=security)
    app.router.routes.append(Mount("/", app=api))  # after /mcp, so the HTTP API serves the rest
    return app


def write_record_line(line: str) -> None:
    """One line of the audit record or the tool-call record, on stdout."""
    print(line, flush=True)


def main(arguments: list[str]) -> None:
    """Serve entry ``arguments[1]`` of the run config at ``arguments[0]``."""
    config = load_run_config(arguments[0])
    entry = next(service for service in config.live_services if service.name == arguments[1])
    settings = config.settings
    registry = EvalkitRegistry(
        load_seed(),
        url=f"http://{entry.name}:{entry.port}",
        resources=JobResources(cpu=settings.evalkit.job_cpu, memory_mb=settings.evalkit.job_memory_mb),
    )
    identities = read_identities(settings.gateway.secrets_dir)
    guard = guard_from_environment(os.environ)
    if guard is not None:  # central IAM decides every call: no identity or rights table is kept here
        api = create_evalkit_api(registry, write_record_line, authority=load_iam_authority(guard))
        may = None
    else:
        if not identities:
            raise SystemExit(f"no identity in {settings.gateway.secrets_dir}")
        callers = Callers(identities, rights_by_caller(config, entry.name))
        api = create_evalkit_api(registry, write_record_line, callers=callers)
        may = callers.holds
    options = serving_options(entry.name, identities, os.environ)
    recorder = tool_call_recorder(caller_from_token, write_record_line, options["annotate"])
    server = build_evalkit_server(
        registry,
        caller_from_token,
        may,
        options["verifier"],
        [recorder, *options["extra_middleware"]],
    )
    check_served_tools(os.environ, server)
    uvicorn.run(
        serving_app(server, api, entry.name),
        host=ALL_ADDRESSES,
        port=entry.port,
        log_level="warning",
        access_log=False,
    )


if __name__ == "__main__":
    main(sys.argv[1:])
