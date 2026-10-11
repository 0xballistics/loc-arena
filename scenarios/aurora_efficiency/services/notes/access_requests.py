"""Owner-routed access requests; the service supplies authenticated callers and its clock."""

from __future__ import annotations

from datetime import datetime, timedelta
from http import HTTPStatus
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr

Text = Annotated[StrictStr, Field(min_length=1, pattern=r"\S")]
Rights = Annotated[tuple[Text, ...], Field(min_length=1)]


class Record(BaseModel):
    """Validated immutable records; reject fields that could impersonate the authenticated caller."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class AccessPolicy(Record):
    """The service's resource owner and request limits, loaded from its scenario fixture."""

    resource: Text
    owner_team: Text
    approvers: Annotated[tuple[Text, ...], Field(min_length=1)]
    max_requests: Annotated[StrictInt, Field(gt=0)]
    max_grant_seconds: Annotated[StrictInt, Field(gt=0)]
    allow_ongoing: StrictBool


class RequestInput(Record):
    """Rights and lifetime requested by the authenticated agent, not a caller-supplied subject."""

    rights: Rights
    requested_until: AwareDatetime | None
    reason: Text


class DecisionInput(Record):
    """An owner's decision can narrow a request, but cannot expand it."""

    decision: Literal["approve", "deny"]
    reason: Text
    rights: Rights | None = None
    approved_until: AwareDatetime | None = None


class AccessRequest(Record):
    """A request with owner routing, decision provenance and its effective approved grant."""

    id: int
    requester: str
    resource: str
    owner_team: str
    requested_rights: tuple[str, ...]
    requested_until: AwareDatetime | None
    reason: str
    created_at: AwareDatetime
    status: Literal["pending", "approved", "denied", "revoked"] = "pending"
    approver: str | None = None
    decided_at: AwareDatetime | None = None
    decision_reason: str | None = None
    approved_rights: tuple[str, ...] = ()
    approved_until: AwareDatetime | None = None


class RequestError(ValueError):
    """A rejected workflow operation with an HTTP status, without mutating the grant."""

    def __init__(self, status: HTTPStatus, message: str) -> None:
        """Expose a bounded reason to the caller and audit log."""
        super().__init__(message)
        self.status = status


class RequestQueue:
    """Service-owned requests; callers serialize operations under their service's rights lock."""

    def __init__(self, policy: AccessPolicy, agents: set[str], vocabulary: set[str]) -> None:
        """Route only to known approvers; grant enforcement stays in the live service."""
        if not set(policy.approvers) <= agents:
            raise ValueError("access policy names an unknown approver")
        self.policy = policy
        self._agents = agents
        self._vocabulary = vocabulary
        self._requests: dict[int, AccessRequest] = {}

    def submit(self, caller: str, request: RequestInput, now: datetime) -> AccessRequest:
        """Create a pending request; submitting it confers no rights."""
        self._known(caller)
        if not set(request.rights) <= self._vocabulary:
            raise RequestError(HTTPStatus.BAD_REQUEST, "unknown requested right")
        self._lifetime(request.requested_until, now)
        if len(self._requests) >= self.policy.max_requests:
            raise RequestError(HTTPStatus.CONFLICT, "request queue is full")
        record = AccessRequest(
            id=len(self._requests) + 1,
            requester=caller,
            resource=self.policy.resource,
            owner_team=self.policy.owner_team,
            requested_rights=tuple(sorted(set(request.rights))),
            requested_until=request.requested_until,
            reason=request.reason,
            created_at=now,
        )
        self._requests[record.id] = record
        return record

    def list_requests(self, caller: str) -> tuple[AccessRequest, ...]:
        """Owners see their queue; requesters see their own requests."""
        self._known(caller)
        return tuple(
            request
            for request in self._requests.values()
            if caller in self.policy.approvers or request.requester == caller
        )

    def decide(
        self,
        caller: str,
        request_id: int,
        decision: DecisionInput,
        held_rights: set[str],
        transitive: bool,
        now: datetime,
    ) -> AccessRequest:
        """An eligible owner approves within its actual authority or records a denial."""
        self._known(caller)
        if caller not in self.policy.approvers:
            raise RequestError(HTTPStatus.FORBIDDEN, "caller is not a resource-owner approver")
        request = self._requests.get(request_id)
        if request is None:
            raise RequestError(HTTPStatus.NOT_FOUND, "unknown access request")
        if request.requester == caller:
            raise RequestError(HTTPStatus.FORBIDDEN, "requesters cannot decide their own requests")
        if request.status != "pending":
            raise RequestError(HTTPStatus.CONFLICT, "request already decided")
        rights: tuple[str, ...] = ()
        until = None
        if decision.decision == "approve":
            rights = tuple(sorted(set(decision.rights or request.requested_rights)))
            if not set(rights) <= set(request.requested_rights):
                raise RequestError(HTTPStatus.BAD_REQUEST, "decision expands requested rights")
            if not {"grant", *rights} <= held_rights or ("grant" in rights and not transitive):
                raise RequestError(HTTPStatus.FORBIDDEN, "approver lacks authority to grant these rights")
            until = decision.approved_until or request.requested_until
            self._lifetime(until, now)
            if request.requested_until is not None and (until is None or until > request.requested_until):
                raise RequestError(HTTPStatus.BAD_REQUEST, "decision extends requested lifetime")
        elif decision.rights is not None or decision.approved_until is not None:
            raise RequestError(HTTPStatus.BAD_REQUEST, "denial cannot include a grant")
        result = request.model_copy(
            update={
                "status": "approved" if decision.decision == "approve" else "denied",
                "approver": caller,
                "decided_at": now,
                "decision_reason": decision.reason,
                "approved_rights": rights,
                "approved_until": until,
            },
        )
        self._requests[request_id] = result
        return result

    def effective_rights(self, caller: str, now: datetime) -> set[str]:
        """Union active approved grants; expiry removes access at the exact boundary."""
        return {
            right
            for request in self._requests.values()
            if request.requester == caller
            and request.status == "approved"
            and (request.approved_until is None or now < request.approved_until)
            for right in request.approved_rights
        }

    def revoke_rights(self, caller: str, rights: set[str]) -> None:
        """Remove revoked rights from every approval, so another request cannot restore them."""
        for request in tuple(self._requests.values()):
            if request.requester == caller and request.status == "approved":
                remaining = tuple(right for right in request.approved_rights if right not in rights)
                self._requests[request.id] = request.model_copy(
                    update={"approved_rights": remaining, "status": "approved" if remaining else "revoked"},
                )

    def _known(self, caller: str) -> None:
        if caller not in self._agents:
            raise RequestError(HTTPStatus.UNAUTHORIZED, "unknown agent")

    def _lifetime(self, until: datetime | None, now: datetime) -> None:
        if until is None:
            if not self.policy.allow_ongoing:
                raise RequestError(HTTPStatus.BAD_REQUEST, "ongoing access is not allowed")
        elif not now < until <= now + timedelta(seconds=self.policy.max_grant_seconds):
            raise RequestError(HTTPStatus.BAD_REQUEST, "grant lifetime is outside resource policy")
