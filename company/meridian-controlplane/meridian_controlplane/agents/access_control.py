"""Who may call the agents service: each caller's identity, and the rights each caller holds now.

A caller sends its identity as ``Authorization: Bearer <identity>``. ``read`` lists and reads agents and their
executions, ``register`` registers, updates and retires agents, ``run`` starts and cancels executions, and
``grant`` gives another caller rights the granter holds, or takes them away. ``grant`` itself can be given
only when the deployment makes it grantable.
"""

from __future__ import annotations

import hmac
from collections.abc import Iterable, Mapping
from http import HTTPStatus

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict

READ, REGISTER, RUN, GRANT = "read", "register", "run", "grant"
ENFORCED_RIGHTS = (READ, REGISTER, RUN, GRANT)
BEARER_PREFIX = "Bearer "


class RightsChange(BaseModel):
    """Rights to give a caller, or to take away from it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    agent: str
    rights: list[str]


class AccessControl:
    """Each caller's identity and current rights. A refused change raises ``HTTPException``."""

    def __init__(
        self,
        identities_by_caller: Mapping[str, bytes],
        declared_rights: Iterable[str],
        starting_rights_by_caller: Mapping[str, Iterable[str]],
        grant_right_is_grantable: bool,
    ) -> None:
        """Refuse a declared right the service does not enforce: it could be granted, never checked."""
        self.declared_rights = tuple(declared_rights)
        if unenforced := sorted(set(self.declared_rights) - set(ENFORCED_RIGHTS)):
            raise ValueError(f"declared rights the service does not enforce: {', '.join(unenforced)}")
        self.grant_right_is_grantable = grant_right_is_grantable
        self._identities_by_caller = dict(identities_by_caller)
        self._rights_by_caller = {
            caller: set(starting_rights_by_caller.get(caller, ())) for caller in identities_by_caller
        }

    def identify_caller(self, authorization_header: str) -> str | None:
        """The caller whose identity the header carries, or ``None`` for no known identity."""
        if not authorization_header.startswith(BEARER_PREFIX):
            return None
        sent_identity = authorization_header.removeprefix(BEARER_PREFIX).encode()
        return next(
            (
                caller
                for caller, identity in self._identities_by_caller.items()
                if hmac.compare_digest(sent_identity, identity)
            ),
            None,
        )

    def holds_right(self, caller: str, right: str) -> bool:
        """Whether ``caller`` holds ``right`` now."""
        return right in self._rights_by_caller[caller]

    def list_rights_by_caller(self) -> dict[str, list[str]]:
        """Every caller's current rights."""
        return {caller: sorted(rights) for caller, rights in sorted(self._rights_by_caller.items())}

    def grant_rights(self, granter: str, change: RightsChange) -> list[str]:
        """Give ``change.agent`` the rights ``change`` names; returns the rights it holds now."""
        self._check_change_is_allowed(granter, change)
        self._rights_by_caller[change.agent].update(change.rights)
        return sorted(self._rights_by_caller[change.agent])

    def revoke_rights(self, revoker: str, change: RightsChange) -> list[str]:
        """Take the rights ``change`` names from ``change.agent``; returns the rights it holds now."""
        self._check_change_is_allowed(revoker, change)
        self._rights_by_caller[change.agent].difference_update(change.rights)
        return sorted(self._rights_by_caller[change.agent])

    def _check_change_is_allowed(self, changed_by: str, change: RightsChange) -> None:
        """404: no such caller; 400: an undeclared right; 403: grant, or a right ``changed_by`` lacks."""
        if change.agent not in self._rights_by_caller:
            raise HTTPException(HTTPStatus.NOT_FOUND, f"no caller named {change.agent!r}")
        if undeclared := sorted(set(change.rights) - set(self.declared_rights)):
            raise HTTPException(HTTPStatus.BAD_REQUEST, f"no such right: {', '.join(undeclared)}")
        if GRANT in change.rights and not self.grant_right_is_grantable:
            raise HTTPException(HTTPStatus.FORBIDDEN, "the grant right cannot be given or taken here")
        if not set(change.rights) <= self._rights_by_caller[changed_by]:
            raise HTTPException(HTTPStatus.FORBIDDEN, "you can only give or take rights you hold")
