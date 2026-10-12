"""Access requests route to owners without conferring authority until an eligible decision."""

from datetime import UTC, datetime, timedelta
from http import HTTPStatus

import pytest
from access_requests import (  # ty: ignore[unresolved-import] - pytest includes the live service directory
    AccessPolicy,
    DecisionInput,
    RequestError,
    RequestInput,
    RequestQueue,
)
from pydantic import ValidationError

NOW = datetime(2026, 10, 11, 9, tzinfo=UTC)
UNTIL = NOW + timedelta(hours=1)


@pytest.fixture
def queue() -> RequestQueue:
    return RequestQueue(
        AccessPolicy(
            resource="notes",
            owner_team="research-platform",
            approvers=("owner",),
            max_requests=10,
            max_grant_seconds=7200,
            allow_ongoing=True,
        ),
        {"owner", "requester", "other"},
        {"read", "write", "grant"},
    )


def submit(queue: RequestQueue, caller: str = "requester") -> int:
    return queue.submit(
        caller,
        RequestInput(rights=("read", "write"), requested_until=UNTIL, reason="review experiment notes"),
        NOW,
    ).id


def test_submission_routes_to_owner_without_granting_access(queue: RequestQueue) -> None:
    request_id = submit(queue)

    assert queue.list_requests("owner")[0].owner_team == "research-platform"
    assert queue.list_requests("requester")[0].id == request_id
    assert queue.list_requests("other") == ()
    assert queue.effective_rights("requester", NOW) == set()


def test_owner_can_narrow_rights_and_lifetime(queue: RequestQueue) -> None:
    request_id = submit(queue)
    earlier = NOW + timedelta(minutes=30)

    decision = queue.decide(
        "owner",
        request_id,
        DecisionInput(
            decision="approve",
            rights=("read",),
            approved_until=earlier,
            reason="read is sufficient",
        ),
        {"read", "write", "grant"},
        False,
        NOW,
    )

    assert decision.approver == "owner"
    assert decision.approved_rights == ("read",)
    assert queue.effective_rights("requester", earlier - timedelta(seconds=1)) == {"read"}
    assert queue.effective_rights("requester", earlier) == set()


@pytest.mark.parametrize(
    ("caller", "subject", "rights", "decision", "status"),
    [
        ("other", "requester", {"read", "write", "grant"}, {"decision": "approve"}, HTTPStatus.FORBIDDEN),
        ("owner", "owner", {"read", "write", "grant"}, {"decision": "approve"}, HTTPStatus.FORBIDDEN),
        ("owner", "requester", {"grant"}, {"decision": "approve"}, HTTPStatus.FORBIDDEN),
        (
            "owner",
            "requester",
            {"read", "write", "grant"},
            {"decision": "approve", "rights": ("grant",)},
            HTTPStatus.BAD_REQUEST,
        ),
        (
            "owner",
            "requester",
            {"read", "write", "grant"},
            {"decision": "approve", "approved_until": UNTIL + timedelta(seconds=1)},
            HTTPStatus.BAD_REQUEST,
        ),
    ],
)
def test_invalid_decision_leaves_request_pending(
    queue: RequestQueue,
    caller: str,
    subject: str,
    rights: set[str],
    decision: dict[str, object],
    status: HTTPStatus,
) -> None:
    request_id = submit(queue, subject)

    with pytest.raises(RequestError) as error:
        queue.decide(
            caller,
            request_id,
            DecisionInput.model_validate({**decision, "reason": "decision"}),
            rights,
            False,
            NOW,
        )

    assert error.value.status == status
    assert queue.list_requests("owner")[0].status == "pending"
    assert queue.effective_rights(subject, NOW) == set()


def test_denial_is_terminal_and_confers_no_rights(queue: RequestQueue) -> None:
    request_id = submit(queue)
    denied = queue.decide(
        "owner",
        request_id,
        DecisionInput(decision="deny", reason="not required"),
        set(),
        False,
        NOW,
    )

    with pytest.raises(RequestError, match="already decided"):
        queue.decide(
            "owner",
            request_id,
            DecisionInput(decision="approve", reason="retry"),
            {"read", "write", "grant"},
            False,
            NOW,
        )

    assert denied.status == "denied"
    assert denied.decision_reason == "not required"
    assert queue.effective_rights("requester", NOW) == set()


def test_revocation_removes_rights_from_all_overlapping_approvals(queue: RequestQueue) -> None:
    for _ in range(2):
        request_id = submit(queue)
        queue.decide(
            "owner",
            request_id,
            DecisionInput(decision="approve", reason="needed"),
            {"read", "write", "grant"},
            False,
            NOW,
        )

    queue.revoke_rights("requester", {"read"})

    assert queue.effective_rights("requester", NOW) == {"write"}


@pytest.mark.parametrize("until", [NOW, NOW + timedelta(hours=3)])
def test_out_of_policy_lifetime_is_rejected(queue: RequestQueue, until: datetime) -> None:
    with pytest.raises(RequestError, match="lifetime"):
        queue.submit(
            "requester",
            RequestInput(rights=("read",), requested_until=until, reason="request"),
            NOW,
        )
    assert queue.list_requests("owner") == ()


def test_payload_cannot_supply_another_requester() -> None:
    with pytest.raises(ValidationError):
        RequestInput.model_validate(
            {"rights": ["read"], "requested_until": UNTIL.isoformat(), "reason": "x", "requester": "owner"},
        )
