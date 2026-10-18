"""The notes service under central IAM: a real IAM server and a real notes server over real SQLite."""

import json
import socket
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from http import HTTPStatus
from pathlib import Path

import pytest
import uvicorn
from notes_iam import (  # ty: ignore[unresolved-import] - live path
    ROUTES,
    IamBinding,
    make_iam_handler,
    parse_settings,
)
from notes_service import NotesServer, Settings  # ty: ignore[unresolved-import] - live service pythonpath

from meridian_iam.client import IamClient
from meridian_iam.contracts import ManifestError, load_manifest
from meridian_iam.core import Iam
from meridian_iam.integration import Guard
from meridian_iam.service import create_app
from tests.unit._iam_waits import wait_until_refused

MANIFEST = Path(__file__).parents[2] / "scenarios/aurora_efficiency/services/notes/iam-manifest.json"
START = int(datetime(2026, 10, 13, 12, 0, tzinfo=UTC).timestamp())
MAIN, ALICE, BOB = "agent-main", "alice", "bob"


class Clock:
    """A settable clock in integer seconds."""

    def __init__(self) -> None:
        """Start at a fixed instant."""
        self.now = START

    def __call__(self) -> int:
        return self.now


@dataclass
class World:
    url: str
    iam: Iam
    clock: Clock
    tokens: dict[str, str]
    server: uvicorn.Server


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture
def world(tmp_path: Path) -> Iterator[World]:
    clock = Clock()
    iam = Iam(tmp_path / "iam.db", clock)
    for name in (MAIN, ALICE, BOB):
        iam.add_principal(name, "agent")
    iam.add_principal("notes", "service")
    iam.add_member(MAIN, "research-platform", "approver", {"notes": ["read", "write"]})
    manifest = load_manifest(json.loads(MANIFEST.read_text()))
    iam.register_resource(
        manifest,
        owner_team="research-platform",
        approver_role="approver",
        max_seconds=86_400,
        allow_ongoing=True,
    )
    iam.bind_service("notes", "notes", "notes")
    tokens = {name: iam.issue_credential(name, ["notes", "iam:notes"]) for name in (MAIN, ALICE, BOB)}
    tokens["notes"] = iam.issue_credential("notes", ["iam"])
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(create_app(iam), port=port, log_level="error", access_log=False))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.01)
    yield World(f"http://127.0.0.1:{port}", iam, clock, tokens, server)
    server.should_exit = True


@pytest.fixture
def notes(world: World) -> Iterator[str]:
    client = IamClient(world.url, world.tokens["notes"], timeout=1, retries=0)
    manifest = load_manifest(json.loads(MANIFEST.read_text()))
    settings = Settings(
        0,
        {},
        ("read", "write", "grant"),
        {},
        False,
        False,
        10,
        1024,
        1.0,
    )
    binding = IamBinding(Guard(manifest, client), client, "notes")
    server = NotesServer(settings, handler=make_iam_handler(binding))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()


def call(
    url: str,
    method: str = "GET",
    *,
    token: str | None = None,
    body: object = None,
) -> tuple[int, bytes]:
    data = None if body is None else json.dumps(body).encode()
    headers = {} if token is None else {"Authorization": f"Bearer {token}"}
    try:
        with urllib.request.urlopen(
            urllib.request.Request(url, data, headers, method=method),
            timeout=5,
        ) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def iso(world: World, seconds: int) -> str:
    return datetime.fromtimestamp(world.clock.now + seconds, UTC).isoformat()


def request_write(
    notes: str,
    world: World,
    who: str = ALICE,
    rights: tuple[str, ...] = ("write",),
    ttl: int | None = 600,
) -> dict:
    body = {
        "rights": rights,
        "requested_until": None if ttl is None else iso(world, ttl),
        "reason": "need it",
    }
    status, reply = call(f"{notes}/requests", "POST", token=world.tokens[who], body=body)
    assert status == HTTPStatus.CREATED, reply
    return json.loads(reply)


def decide(notes: str, world: World, request: dict, **body: object) -> tuple[int, dict]:
    payload = {"decision": "approve", "reason": "ok", **body}
    status, reply = call(
        f"{notes}/requests/{request['id']}/decision",
        "POST",
        token=world.tokens[MAIN],
        body=payload,
    )
    return status, json.loads(reply)


def test_public_routes_need_no_credential(notes: str) -> None:
    assert call(f"{notes}/health")[0] == HTTPStatus.OK
    assert call(f"{notes}/")[0] == HTTPStatus.OK


def test_the_description_says_how_to_ask_for_access_and_decide_through_iam(notes: str) -> None:
    _, description = call(f"{notes}/")

    for route in ("POST /requests", "GET /requests", "POST /requests/<id>/decision", "IAM service"):
        assert route in description.decode()


def test_unknown_identity_is_401_and_missing_right_is_403_with_guidance(notes: str, world: World) -> None:
    assert call(f"{notes}/notes", token="junk")[0] == HTTPStatus.UNAUTHORIZED
    assert call(f"{notes}/notes")[0] == HTTPStatus.UNAUTHORIZED
    status, reply = call(f"{notes}/notes/a", "PUT", token=world.tokens[ALICE], body="x")
    assert status == HTTPStatus.FORBIDDEN
    assert json.loads(reply) == {
        "error": "forbidden",
        "resource": "notes",
        "permission": "write",
        "request_with": "POST /requests on the IAM service",
    }


def test_request_approval_enforced_until_the_exact_expiry(notes: str, world: World) -> None:
    pending = request_write(notes, world)
    assert pending["status"] == "pending"
    assert call(f"{notes}/notes/a", "PUT", token=world.tokens[ALICE], body="x")[0] == HTTPStatus.FORBIDDEN
    assert decide(notes, world, pending)[1]["status"] == "approved"
    for _ in range(2):  # access persists across calls
        assert call(f"{notes}/notes/a", "PUT", token=world.tokens[ALICE], body="x")[0] in (200, 201)
    assert call(f"{notes}/notes/a", token=world.tokens[ALICE])[0] == HTTPStatus.FORBIDDEN  # read not approved
    world.clock.now += 599
    assert call(f"{notes}/notes/a", "PUT", token=world.tokens[ALICE], body="y")[0] == HTTPStatus.OK
    world.clock.now += 1  # now == approved_until
    assert call(f"{notes}/notes/a", "PUT", token=world.tokens[ALICE], body="z")[0] == HTTPStatus.FORBIDDEN


def test_only_an_approver_decides_and_never_their_own_request(notes: str, world: World) -> None:
    pending = request_write(notes, world)
    stranger = call(
        f"{notes}/requests/{pending['id']}/decision",
        "POST",
        token=world.tokens[BOB],
        body={"decision": "approve", "reason": "x"},
    )
    assert stranger[0] == HTTPStatus.FORBIDDEN
    own = request_write(notes, world, who=MAIN)
    assert decide(notes, world, own)[0] == HTTPStatus.FORBIDDEN
    assert call(f"{notes}/notes/a", "PUT", token=world.tokens[BOB], body="x")[0] == HTTPStatus.FORBIDDEN


def test_denial_and_queue_visibility(notes: str, world: World) -> None:
    pending = request_write(notes, world)
    status, reply = call(f"{notes}/requests", token=world.tokens[BOB])
    assert (status, json.loads(reply)) == (HTTPStatus.OK, [])  # not the requester, not an owner
    assert [r["id"] for r in json.loads(call(f"{notes}/requests", token=world.tokens[MAIN])[1])] == [
        pending["id"],
    ]
    assert decide(notes, world, pending, decision="deny")[1]["status"] == "denied"
    assert call(f"{notes}/notes/a", "PUT", token=world.tokens[ALICE], body="x")[0] == HTTPStatus.FORBIDDEN


def test_approval_cannot_widen_the_request(notes: str, world: World) -> None:
    pending = request_write(notes, world)
    assert decide(notes, world, pending, rights=["write", "read"])[0] == HTTPStatus.BAD_REQUEST
    assert decide(notes, world, pending, approved_until=iso(world, 700))[0] == HTTPStatus.BAD_REQUEST


def test_direct_grant_list_and_overlapping_revoke(notes: str, world: World) -> None:
    approved = request_write(notes, world, rights=("read", "write"))
    decide(notes, world, approved)
    body = {"agent": ALICE, "rights": ["write"]}
    assert call(f"{notes}/grants", "POST", token=world.tokens[ALICE], body=body)[0] == HTTPStatus.FORBIDDEN
    status, reply = call(f"{notes}/grants", "POST", token=world.tokens[MAIN], body=body)
    assert (status, json.loads(reply)) == (HTTPStatus.OK, ["read", "write"])
    assert json.loads(call(f"{notes}/grants", token=world.tokens[BOB])[1]) == {ALICE: ["read", "write"]}
    status, reply = call(f"{notes}/grants", "DELETE", token=world.tokens[MAIN], body=body)
    assert (status, json.loads(reply)) == (HTTPStatus.OK, ["read"])
    assert call(f"{notes}/notes/a", "PUT", token=world.tokens[ALICE], body="x")[0] == HTTPStatus.FORBIDDEN
    assert call(f"{notes}/notes", token=world.tokens[ALICE])[0] == HTTPStatus.OK


def test_iam_outage_is_503_and_changes_no_note(notes: str, world: World) -> None:
    decide(notes, world, request_write(notes, world))
    assert call(f"{notes}/notes/a", "PUT", token=world.tokens[ALICE], body="x")[0] == HTTPStatus.CREATED
    world.server.should_exit = True
    wait_until_refused(world.url)
    assert (
        call(f"{notes}/notes/a", "PUT", token=world.tokens[ALICE], body="changed")[0]
        == HTTPStatus.SERVICE_UNAVAILABLE
    )
    assert call(f"{notes}/notes/a", "DELETE", token=world.tokens[ALICE])[0] == HTTPStatus.SERVICE_UNAVAILABLE
    assert call(f"{notes}/requests", token=world.tokens[ALICE])[0] == HTTPStatus.SERVICE_UNAVAILABLE


def test_no_local_rights_table_exists_in_iam_mode(notes: str, world: World) -> None:
    # Rights given before the notes service saw the caller still apply; none are kept in the notes process.
    world.iam.grant(world.tokens[MAIN], BOB, "notes", ["read"], None, "seed")
    assert call(f"{notes}/notes", token=world.tokens[BOB])[0] == HTTPStatus.OK
    world.iam.revoke(world.tokens[MAIN], BOB, "notes", ["read"], "rev")
    assert call(f"{notes}/notes", token=world.tokens[BOB])[0] == HTTPStatus.FORBIDDEN


def test_notes_starts_only_if_the_manifest_declares_exactly_the_routes_it_implements(tmp_path: Path) -> None:
    declared = json.loads(MANIFEST.read_text())
    assert set(declared["http"]) == set(ROUTES)
    credential = tmp_path / "credential"
    credential.write_text("service-credential")

    def start(manifest: dict[str, object]) -> None:
        path = tmp_path / "manifest.json"
        path.write_text(json.dumps(manifest))
        arguments = [
            "--iam-url",
            "http://iam:8000",
            "--iam-credential",
            str(credential),
            "--iam-manifest",
            str(path),
        ]
        parse_settings(arguments)

    start(declared)  # exact: fine
    dropped_route = "DELETE /notes/{key}"
    without = {**declared, "http": {k: v for k, v in declared["http"].items() if k != dropped_route}}
    without["operations"] = {k: v for k, v in declared["operations"].items() if k != "delete_note"}
    with pytest.raises(ManifestError, match="undeclared route DELETE /notes/{key}"):
        start(without)  # the handler would still serve it, so the service does not start
    extra = {**declared, "http": {**declared["http"], "PATCH /notes/{key}": "write_note"}}
    with pytest.raises(ManifestError, match="declared but unregistered route PATCH"):
        start(extra)


def test_a_request_older_than_the_queue_limit_is_still_decided_and_returned(notes: str, world: World) -> None:
    oldest = request_write(notes, world)  # stays pending while 110 newer requests come and go
    for n in range(110):
        newer = world.iam.submit_request(world.tokens[ALICE], "notes", ["read"], f"r{n}", None, f"s{n}")[
            "request"
        ]
        world.iam.decide(world.tokens[MAIN], newer, approve=False, key=f"d{n}")
    assert oldest["id"] not in [
        r["request"] for r in world.iam.owner_queue(world.tokens[MAIN], "notes")
    ]  # cut off
    status, record = decide(notes, world, oldest)
    assert (status, record["status"], record["id"]) == (HTTPStatus.OK, "approved", oldest["id"])
    assert (record["reason"], record["decision_reason"]) == ("need it", "ok")


def test_the_record_carries_why_access_was_asked_and_why_it_was_decided(notes: str, world: World) -> None:
    pending = request_write(notes, world)
    assert pending["reason"] == "need it"
    decided = decide(notes, world, pending, reason="fine for this week")[1]
    assert (decided["reason"], decided["decision_reason"]) == ("need it", "fine for this week")
