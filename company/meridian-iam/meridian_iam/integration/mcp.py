"""MCP adapter: IAM verifies who connects, and the business operation is checked before every tool runs.

Protocol setup and ``tools/list`` need only a valid credential (they run nothing); every ``tools/call`` goes
through ``Guard.check_tool``. A refusal is an ordinary tool-error result: the calling agent sees why and the
recorder still writes the line. Needs the ``mcp`` extra.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any

from fastapi.concurrency import run_in_threadpool
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.context import CallNext, HandlerResult, ServerMiddleware, ServerRequestContext

from meridian_iam.contracts import IamError
from meridian_iam.integration.guard import AccessDenied, Guard

TOOL_CALL = "tools/call"
DECISION_ATTRIBUTE = "iam.decision.id"  # on a tool-call record: the join to IAM's audit event
_decision_id: ContextVar[str | None] = ContextVar("iam_decision_id", default=None)


def decision_attributes() -> dict[str, str]:
    """The record attribute naming the IAM decision of the call being recorded, if it had one."""
    decision = _decision_id.get()
    return {} if decision is None else {DECISION_ATTRIBUTE: decision}


class IamVerifier(TokenVerifier):
    """Admits a bearer credential IAM accepts for this service's audience, as the principal it names."""

    def __init__(self, guard: Guard, audience: str) -> None:
        """Verify against ``guard``'s IAM client for ``audience``."""
        self._guard = guard
        self._audience = audience

    async def verify_token(self, token: str) -> AccessToken | None:
        """The principal as the token's client, or ``None`` (401) for anything IAM does not accept."""
        try:
            principal = await run_in_threadpool(self._guard.client.identify, token, self._audience)
        except IamError:
            return None
        return AccessToken(token=token, client_id=principal, scopes=[])


def refusal(message: str) -> dict[str, Any]:
    """The wire form of a tool-error result."""
    return {"resultType": "complete", "content": [{"type": "text", "text": message}], "isError": True}


def tool_authorizer(guard: Guard) -> ServerMiddleware[Any]:
    """A middleware that checks IAM before each ``tools/call``; place it inside the call recorder."""

    async def authorize(ctx: ServerRequestContext[Any, Any], call_next: CallNext) -> HandlerResult:
        if ctx.method != TOOL_CALL:
            return await call_next(ctx)
        _decision_id.set(None)
        params = ctx.params or {}
        header = ctx.request.headers.get("authorization", "") if ctx.request is not None else ""
        token = header.removeprefix("Bearer ") if header.startswith("Bearer ") else None
        try:
            decision = await run_in_threadpool(
                guard.check_tool,
                token,
                str(params.get("name")),
                params.get("arguments") or {},
            )
            _decision_id.set(None if decision is None else decision.decision_id)
        except AccessDenied as denied:
            _decision_id.set(denied.decision_id)
            guidance = denied.guidance
            where = f" Ask for it with: {guidance['request_with']}." if guidance["request_with"] else ""
            return refusal(f"not permitted: {guidance['permission']} on {guidance['resource']}.{where}")
        except IamError as error:
            return refusal(f"{error.code}: no tool ran")
        return await call_next(ctx)

    return authorize
