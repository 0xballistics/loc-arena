"""The shared enforcement point: resolve the operation, ask IAM online, execute only on a matching allow."""

from __future__ import annotations

from typing import Any

from meridian_iam.client import IamClient
from meridian_iam.contracts import (
    Decision,
    Forbidden,
    Manifest,
    Unauthenticated,
    Unavailable,
    check_coverage,
    resolve_http,
)

BEARER = "Bearer "


class AccessDenied(Forbidden):
    """A 403 that names the missing permission and where to ask for it, and nothing about other agents."""

    def __init__(
        self,
        resource: str,
        permission: str | None,
        *,
        requestable: bool,
        principal: str | None = None,
        decision_id: str | None = None,
    ) -> None:
        """Build the safe guidance a denied caller sees; ``principal`` is for the audit line only."""
        super().__init__("insufficient permission")
        self.principal = principal
        self.decision_id = decision_id
        self.guidance: dict[str, Any] = {
            "error": self.code,
            "resource": resource,
            "permission": permission,
            "request_with": "POST /requests on the IAM service" if requestable else None,
        }


class Guard:
    """One per resource service: its manifest plus a client authenticated as the service's identity."""

    def __init__(self, manifest: Manifest, client: IamClient) -> None:
        """Hold the validated manifest and the IAM client."""
        self.manifest = manifest
        self.client = client

    def check(self, caller_token: str | None, operation: str, target: str | None = None) -> Decision | None:
        """Raise unless this exact call is allowed. Public operations return ``None`` without asking IAM.

        Unknown operations fail closed; an unavailable or inconsistent authority is ``Unavailable``
        (503), never a missing grant.
        """
        spec = self.manifest.operations.get(operation)
        if spec is None:
            raise AccessDenied(self.manifest.resource, None, requestable=False)
        if spec.public:
            return None
        if not caller_token:
            raise Unauthenticated("send a credential as a bearer token")
        decision = self.client.authorize(caller_token, operation, target)
        if not decision.matches(self.manifest.resource, operation, target):
            raise Unavailable("IAM decision does not match this call")
        if not decision.allow:
            raise AccessDenied(
                self.manifest.resource,
                spec.permission,
                requestable=spec.requestable,
                principal=decision.principal,
                decision_id=decision.decision_id,
            )
        return decision

    def check_http(self, method: str, path: str, authorization: str | None) -> Decision | None:
        """Stdlib-server adapter: resolve with the manifest's matcher, then ``check`` (ASGI: see ``asgi``)."""
        resolved = resolve_http(self.manifest, method, path)
        if resolved is None:
            raise AccessDenied(self.manifest.resource, None, requestable=False)
        operation, target = resolved
        return self.check(_bearer(authorization), operation.name, target)

    def check_tool(self, caller_token: str | None, tool: str, arguments: dict[str, Any]) -> Decision | None:
        """MCP adapter: map the tool to its operation and extract the target from its arguments."""
        name = self.manifest.tools.get(tool)
        if name is None:
            raise AccessDenied(self.manifest.resource, None, requestable=False)
        target_name = self.manifest.operations[name].target
        raw = arguments.get(target_name) if target_name else None
        return self.check(caller_token, name, None if raw is None else str(raw))

    def verify_routes(self, routes: set[str] | None, tools: set[str] | None = None) -> None:
        """Startup check: registered routes and tools must match the manifest; ``None`` skips a side."""
        check_coverage(self.manifest, routes, tools)


def _bearer(header: str | None) -> str | None:
    return header.removeprefix(BEARER) if header and header.startswith(BEARER) else None
