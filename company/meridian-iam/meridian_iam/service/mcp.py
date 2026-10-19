"""IAM's one MCP tool, ``access_inbox``: what the tools gateway delivers to each agent at each turn's start.

The gateway calls it with the agent's ``iam-inbox`` credential, which reads the inbox and opens nothing
else, so the gateway never holds a credential that manages IAM. Asking for access and deciding stay on IAM's
HTTP API, where the agent acts with its own credential.
"""

from __future__ import annotations

from mcp.server import MCPServer
from mcp.server.auth.provider import AccessToken, TokenVerifier

from meridian_iam.core import Iam

INBOX_TOOL = "access_inbox"


class InboxVerifier(TokenVerifier):
    """Admits a bearer token IAM accepts for reading an inbox, as the principal it names."""

    def __init__(self, iam: Iam) -> None:
        """Verify against ``iam`` itself, in this process."""
        self._iam = iam

    async def verify_token(self, token: str) -> AccessToken | None:
        """The token's principal as the client, or ``None`` when IAM would refuse it the inbox.

        Async and on IAM's event loop, like the HTTP handlers: the core is not thread-safe.
        """
        raise NotImplementedError


def inbox_server(iam: Iam) -> MCPServer:
    """An MCP server serving exactly one tool, ``access_inbox``, verified by ``InboxVerifier``.

    The tool takes no argument: whose inbox it reads is the verified token's principal, never an argument. It
    answers the caller's ``Requests.inbox`` items as plain JSON objects, and writes no record of its own: a
    read changes nothing, and IAM's audit log already records every request and decision it shows.
    """
    raise NotImplementedError
