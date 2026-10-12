"""The agents service's HTTP API over the agent registry, with access control and an audit record.

A request with a malformed body is refused with 422. A call of the API without a known identity is refused
with 401, and one without the right with 403; ``/``, ``/health`` and ``/openapi.json`` are open. Each request
is one JSON line of the audit record: its time, the caller, the container it came from, the method, path and
status, and, for an operation of the API, the operation's name and its arguments. Each operation is named
after its route's handler.
"""

from __future__ import annotations

import functools
import json
import socket
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from http import HTTPStatus

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import PlainTextResponse
from pydantic import JsonValue
from starlette.middleware.base import RequestResponseEndpoint

from meridian_controlplane.agents.access_control import GRANT, READ, REGISTER, AccessControl, RightsChange
from meridian_controlplane.agents.registry import (
    AgentDefinition,
    AgentRegistration,
    AgentRegistry,
    AgentSummary,
    AgentVersion,
)

API_DESCRIPTION = """Meridian's agents service: the registry of the agents the company runs.
Send your identity as "Authorization: Bearer <identity>".
GET /agents lists the agents; GET /agents/<name> reads one, GET /agents/<name>/versions its versions (read).
POST /agents with {{"name", "model", "system_prompt"}} registers one; PUT /agents/<name> with {{"model",
"system_prompt"}} makes its next version; DELETE /agents/<name> retires it (register).
GET /grants lists every caller's rights. POST /grants with {{"agent": "<id>", "rights": [...]}} gives that
caller rights you hold; DELETE /grants with the same body takes them away (grant).
Rights: {rights}. The grant right itself can be granted: {grantable}. The schema is at /openapi.json.
"""


def create_agents_service_app(
    registry: AgentRegistry,
    access_control: AccessControl,
    write_audit_line: Callable[[str], None],
) -> FastAPI:
    """The API over ``registry``, open to the callers ``access_control`` knows, auditing every request."""
    app = FastAPI(title="Meridian agents service", docs_url=None, redoc_url=None)

    def put_operation_on_audit_line(request: Request, arguments: Mapping[str, JsonValue]) -> str | None:
        """Name the route's operation, its arguments and its caller on the audit line; returns the caller."""
        request.state.operation = request.scope["route"].name
        request.state.arguments = dict(arguments)
        request.state.caller = access_control.identify_caller(request.headers.get("Authorization", ""))
        return request.state.caller

    def authorize(request: Request, arguments: Mapping[str, JsonValue], required_right: str | None) -> str:
        """The caller, once it may run the route's operation."""
        caller = put_operation_on_audit_line(request, arguments)
        if caller is None:
            raise HTTPException(HTTPStatus.UNAUTHORIZED, "send a known identity as a bearer token")
        if required_right is not None and not access_control.holds_right(caller, required_right):
            raise HTTPException(HTTPStatus.FORBIDDEN, f"needs the {required_right!r} right")
        return caller

    @app.middleware("http")
    async def write_audit_line_for_each_request(
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        response = await call_next(request)
        operation = getattr(request.state, "operation", None)
        audit_line = {
            "wall_ts": datetime.now(UTC).isoformat(),
            "caller": getattr(request.state, "caller", None),
            "container": _name_container(request.client.host) if request.client else None,
            "method": request.method,
            "path": request.url.path,
            "status": response.status_code,
            **({"operation": operation, "arguments": request.state.arguments} if operation else {}),
        }
        write_audit_line(json.dumps(audit_line))
        return response

    @app.exception_handler(RequestValidationError)
    async def refuse_malformed_request(request: Request, error: RequestValidationError) -> Response:
        """Refused before the route can authorize, a malformed body's audit line still names the operation."""
        put_operation_on_audit_line(request, request.path_params)
        return await request_validation_exception_handler(request, error)

    @app.get("/health", response_class=PlainTextResponse)
    async def health() -> str:
        return "ok"

    @app.get("/", response_class=PlainTextResponse)
    async def describe_api() -> str:
        grantable = "yes" if access_control.grant_right_is_grantable else "no"
        return API_DESCRIPTION.format(rights=", ".join(access_control.declared_rights), grantable=grantable)

    @app.get("/agents")
    async def list_agents(request: Request) -> list[AgentSummary]:
        authorize(request, {}, READ)
        return registry.list_agents()

    @app.get("/agents/{name}")
    async def read_agent(request: Request, name: str) -> AgentSummary:
        authorize(request, {"name": name}, READ)
        return registry.get_agent(name)

    @app.get("/agents/{name}/versions")
    async def list_versions(request: Request, name: str) -> list[AgentVersion]:
        authorize(request, {"name": name}, READ)
        return registry.list_versions(name)

    @app.post("/agents", status_code=HTTPStatus.CREATED)
    async def register_agent(request: Request, registration: AgentRegistration) -> AgentSummary:
        caller = authorize(request, registration.model_dump(), REGISTER)
        return registry.register(registration, registered_by=caller)

    @app.put("/agents/{name}")
    async def update_agent(request: Request, name: str, definition: AgentDefinition) -> AgentSummary:
        caller = authorize(request, {"name": name, **definition.model_dump()}, REGISTER)
        return registry.add_version(name, definition, created_by=caller)

    @app.delete("/agents/{name}")
    async def retire_agent(request: Request, name: str) -> AgentSummary:
        authorize(request, {"name": name}, REGISTER)
        return registry.retire(name)

    @app.get("/grants")
    async def list_grants(request: Request) -> dict[str, list[str]]:
        authorize(request, {}, required_right=None)
        return access_control.list_rights_by_caller()

    @app.post("/grants")
    async def grant_rights(request: Request, change: RightsChange) -> list[str]:
        granter = authorize(request, change.model_dump(), GRANT)
        return access_control.grant_rights(granter, change)

    @app.delete("/grants")
    async def revoke_rights(request: Request, change: RightsChange) -> list[str]:
        revoker = authorize(request, change.model_dump(), GRANT)
        return access_control.revoke_rights(revoker, change)

    return app


@functools.cache
def _name_container(address: str) -> str:
    """The container at ``address``: its name on the network, else the address itself."""
    try:
        return socket.gethostbyaddr(address)[0]
    except OSError:
        return address
