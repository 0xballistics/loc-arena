"""Who may call a command server: the one trusted caller, holding the server's token.

The sandbox's command server and a node's daemon each serve one caller alone (the episode, the agents
service, the job service) that holds the server's token. Both admit the same way: the request comes from an
address the trusted caller resolves to, and carries the token. The checks are dependencies so a framework
page can never answer around them.
"""

from __future__ import annotations

import hmac
import socket
from http import HTTPStatus
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import Field, SecretStr

ALL_INTERFACES = "0.0.0.0"  # noqa: S104 - agent-net is its only network
MINIMUM_TOKEN_LENGTH = 32
# A token a trusted caller presents: an empty or short one is refused wherever it is read.
ServerToken = Annotated[SecretStr, Field(min_length=MINIMUM_TOKEN_LENGTH)]
# At module level: the route's annotations are resolved in this module's namespace.
_BEARER = HTTPBearer(auto_error=False)


def resolve_addresses(host: str) -> set[str]:
    """The addresses ``host`` resolves to now; none when it does not resolve."""
    try:
        return {str(address[4][0]) for address in socket.getaddrinfo(host, None)}
    except socket.gaierror:
        return set()


def admit_trusted_caller(trusted_caller: str, server_role: str):  # noqa: ANN201 - a FastAPI dependency
    """A dependency that refuses every caller but ``trusted_caller``, whatever token it holds."""

    def admit(request: Request) -> None:
        # Looked up on each request: the caller's address is known only once it is up.
        if request.client is None or request.client.host not in resolve_addresses(trusted_caller):
            raise HTTPException(
                HTTPStatus.FORBIDDEN,
                f"only the {trusted_caller} service may call this {server_role}",
            )

    return admit


def authenticate_token(token: SecretStr):  # noqa: ANN201 - a FastAPI dependency
    """A dependency that refuses a caller not holding ``token``."""

    def authenticate(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_BEARER)]) -> None:
        presented = credentials.credentials.encode() if credentials else b""
        if not hmac.compare_digest(presented, token.get_secret_value().encode()):
            raise HTTPException(HTTPStatus.UNAUTHORIZED, "a server token is required")

    return authenticate
