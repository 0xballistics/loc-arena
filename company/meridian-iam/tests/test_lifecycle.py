"""Credential renewal and the administration of teams and principals through IAM itself."""

from __future__ import annotations

import pytest
from iam_support import Clock

from meridian_iam.contracts import Conflict, Forbidden, Invalid, Unauthenticated
from meridian_iam.core import Iam
from meridian_iam.core.lifecycle import OPERATOR_ROLE, OPERATOR_TEAM


def allowed(iam: Iam, t: dict[str, str], who: str, op: str = "read_note") -> bool:
    return iam.authorize(t["svc"], t[who], op, "n").allow


def secret(n: int) -> str:
    """A replacement secret as a client generates it: 32+ URL-safe characters."""
    return f"mi_replacement_{n:03d}_" + "x" * 24


def test_renewal_replaces_the_credential_keeping_principal_grants_and_audience(
    iam: Iam,
    tokens: dict[str, str],
    clock: Clock,
) -> None:
    iam.add_baseline_grant("alice", "notes", ["read"])
    reply = iam.renew_credential(tokens["alice"], secret(1))
    assert reply == {"generation": 2, "previous_valid_until": clock.now + 60}  # IAM never returns the secret
    assert allowed(iam, {**tokens, "alice": secret(1)}, "alice")  # the grant followed the principal
    assert iam.identify(secret(1), "notes") == "alice" and iam.identify(secret(1), "iam") == "alice"
    with pytest.raises(Unauthenticated):
        iam.identify(secret(1), "other")  # the audiences are the old credential's, no more


@pytest.mark.parametrize("bad", ["", "short", "has spaces " * 4, "x" * 200, 12345])
def test_a_weak_or_malformed_replacement_is_refused(iam: Iam, tokens: dict[str, str], bad: object) -> None:
    with pytest.raises(Invalid):
        iam.renew_credential(tokens["alice"], bad)  # ty: ignore[invalid-argument-type]
    assert iam.identify(tokens["alice"], "iam") == "alice"  # nothing changed


def test_the_old_credential_works_through_the_grace_window_and_not_after(
    iam: Iam,
    tokens: dict[str, str],
    clock: Clock,
) -> None:
    iam.renew_credential(tokens["alice"], secret(1))
    clock.now += 59
    assert iam.identify(tokens["alice"], "iam") == "alice"
    clock.now += 1  # the exact end of the window
    with pytest.raises(Unauthenticated):
        iam.identify(tokens["alice"], "iam")


def test_a_lost_reply_costs_nothing_because_the_holder_chose_the_secret_and_can_repeat_the_renewal(
    iam: Iam,
    tokens: dict[str, str],
) -> None:
    first = iam.renew_credential(tokens["alice"], secret(1))
    assert iam.renew_credential(tokens["alice"], secret(1)) == first  # idempotent for the same replacement
    assert iam.identify(secret(1), "iam") == "alice"


def test_an_old_credential_cannot_replace_the_newer_one(iam: Iam, tokens: dict[str, str]) -> None:
    iam.renew_credential(tokens["alice"], secret(1))  # the legitimate agent renews and holds secret(1)
    with pytest.raises(Conflict):  # whoever kept the old token cannot name a different replacement...
        iam.renew_credential(tokens["alice"], secret(2))
    assert iam.identify(secret(1), "iam") == "alice"  # ...so the delivered one is intact
    with pytest.raises(Unauthenticated):
        iam.identify(secret(2), "iam")  # and the thief's secret was never installed
    iam.renew_credential(secret(1), secret(3))  # only the newest credential may renew
    with pytest.raises(Conflict):
        iam.renew_credential(secret(1), secret(4))


def test_a_repeated_renewal_reports_the_generation_the_first_one_created(
    iam: Iam,
    tokens: dict[str, str],
) -> None:
    iam.issue_credential(
        "alice",
        ["iam"],
    )  # a second credential under the same label: generation 2 already exists
    first = iam.renew_credential(tokens["alice"], secret(1))  # renewing generation 1 creates generation 3
    assert first["generation"] == 3
    assert (
        iam.renew_credential(tokens["alice"], secret(1)) == first
    )  # the repeat says the same, not "generation 2"


def test_a_secret_already_in_use_cannot_be_chosen(iam: Iam, tokens: dict[str, str]) -> None:
    with pytest.raises(Conflict):
        iam.renew_credential(
            tokens["alice"],
            tokens["bob"],
        )  # that would let two principals share a credential


def test_a_disabled_principal_cannot_renew_and_rights_do_not_widen(iam: Iam, tokens: dict[str, str]) -> None:
    only_notes = iam.issue_credential("bob", ["notes"])
    with pytest.raises(Unauthenticated):
        iam.renew_credential(only_notes, secret(1))  # renewal is an IAM call: it needs an IAM audience
    iam.disable_principal("alice")
    with pytest.raises(Unauthenticated):
        iam.renew_credential(tokens["alice"], secret(2))


def test_renewal_keeps_a_finite_lifetime_and_does_not_extend_the_old_one(iam: Iam, clock: Clock) -> None:
    start = clock.now
    short = iam.issue_credential("bob", ["iam"], ttl=300)
    clock.now += 100
    iam.renew_credential(short, secret(1))
    clock.now += 61  # past the grace: the old one is gone though its own lifetime had time left
    with pytest.raises(Unauthenticated):
        iam.identify(short, "iam")
    clock.now = (
        start + 100 + 300 - 1
    )  # the replacement lives its own 300 seconds from the renewal, to the second
    assert iam.identify(secret(1), "iam") == "bob"
    clock.now += 1
    with pytest.raises(Unauthenticated):
        iam.identify(secret(1), "iam")


def test_an_approver_adds_a_member_with_authority_it_holds_and_no_more(
    iam: Iam,
    tokens: dict[str, str],
) -> None:
    added = iam.add_team_member(tokens["lead"], "research", "alice", "lead", {"notes": ["read"]}, "k1")
    assert added["authority"] == {"notes": ["read"]}
    ask = iam.submit_request(tokens["bob"], "notes", ["read"], "r", 1500, "k2")["request"]
    iam.decide(tokens["alice"], ask, approve=True, key="d")  # alice now approves read, as delegated
    assert allowed(iam, tokens, "bob")
    ask2 = iam.submit_request(tokens["bob"], "notes", ["write"], "r", 1500, "k3")["request"]
    with pytest.raises(Forbidden):
        iam.decide(tokens["alice"], ask2, approve=True, key="d2")  # she was given read, not write
    with pytest.raises(Forbidden):  # nor can she hand on what she was not given
        iam.add_team_member(tokens["alice"], "research", "bob", "lead", {"notes": ["write"]}, "k4")


def test_adding_a_member_is_refused_for_non_admins_unknown_targets_and_foreign_resources(
    iam: Iam,
    tokens: dict[str, str],
) -> None:
    with pytest.raises(Forbidden):
        iam.add_team_member(tokens["alice"], "research", "bob", "lead", {}, "a")
    with pytest.raises(Forbidden):
        iam.add_team_member(tokens["lead"], "no-such-team", "bob", "lead", {}, "b")
    with pytest.raises(Invalid):
        iam.add_team_member(tokens["lead"], "research", "ghost", "lead", {}, "c")
    with pytest.raises(Invalid):
        iam.add_team_member(tokens["lead"], "research", "bob", "lead", {"wiki": ["read"]}, "d")
    with pytest.raises(Invalid):  # authority is for the approver role only
        iam.add_team_member(tokens["lead"], "research", "bob", "member", {"notes": ["read"]}, "e")
    iam.add_team_member(tokens["lead"], "research", "alice", "lead", {"notes": ["read"]}, "f")
    with pytest.raises(Conflict):
        iam.add_team_member(
            tokens["lead"],
            "research",
            "alice",
            "lead",
            {"notes": ["write"]},
            "f",
        )  # key reuse


def test_a_member_leaves_or_is_removed_within_the_removers_authority(
    iam: Iam,
    tokens: dict[str, str],
) -> None:
    iam.add_team_member(tokens["lead"], "research", "alice", "lead", {"notes": ["read", "write"]}, "k1")
    iam.add_team_member(tokens["lead"], "research", "bob", "lead", {"notes": ["read"]}, "k2")
    with pytest.raises(Forbidden):  # bob holds only read: he cannot remove someone holding write
        iam.remove_team_member(tokens["bob"], "research", "alice", "r1")
    iam.remove_team_member(tokens["alice"], "research", "alice", "r2")  # leaving is one's own
    ask = iam.submit_request(tokens["bob"], "notes", ["read"], "r", 1500, "q")["request"]
    with pytest.raises(Forbidden):
        iam.decide(tokens["alice"], ask, approve=True, key="d")  # her authority ended with her membership
    iam.remove_team_member(tokens["lead"], "research", "bob", "r3")
    with pytest.raises(Invalid):
        iam.remove_team_member(tokens["lead"], "research", "bob", "r4")  # already gone


def test_removal_keeps_grants_already_issued(iam: Iam, tokens: dict[str, str]) -> None:
    iam.add_team_member(tokens["lead"], "research", "alice", "lead", {"notes": ["read"]}, "k1")
    ask = iam.submit_request(tokens["bob"], "notes", ["read"], "r", 1500, "q")["request"]
    iam.decide(tokens["alice"], ask, approve=True, key="d")
    iam.remove_team_member(tokens["lead"], "research", "alice", "r")
    assert allowed(
        iam,
        tokens,
        "bob",
    )  # independent of the approver's later eligibility, until expiry or revoke


def test_only_an_operator_disables_a_principal_and_never_itself(iam: Iam, tokens: dict[str, str]) -> None:
    with pytest.raises(Forbidden):
        iam.disable_principal_as(tokens["lead"], "alice", "k")
    iam.add_member("lead", OPERATOR_TEAM, OPERATOR_ROLE)
    with pytest.raises(Invalid):
        iam.disable_principal_as(tokens["lead"], "lead", "k2")
    assert iam.disable_principal_as(tokens["lead"], "alice", "k3") == {"principal": "alice", "active": False}
    with pytest.raises(Unauthenticated):
        iam.identify(tokens["alice"], "iam")
    with pytest.raises(Invalid):
        iam.disable_principal_as(tokens["lead"], "ghost", "k4")


def test_lifecycle_changes_are_audited_without_credentials(iam: Iam, tokens: dict[str, str]) -> None:
    iam.renew_credential(tokens["alice"], secret(1))
    iam.add_team_member(tokens["lead"], "research", "bob", "lead", {"notes": ["read"]}, "k")
    log = str(iam.audit_log())
    assert "credential_renewed" in log and "member_added" in log
    assert secret(1) not in log and tokens["alice"] not in log


def test_a_weaker_owner_cannot_strip_a_stronger_owners_authority(iam: Iam, tokens: dict[str, str]) -> None:
    iam.add_team_member(tokens["lead"], "research", "alice", "lead", {"notes": ["read", "write"]}, "k1")
    iam.add_team_member(tokens["lead"], "research", "bob", "lead", {"notes": ["read"]}, "k2")
    with pytest.raises(Invalid):  # re-adding with nothing is not a way to narrow someone
        iam.add_team_member(tokens["bob"], "research", "alice", "lead", {"notes": []}, "k3")
    again = iam.add_team_member(tokens["bob"], "research", "alice", "lead", {"notes": ["read"]}, "k4")
    assert again["authority"] == {"notes": ["read", "write"]}  # additive: write is still hers
    with pytest.raises(Forbidden):  # and bob cannot remove someone holding more than he does
        iam.remove_team_member(tokens["bob"], "research", "alice", "k5")
    ask = iam.submit_request(tokens["lead"], "notes", ["write"], "r", 1500, "q")["request"]
    iam.decide(tokens["alice"], ask, approve=True, key="d")  # alice still approves write


def test_the_operator_team_is_reserved_even_when_a_resource_is_configured_to_own_it(
    iam: Iam,
    tokens: dict[str, str],
) -> None:
    import json  # noqa: PLC0415

    from iam_support import NOTES  # noqa: PLC0415

    from meridian_iam.contracts import load_manifest  # noqa: PLC0415

    other = json.loads(json.dumps(NOTES))
    other["resource"] = "wiki"
    with pytest.raises(Invalid):  # a resource cannot be owned by the operator team at all...
        iam.register_resource(
            load_manifest(other),
            owner_team=OPERATOR_TEAM,
            approver_role="lead",
            max_seconds=100,
        )
    iam.add_member(
        "lead",
        OPERATOR_TEAM,
        "lead",
        {"notes": ["read"]},
    )  # ...but a bootstrap could still seed one
    with pytest.raises(Forbidden):  # no delegated owner can add or remove an operator
        iam.add_team_member(tokens["lead"], OPERATOR_TEAM, "bob", OPERATOR_ROLE, {}, "k")
    with pytest.raises(Forbidden):
        iam.remove_team_member(tokens["lead"], OPERATOR_TEAM, "lead", "k2")
    with pytest.raises(Forbidden):
        iam.disable_principal_as(tokens["bob"], "alice", "k3")
