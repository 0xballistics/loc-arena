"""Who may call the eval service: a central authority, or the identities and rights a deployment hands in.

The company package imports no platform package. A deployment under central IAM hands in an ``Authority``
(the engine's implementation), and every call is decided before its handler runs. Without one, ``Callers``
admits each caller by the identity it sends as ``Authorization: Bearer <identity>`` and holds its rights:
``read`` lists and inspects, ``run`` starts runs, ``curate`` registers and retires evals, ``publish`` puts a
run on a leaderboard. Rights never change while the service runs; a deployment sets them when it starts.
"""

from __future__ import annotations

import hmac
from collections.abc import Awaitable, Callable, Iterable, Mapping
from typing import Protocol

from fastapi import FastAPI, Request

READ, RUN, CURATE, PUBLISH = "read", "run", "curate", "publish"
RIGHTS = (READ, RUN, CURATE, PUBLISH)
BEARER_PREFIX = "Bearer "


class Authority(Protocol):
    """A central authority deciding every call of the service. Refusals are raised as ``HTTPException``."""

    dependency: Callable[[Request], Awaitable[None]]
    """The app-level dependency: it runs after routing and before any handler, setting ``request.state``
    ``iam_principal`` (the caller) and ``iam_operation`` (the operation the route is declared as)."""

    def verify_routes(self, app: FastAPI) -> None:
        """Refuse to start unless the routes of ``app`` and the declared operations agree exactly."""
        ...


class Callers:
    """Each caller's identity and the rights it holds, for a deployment without a central authority."""

    def __init__(
        self,
        identities_by_caller: Mapping[str, bytes],
        rights_by_caller: Mapping[str, Iterable[str]],
    ) -> None:
        """Refuse a right the service does not enforce: it could be held, never checked."""
        self._identities_by_caller = dict(identities_by_caller)
        self._rights_by_caller = {caller: frozenset(rights) for caller, rights in rights_by_caller.items()}
        held = set().union(*self._rights_by_caller.values()) if self._rights_by_caller else set()
        if unenforced := sorted(held - set(RIGHTS)):
            raise ValueError(f"rights the eval service does not enforce: {', '.join(unenforced)}")

    def identify(self, authorization: str) -> str | None:
        """The caller whose identity an ``Authorization`` header carries, or ``None``."""
        if not authorization.startswith(BEARER_PREFIX):
            return None
        sent = authorization.removeprefix(BEARER_PREFIX).encode()
        known = self._identities_by_caller.items()
        return next((caller for caller, identity in known if hmac.compare_digest(sent, identity)), None)

    def holds(self, caller: str, right: str) -> bool:
        """Whether ``caller`` holds ``right`` now."""
        return right in self._rights_by_caller.get(caller, frozenset())
