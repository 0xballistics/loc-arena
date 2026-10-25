"""What the agents service needs of a central authorization service, without importing one.

The company package stays free of any platform dependency: a deployment hands in an implementation of this
protocol (central IAM's, in Meridian's deployment). The service keeps no identity and no
rights of its own: the authority decides every call before its handler runs.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol

from fastapi import FastAPI, Request
from pydantic import BaseModel, ConfigDict


class RightsChange(BaseModel):
    """Rights to give a caller, or to take away from it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    agent: str
    rights: list[str]


class CentralAuthority(Protocol):
    """A central authority deciding every call of the service. Refusals are raised as ``HTTPException``."""

    dependency: Callable[[Request], Awaitable[None]]
    """The app-level dependency: it runs after routing and before any handler, setting ``request.state``
    ``iam_principal`` (the caller) and ``iam_operation`` (the operation the route is declared as)."""

    def verify_routes(self, app: FastAPI) -> None:
        """Refuse to start unless the routes of ``app`` and the declared operations agree exactly."""
        ...

    async def list_rights(self, token: str) -> dict[str, list[str]]:
        """Every caller's rights on this service."""
        ...

    async def change_rights(self, token: str, change: RightsChange, *, granting: bool) -> list[str]:
        """Give or take ``change.rights`` of ``change.agent`` as ``token``'s holder; the rights held now."""
        ...
