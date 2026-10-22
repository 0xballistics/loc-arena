"""A stand-in for central IAM's credential check in the MCP service tests: each agent's token admits it."""

import hmac
from collections.abc import Mapping

from mcp.server.auth.provider import AccessToken, TokenVerifier


class AgentTokens(TokenVerifier):
    """Admits a bearer token that is a test agent's, as that agent; ``test_mcp_iam`` tests the real check."""

    def __init__(self, tokens: Mapping[str, bytes]) -> None:
        """Hold every test agent's token, by agent id."""
        self._tokens = dict(tokens)

    async def verify_token(self, token: str) -> AccessToken | None:
        """The agent whose token ``token`` is, as the token's client, or ``None``."""
        sent = token.encode()
        agent = next((agent for agent, held in self._tokens.items() if hmac.compare_digest(sent, held)), None)
        return None if agent is None else AccessToken(token=token, client_id=agent, scopes=[])
