"""What the job service needs of a central authorization service, without importing one.

The company package stays free of any platform dependency: a deployment hands in an implementation of this
protocol, which decides every call before it runs and names its caller. The service keeps no identity and no
rights of its own; it checks only that a job is the caller's own before changing it.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol

from fastapi.params import Depends
from mcp.server.auth.provider import TokenVerifier
from mcp.server.context import ServerMiddleware


class CentralAuthority(Protocol):
    """A central authority deciding every call of the service, over HTTP and over MCP."""

    @property
    def dependencies(self) -> Sequence[Depends]:
        """HTTP: the app-level dependencies, deciding each request after routing and before any handler.

        They set ``request.state.iam_principal`` (the caller) and raise a refusal as an ``HTTPException``.
        """
        ...

    @property
    def verifier(self) -> TokenVerifier:
        """MCP: admits a caller only with a credential the authority accepts."""
        ...

    @property
    def middleware(self) -> Sequence[ServerMiddleware[Any]]:
        """MCP: what runs around each tool call: the deployment's record of it, the authority's decision."""
        ...

    def caller(self) -> str | None:
        """MCP: the caller of the tool call being served, or ``None`` outside an admitted call."""
        ...
