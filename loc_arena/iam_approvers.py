"""Each service's approver at work: it decides the access requests on its service as the scenario says.

An approver is a principal of the company that the scenario names for one service
(``iam_seed.ApproverSeed``), never an agent under test. It reads its own IAM inbox and decides each pending
request on its services through IAM's ordinary decision, so IAM's audit names it: it approves a request when
it approves every permission asked, and denies the rest, saying what it approves. It runs in the IAM service,
on the event loop that serves IAM, so it never touches the store while a request is being answered.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from loc_arena.iam_bootstrap import IAM_AUDIENCE
from loc_arena.iam_seed import ApproverSeed
from meridian_iam.contracts import IamError, InboxItem
from meridian_iam.core import Iam
from meridian_iam.core.requests import MAX_REASON


@dataclass(frozen=True)
class Approver:
    """One approver: its principal, the credential it decides with, and what it approves on each service."""

    principal: str
    token: str
    approves: Mapping[str, frozenset[str]]  # by service: the permissions it approves there


def issue_approvers(iam: Iam, document: Mapping[str, Any]) -> list[Approver]:
    """The approvers of the bootstrap ``document``, each with a credential for this process alone."""
    approves: dict[str, dict[str, frozenset[str]]] = {}
    for name, resource in document["resources"].items():
        approver = ApproverSeed.model_validate(resource["approver"])
        approves.setdefault(approver.principal, {})[name] = frozenset(approver.approves)
    return [
        Approver(principal, iam.issue_credential(principal, [IAM_AUDIENCE], label="approver"), held)
        for principal, held in sorted(approves.items())
    ]


def decide_pending(iam: Iam, approver: Approver) -> None:
    """Decide each pending request on ``approver``'s services: approve what it approves, deny the rest.

    One request that fails to be decided is logged and left for the next reading; it never stops the others.
    """
    for item in iam.inbox(approver.token):
        if item.kind != "decide" or item.resource not in approver.approves:
            continue  # another service of the same owner team: its own approver decides
        try:
            _decide(iam, approver, item)
        except Exception as error:  # noqa: BLE001 - one request's failure must not stop the others
            _log(approver, f"request {item.request}: {error!r}")


def _decide(iam: Iam, approver: Approver, item: InboxItem) -> None:
    """Approve ``item`` if its approver approves each permission asked, else deny it, saying what it approves.

    A request it approves but IAM will not let it approve (its requester disabled meanwhile, say) is denied
    with IAM's reason, so it waits for no decision that cannot come. Reasons are cut to IAM's bound.
    """
    allowed = approver.approves[item.resource]
    key = f"{approver.principal}-{item.request}"
    if set(item.permissions) <= allowed:
        try:
            reason = f"{approver.principal} approves it on {item.resource}"
            iam.decide(approver.token, item.request, approve=True, key=key, reason=reason[:MAX_REASON])
            return
        except IamError as error:
            reason, key = f"{approver.principal} could not approve it: {error}", f"{key}-denied"
    else:
        reason = (
            f"{approver.principal} approves only {', '.join(sorted(allowed)) or 'nothing'} on {item.resource}"
        )
    iam.decide(approver.token, item.request, approve=False, key=key, reason=reason[:MAX_REASON])


async def decide_forever(iam: Iam, approvers: Sequence[Approver], poll_seconds: float) -> None:
    """Every ``poll_seconds``, let each approver decide what is pending; a failure is logged, never fatal."""
    while True:
        for approver in approvers:
            try:
                decide_pending(iam, approver)
            except Exception as error:  # noqa: BLE001 - the approvers must outlive any one failed reading
                _log(approver, repr(error))
        await asyncio.sleep(poll_seconds)


def _log(approver: Approver, error: str) -> None:
    print(json.dumps({"approver": approver.principal, "error": error}), flush=True)
