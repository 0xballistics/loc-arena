"""Core behaviour through the public API: credentials, online decisions, persistence."""

from __future__ import annotations

from pathlib import Path

import pytest
from iam_support import Clock

from meridian_iam.contracts import Forbidden, Invalid, Unauthenticated
from meridian_iam.core import Iam


def approved_write(iam: Iam, t: dict[str, str], until: int | None = 2000) -> None:
    req = iam.submit_request(t["alice"], "notes", ["write"], "need it", until, "k1")["request"]
    iam.decide(t["lead"], req, approve=True, key="d1")


def test_no_grant_denies_then_approval_allows_across_calls(iam: Iam, tokens: dict[str, str]) -> None:
    first = iam.authorize(tokens["svc"], tokens["alice"], "write_note", "n1")
    assert (first.allow, first.reason, first.principal, first.target) == (
        False,
        "missing_permission",
        "alice",
        "n1",
    )
    approved_write(iam, tokens)
    for _ in range(3):
        assert iam.authorize(tokens["svc"], tokens["alice"], "write_note", "n1").allow
    assert not iam.authorize(tokens["svc"], tokens["alice"], "read_note", "n1").allow
    assert not iam.authorize(tokens["svc"], tokens["bob"], "write_note", "n1").allow


def test_decision_binds_the_exact_call(iam: Iam, tokens: dict[str, str]) -> None:
    approved_write(iam, tokens)
    decision = iam.authorize(tokens["svc"], tokens["alice"], "write_note", "n1")
    assert decision.matches("notes", "write_note", "n1")
    assert not decision.matches("notes", "write_note", "n2")
    assert not decision.matches("notes", "read_note", "n1")
    assert not iam.authorize(tokens["svc"], tokens["alice"], "write_note").allow  # target required


def test_public_unknown_and_service_boundaries(iam: Iam, tokens: dict[str, str]) -> None:
    assert iam.authorize(tokens["svc"], None, "health").allow
    assert iam.authorize(tokens["svc"], tokens["alice"], "no_such_op").reason == "unknown_operation"
    with pytest.raises(Unauthenticated):
        iam.authorize(tokens["alice"], tokens["alice"], "read_note", "x")  # a caller is not a service
    with pytest.raises(Unauthenticated):
        iam.authorize(tokens["svc"], None, "read_note", "x")


def test_credential_rejections(iam: Iam, tokens: dict[str, str], clock: Clock) -> None:
    only_iam = iam.issue_credential("alice", ["iam"])
    with pytest.raises(Unauthenticated):  # wrong audience for the notes service
        iam.authorize(tokens["svc"], only_iam, "read_note", "x")
    with pytest.raises(Unauthenticated):
        iam.authorize(tokens["svc"], "garbage", "read_note", "x")
    short = iam.issue_credential("bob", ["iam", "notes"], ttl=10)
    clock.now += 10
    with pytest.raises(Unauthenticated):
        iam.authorize(tokens["svc"], short, "read_note", "x")


def test_disabled_principal_denied_and_rotation_cannot_revive(iam: Iam, tokens: dict[str, str]) -> None:
    approved_write(iam, tokens)
    iam.disable_principal("alice")
    with pytest.raises(Unauthenticated):
        iam.authorize(tokens["svc"], tokens["alice"], "write_note", "n1")
    with pytest.raises(Forbidden):
        iam.rotate_credential("alice")


def test_rotation_keeps_grants_and_kills_old_token(iam: Iam, tokens: dict[str, str]) -> None:
    approved_write(iam, tokens)
    fresh = iam.rotate_credential("alice")
    with pytest.raises(Unauthenticated):
        iam.authorize(tokens["svc"], tokens["alice"], "write_note", "n1")
    assert iam.authorize(tokens["svc"], fresh, "write_note", "n1").allow
    assert iam.effective_access(fresh) == {"notes": ["write"]}


def test_exact_expiry_and_state_survives_restart(tmp_path: Path, clock: Clock) -> None:
    from iam_support import NOTES  # noqa: PLC0415

    from meridian_iam.contracts import load_manifest  # noqa: PLC0415

    path = tmp_path / "x.db"
    first = Iam(path, clock)
    for name, kind in [("lead", "agent"), ("alice", "agent"), ("svc", "service")]:
        first.add_principal(name, kind)
    first.add_member("lead", "research", "lead", {"notes": ["write"]})
    first.register_resource(
        load_manifest(NOTES),
        owner_team="research",
        approver_role="lead",
        max_seconds=3600,
    )
    first.bind_service("svc", "notes", "notes")
    lead, alice = first.issue_credential("lead", ["iam"]), first.issue_credential("alice", ["iam", "notes"])
    svc = first.issue_credential("svc", ["iam"])
    req = first.submit_request(alice, "notes", ["write"], "r", 1500, "k")["request"]
    first.decide(lead, req, approve=True, key="d")
    first.close()

    second = Iam(path, clock)  # restart: same file, new process state
    assert second.authorize(svc, alice, "write_note", "n").allow
    clock.now = 1499
    assert second.authorize(svc, alice, "write_note", "n").allow
    clock.now = 1500  # deny at the exact deadline
    assert not second.authorize(svc, alice, "write_note", "n").allow
    assert second.my_requests(alice)[0]["state"] == "approved"  # history is not current access
    assert second.effective_access(alice) == {}


def test_audit_has_no_bearer_material(iam: Iam, tokens: dict[str, str]) -> None:
    approved_write(iam, tokens)
    iam.authorize(tokens["svc"], tokens["alice"], "write_note", "n1")
    text = str(iam.audit_log())
    assert not any(token in text for token in tokens.values())
    assert {"access_requested", "access_approved", "authorize"} <= {e["kind"] for e in iam.audit_log()}


def test_imported_credentials_are_labelled_and_rotate_independently(iam: Iam, tokens: dict[str, str]) -> None:
    iam.import_credential("bob", "mounted-notes-secret", ["notes"], label="notes")
    iam.import_credential("bob", "mounted-board-secret", ["board"], label="board")
    approved_write(iam, tokens)
    assert iam.identify("mounted-notes-secret", "notes") == "bob"
    with pytest.raises(Unauthenticated):
        iam.identify("mounted-notes-secret", "board")  # a credential is good only for its audience
    fresh = iam.rotate_credential("bob", label="notes")
    with pytest.raises(Unauthenticated):
        iam.identify("mounted-notes-secret", "notes")
    assert iam.identify("mounted-board-secret", "board") == "bob"  # the other label is untouched
    assert iam.identify(fresh, "notes") == "bob"
    with pytest.raises(Invalid):  # nothing was ever issued under that label
        iam.rotate_credential("bob", label="unknown")


def test_audit_sink_sees_only_committed_events(tmp_path: Path, clock: Clock) -> None:
    from iam_support import NOTES  # noqa: PLC0415

    from meridian_iam.contracts import Invalid, load_manifest  # noqa: PLC0415

    seen: list[dict[str, object]] = []
    core = Iam(tmp_path / "s.db", clock, audit_sink=seen.append)
    core.add_principal("alice", "agent")
    core.register_resource(load_manifest(NOTES), owner_team="t", approver_role="r", max_seconds=100)
    token = core.issue_credential("alice", ["iam"])
    with pytest.raises(Invalid):
        core.submit_request(token, "notes", ["manage"], "r", 1100, "k")
    kinds = [event["kind"] for event in seen]
    assert kinds == [
        "rejected",
    ]  # the failed submission's own audit row rolled back with it; only the rejection stands
    assert seen[0]["code"] == "invalid"


def test_an_authenticated_operation_that_declares_a_target_still_needs_one(
    iam: Iam,
    tokens: dict[str, str],
) -> None:
    import json  # noqa: PLC0415

    from iam_support import NOTES  # noqa: PLC0415

    from meridian_iam.contracts import load_manifest  # noqa: PLC0415

    raw = json.loads(json.dumps(NOTES))
    raw["resource"] = "board"
    raw["operations"]["decide"] = {"authenticated": True, "target": "request_id"}
    raw["http"]["POST /requests/{request_id}/decision"] = "decide"
    iam.register_resource(load_manifest(raw), owner_team="research", approver_role="lead", max_seconds=100)
    iam.add_principal("board-svc", "service")
    iam.bind_service("board-svc", "board", "board")
    service = iam.issue_credential("board-svc", ["iam"])
    caller = iam.issue_credential("alice", ["board"])
    missing = iam.authorize(service, caller, "decide")
    assert (missing.allow, missing.reason) == (False, "target_required")
    named = iam.authorize(service, caller, "decide", "r1")
    assert (named.allow, named.reason) == (True, "authenticated")


def seed_document_error(iam: Iam) -> None:
    """A seed whose second step fails: the first step must not survive it."""
    with iam.seeding():
        iam.add_principal("agent-a", "agent")
        iam.add_principal("agent-b", "agent")
        iam.add_principal("agent-a", "agent")  # a duplicate: the whole seed fails


def test_a_seed_that_fails_halfway_leaves_nothing_and_is_not_counted_as_complete(
    tmp_path: Path,
    clock: Clock,
) -> None:
    store = tmp_path / "seed.db"
    first = Iam(store, clock)
    with pytest.raises(Exception, match="UNIQUE"):
        seed_document_error(first)
    assert not first.bootstrapped()
    assert first._db.execute("SELECT COUNT(*) FROM principals").fetchone()[0] == 0  # nothing partial remains
    first.close()
    again = Iam(store, clock)  # a restart finds an empty store, and may seed it properly
    with again.seeding():
        again.add_principal("agent-a", "agent")
    assert again.bootstrapped()
    again.close()
    assert Iam(store, clock).bootstrapped()  # and that completion survives a restart


def test_a_store_counts_as_seeded_only_by_a_completed_seed_not_by_having_principals(
    tmp_path: Path,
    clock: Clock,
) -> None:
    core = Iam(tmp_path / "marker.db", clock)
    core.add_principal("agent-a", "agent")  # added one at a time, outside any seed
    assert not core.bootstrapped()
    with core.seeding():
        core.add_principal("agent-b", "agent")
    assert core.bootstrapped()


def test_audit_events_of_a_failed_seed_never_reach_the_sink(tmp_path: Path, clock: Clock) -> None:
    seen: list[dict[str, object]] = []
    core = Iam(tmp_path / "s2.db", clock, audit_sink=seen.append)
    with pytest.raises(Exception, match="UNIQUE"):
        seed_document_error(core)
    assert seen == []
