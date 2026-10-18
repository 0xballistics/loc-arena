"""The access inbox: an approver sees what it may decide, a requester sees what was decided, nobody else's."""

from __future__ import annotations

from collections.abc import Callable
from http import HTTPStatus
from typing import Any

import pytest
from fastapi.testclient import TestClient
from iam_support import Clock

from meridian_iam.contracts import InboxItem, Unauthenticated
from meridian_iam.core import Iam
from meridian_iam.service import create_app


@pytest.fixture
def mailbox(iam: Iam) -> dict[str, str]:
    """Each agent's inbox key: the ``iam-inbox`` audience alone."""
    return {
        name: iam.issue_credential(name, ["iam-inbox"], label="inbox") for name in ("lead", "alice", "bob")
    }


def ask(iam: Iam, token: str, permissions: list[str], key: str) -> str:
    return iam.submit_request(token, "notes", permissions, "because", 2000, key)["request"]


def test_an_approver_sees_each_pending_request_it_may_decide_never_its_own(
    iam: Iam,
    tokens: dict[str, str],
    mailbox: dict[str, str],
) -> None:
    alices = ask(iam, tokens["alice"], ["write"], "a")
    ask(iam, tokens["lead"], ["read"], "l")

    inbox = iam.inbox(mailbox["lead"])

    assert inbox == [
        InboxItem(
            kind="decide",
            request=alices,
            requester="alice",
            resource="notes",
            permissions=("write",),
            until=2000,
            reason="because",
            state="pending",
            approver=None,
            approved=None,
            approved_until=None,
            decision_reason=None,
        ),
    ]


def test_a_requester_hears_the_outcome_once_its_request_is_decided(
    iam: Iam,
    tokens: dict[str, str],
    mailbox: dict[str, str],
) -> None:
    request = ask(iam, tokens["alice"], ["write"], "a")
    pending = iam.inbox(mailbox["alice"])
    iam.decide(tokens["lead"], request, approve=True, key="d", reason="ok for the sprint")

    decided = iam.inbox(mailbox["alice"])

    assert pending == []
    assert decided == [
        InboxItem(
            kind="outcome",
            request=request,
            requester="alice",
            resource="notes",
            permissions=("write",),
            until=2000,
            reason="because",
            state="approved",
            approver="lead",
            approved=("write",),
            approved_until=2000,
            decision_reason="ok for the sprint",
        ),
    ]
    assert iam.inbox(mailbox["lead"]) == []  # decided, so nothing left for the approver


def test_a_denial_reaches_the_requester_with_nothing_approved(
    iam: Iam,
    tokens: dict[str, str],
    mailbox: dict[str, str],
) -> None:
    request = ask(iam, tokens["alice"], ["write"], "a")
    iam.decide(tokens["lead"], request, approve=False, key="d", reason="not this sprint")

    (outcome,) = iam.inbox(mailbox["alice"])

    assert (outcome.state, outcome.approved, outcome.decision_reason) == ("denied", None, "not this sprint")


def test_an_approver_sees_the_longest_waiting_request_first(
    iam: Iam,
    tokens: dict[str, str],
    mailbox: dict[str, str],
    clock: Clock,
) -> None:
    first = ask(iam, tokens["alice"], ["write"], "a")
    clock.now += 10
    second = ask(iam, tokens["bob"], ["write"], "b")

    assert [item.request for item in iam.inbox(mailbox["lead"])] == [first, second]


def test_a_requester_sees_its_latest_decision_first_whenever_it_asked(
    iam: Iam,
    tokens: dict[str, str],
    mailbox: dict[str, str],
    clock: Clock,
) -> None:
    early = ask(iam, tokens["alice"], ["write"], "a")
    clock.now += 10
    late = ask(iam, tokens["alice"], ["read"], "b")
    iam.decide(tokens["lead"], late, approve=True, key="d1", reason="ok")
    clock.now += 10
    iam.decide(tokens["lead"], early, approve=True, key="d2", reason="ok")

    assert [item.request for item in iam.inbox(mailbox["alice"])] == [early, late]


def test_someone_who_may_not_decide_sees_nobody_elses_request(
    iam: Iam,
    tokens: dict[str, str],
    mailbox: dict[str, str],
) -> None:
    ask(iam, tokens["alice"], ["write"], "a")

    assert iam.inbox(mailbox["bob"]) == []


AUTHORITY = {"notes": ["read", "write"]}


@pytest.mark.parametrize(
    "membership",
    [
        lambda iam: iam.add_member("bob", "research", "viewer", AUTHORITY),
        lambda iam: iam.add_member("bob", "other-team", "lead", AUTHORITY),
        lambda iam: (
            iam.add_member("bob", "research", "lead", AUTHORITY),
            iam.remove_member("bob", "research"),
        ),
    ],
    ids=["another role", "another team", "no longer a member"],
)
def test_only_an_active_approver_of_the_owner_team_is_asked_to_decide(
    iam: Iam,
    tokens: dict[str, str],
    mailbox: dict[str, str],
    membership: Callable[[Iam], Any],
) -> None:
    membership(iam)
    ask(iam, tokens["alice"], ["write"], "a")

    assert iam.inbox(mailbox["bob"]) == []


@pytest.mark.parametrize(
    "call",
    [
        lambda iam, key: iam.submit_request(key, "notes", ["write"], "because", 2000, "k"),
        lambda iam, key: iam.my_requests(key),
        lambda iam, key: iam.owner_queue(key, "notes"),
        lambda iam, key: iam.renew_credential(key, "mi_a_replacement_secret_of_enough_length"),
        lambda iam, key: iam.effective_access(key),
    ],
)
def test_the_inbox_key_opens_the_inbox_and_nothing_else(
    iam: Iam,
    mailbox: dict[str, str],
    call: Callable[[Iam, str], Any],
) -> None:
    with pytest.raises(Unauthenticated):
        call(iam, mailbox["alice"])


@pytest.mark.parametrize("audiences", [["notes", "iam:notes"], ["notes", "iam:notes", "iam-inbox"]])
def test_a_credential_bound_to_one_service_cannot_read_the_inbox(iam: Iam, audiences: list[str]) -> None:
    scoped = iam.issue_credential("alice", audiences, label="scoped")

    with pytest.raises(Unauthenticated):
        iam.inbox(scoped)


def test_reading_the_inbox_writes_no_audit_line(
    iam: Iam,
    tokens: dict[str, str],
    mailbox: dict[str, str],
) -> None:
    ask(iam, tokens["alice"], ["write"], "a")
    before = len(iam.audit_log())

    iam.inbox(mailbox["lead"])

    assert len(iam.audit_log()) == before


def test_the_inbox_is_served_over_http(iam: Iam, tokens: dict[str, str], mailbox: dict[str, str]) -> None:
    request = ask(iam, tokens["alice"], ["write"], "a")

    reply = TestClient(create_app(iam)).get("/inbox", headers={"Authorization": f"Bearer {mailbox['lead']}"})

    assert reply.status_code == HTTPStatus.OK
    assert [(item["kind"], item["request"]) for item in reply.json()] == [("decide", request)]
