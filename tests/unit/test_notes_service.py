"""The notes service: identities, rights, and bounds."""

import json
import socket
import threading
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from pathlib import Path

import pytest
from access_requests import AccessPolicy  # ty: ignore[unresolved-import] - live service pythonpath
from notes_service import (  # ty: ignore[unresolved-import] - found through pytest's pythonpath, not ty's
    NotesServer,
    Settings,
    parse_settings,
    read_identities,
)

EPHEMERAL_PORT = 0
MAX_NOTES = 2
MAX_NOTE_BYTES = 128
OVER_BOUND_EXTRA = 1
TIMEOUT = 0.2
CLIENT_TIMEOUT = 5.0
EXIT_CODE_ERROR = 2
ONE_BYTE = 1
ENFORCED_RIGHTS = ("read", "write", "grant")
VOCABULARY_VARIABLE = "LOC_ARENA_VOCABULARY"

READER, WRITER, GRANTER, DELEGATE, NOBODY = "reader", "writer", "granter", "delegate", "nobody"
READER_TOKEN, WRITER_TOKEN, GRANTER_TOKEN = "reader-token", "writer-token", "granter-token"
DELEGATE_TOKEN, NOBODY_TOKEN, UNKNOWN_TOKEN = "delegate-token", "nobody-token", "unknown-token"
IDENTITIES = {READER: READER_TOKEN, WRITER: WRITER_TOKEN, GRANTER: GRANTER_TOKEN}
IDENTITIES |= {DELEGATE: DELEGATE_TOKEN, NOBODY: NOBODY_TOKEN}


def _serve(
    tmp_path: Path,
    *,
    transitive: bool = False,
    is_open: bool = False,
    vocabulary: tuple[str, ...] = ENFORCED_RIGHTS,
    access_policy: AccessPolicy | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> Iterator[str]:
    for agent, identity in IDENTITIES.items():
        (tmp_path / f"identity_{agent}").write_text(identity)
    identities = read_identities(tmp_path)
    reads = {"read"}
    rights = {READER: reads, WRITER: reads | {"write"}, GRANTER: reads | {"write", "grant"}}
    rights |= {DELEGATE: reads | {"grant"}, NOBODY: set()}
    settings = Settings(
        EPHEMERAL_PORT,
        identities,
        vocabulary,
        rights,
        transitive,
        is_open,
        MAX_NOTES,
        MAX_NOTE_BYTES if access_policy is None else 1024,
        TIMEOUT,
        access_policy,
    )
    server = NotesServer(settings, clock=clock)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()


@pytest.fixture
def service(tmp_path: Path) -> Iterator[str]:
    yield from _serve(tmp_path)


@pytest.fixture
def transitive_service(tmp_path: Path) -> Iterator[str]:
    yield from _serve(tmp_path, transitive=True)


@pytest.fixture
def read_grant_service(tmp_path: Path) -> Iterator[str]:
    yield from _serve(tmp_path, vocabulary=("read", "grant"))


@pytest.fixture
def open_service(tmp_path: Path) -> Iterator[str]:
    yield from _serve(tmp_path, is_open=True)


def _call(
    url: str,
    method: str = "GET",
    *,
    token: str | None = None,
    body: bytes | None = None,
) -> HTTPStatus:
    headers = {} if token is None else {"Authorization": f"Bearer {token}"}
    try:
        with urllib.request.urlopen(urllib.request.Request(url, body, headers, method=method)) as reply:
            return HTTPStatus(reply.status)
    except urllib.error.HTTPError as error:
        return HTTPStatus(error.code)


def _read(url: str, *, token: str | None = None) -> bytes:
    headers = {} if token is None else {"Authorization": f"Bearer {token}"}
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers)) as reply:
        return reply.read()


def test_healthcheck_needs_no_identity(service: str) -> None:
    target = f"{service}/health"

    status = _call(target, token=None)

    assert status == HTTPStatus.OK


def test_root_description_needs_no_identity(service: str) -> None:
    target = f"{service}/"

    body = _read(target, token=None)

    assert b"Rights: read, write, grant" in body


def test_root_description_lists_the_declared_vocabulary(read_grant_service: str) -> None:
    target = f"{read_grant_service}/"

    body = _read(target, token=None)

    assert b"Rights: read, grant." in body


def test_unknown_identity_is_refused(service: str) -> None:
    target = f"{service}/notes"

    status = _call(target, token=UNKNOWN_TOKEN)

    assert status == HTTPStatus.UNAUTHORIZED


def test_reader_is_served_on_notes(service: str) -> None:
    target = f"{service}/notes"

    status = _call(target, token=READER_TOKEN)

    assert status == HTTPStatus.OK


def test_identity_without_rights_is_forbidden(service: str) -> None:
    target = f"{service}/notes"

    status = _call(target, token=NOBODY_TOKEN)

    assert status == HTTPStatus.FORBIDDEN


def test_reader_gets_note_written_by_writer(service: str) -> None:
    target = f"{service}/notes/plan"
    _call(target, "PUT", token=WRITER_TOKEN, body=b"hello")

    body = _read(target, token=READER_TOKEN)

    assert body == b"hello"


def test_reader_without_write_is_forbidden_on_put(service: str) -> None:
    target = f"{service}/notes/plan"

    status = _call(target, "PUT", token=READER_TOKEN, body=b"hello")

    assert status == HTTPStatus.FORBIDDEN


def test_grants_endpoint_lists_rights(service: str) -> None:
    target = f"{service}/grants"

    grants = json.loads(_read(target, token=READER_TOKEN))

    assert grants[READER] == ["read"]


def test_grantee_reads_after_grant(service: str) -> None:
    payload = json.dumps({"agent": NOBODY, "rights": ["read"]}).encode()
    _call(f"{service}/grants", "POST", token=GRANTER_TOKEN, body=payload)

    status = _call(f"{service}/notes", token=NOBODY_TOKEN)

    assert status == HTTPStatus.OK


def test_non_granter_grant_is_forbidden(service: str) -> None:
    payload = json.dumps({"agent": NOBODY, "rights": ["read"]}).encode()

    status = _call(f"{service}/grants", "POST", token=WRITER_TOKEN, body=payload)

    assert status == HTTPStatus.FORBIDDEN


def test_granting_grant_when_intransitive_is_forbidden(service: str) -> None:
    payload = json.dumps({"agent": NOBODY, "rights": ["grant"]}).encode()

    status = _call(f"{service}/grants", "POST", token=GRANTER_TOKEN, body=payload)

    assert status == HTTPStatus.FORBIDDEN


def test_granting_grant_when_transitive_is_served(transitive_service: str) -> None:
    payload = json.dumps({"agent": NOBODY, "rights": ["grant"]}).encode()

    status = _call(f"{transitive_service}/grants", "POST", token=GRANTER_TOKEN, body=payload)

    assert status == HTTPStatus.OK


def test_granting_right_granter_lacks_is_forbidden(service: str) -> None:
    payload = json.dumps({"agent": NOBODY, "rights": ["write"]}).encode()

    status = _call(f"{service}/grants", "POST", token=DELEGATE_TOKEN, body=payload)

    assert status == HTTPStatus.FORBIDDEN


def test_granting_to_unknown_agent_is_not_found(service: str) -> None:
    payload = json.dumps({"agent": "ghost", "rights": ["read"]}).encode()

    status = _call(f"{service}/grants", "POST", token=GRANTER_TOKEN, body=payload)

    assert status == HTTPStatus.NOT_FOUND


def test_granting_unknown_right_is_bad_request(service: str) -> None:
    payload = json.dumps({"agent": NOBODY, "rights": ["admin"]}).encode()

    status = _call(f"{service}/grants", "POST", token=GRANTER_TOKEN, body=payload)

    assert status == HTTPStatus.BAD_REQUEST


def test_granting_right_outside_the_declared_vocabulary_is_bad_request(read_grant_service: str) -> None:
    payload = json.dumps({"agent": NOBODY, "rights": ["write"]}).encode()

    status = _call(f"{read_grant_service}/grants", "POST", token=GRANTER_TOKEN, body=payload)

    assert status == HTTPStatus.BAD_REQUEST


def test_revoke_removes_right(service: str) -> None:
    payload = json.dumps({"agent": READER, "rights": ["read"]}).encode()
    _call(f"{service}/grants", "DELETE", token=GRANTER_TOKEN, body=payload)

    status = _call(f"{service}/notes", token=READER_TOKEN)

    assert status == HTTPStatus.FORBIDDEN


def test_grant_log_records_event_details(service: str, capsys: pytest.CaptureFixture[str]) -> None:
    payload = json.dumps({"agent": NOBODY, "rights": ["read"]}).encode()

    _call(f"{service}/grants", "POST", token=GRANTER_TOKEN, body=payload)

    out = capsys.readouterr().out
    event = next(json.loads(line) for line in out.splitlines() if '"event": "grant"' in line)
    assert event["granter"] == GRANTER and event["agent"] == NOBODY and event["rights"] == ["read"]
    assert "container" in event


def test_refuses_to_start_with_no_identity(tmp_path: Path) -> None:
    args = ["--secrets-dir", str(tmp_path)]

    with pytest.raises(SystemExit) as exit_info:
        parse_settings(args)

    assert exit_info.value.code == EXIT_CODE_ERROR


def test_declared_vocabulary_is_read_from_the_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "identity_a").write_text("token")
    monkeypatch.setenv(VOCABULARY_VARIABLE, '["read", "grant"]')

    settings = parse_settings(["--secrets-dir", str(tmp_path)])

    assert settings.vocabulary == ("read", "grant")


def test_undeclared_vocabulary_is_every_right_the_service_enforces(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(VOCABULARY_VARIABLE, raising=False)

    settings = parse_settings(["--secrets-dir", str(tmp_path), "--open"])

    assert settings.vocabulary == ("read", "write", "grant")


def test_refuses_to_start_with_a_right_it_cannot_enforce(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "identity_a").write_text("token")
    monkeypatch.setenv(VOCABULARY_VARIABLE, '["read", "admin"]')

    with pytest.raises(SystemExit) as exit_info:
        parse_settings(["--secrets-dir", str(tmp_path)])

    assert exit_info.value.code == EXIT_CODE_ERROR


def test_open_service_serves_caller_without_identity(open_service: str) -> None:
    target = f"{open_service}/notes"

    status = _call(target, token=None)

    assert status == HTTPStatus.OK


def test_list_holds_keys_written(service: str) -> None:
    _call(f"{service}/notes/b", "PUT", token=WRITER_TOKEN, body=b"2")
    _call(f"{service}/notes/a", "PUT", token=WRITER_TOKEN, body=b"1")

    keys = _read(f"{service}/notes", token=READER_TOKEN)

    assert keys == b'["a", "b"]'


def test_note_over_size_bound_is_refused(service: str) -> None:
    target = f"{service}/notes/big"
    body = b"x" * (MAX_NOTE_BYTES + OVER_BOUND_EXTRA)

    status = _call(target, "PUT", token=WRITER_TOKEN, body=body)

    assert status == HTTPStatus.REQUEST_ENTITY_TOO_LARGE


def test_note_past_count_bound_is_refused(service: str) -> None:
    for key in range(MAX_NOTES):
        _call(f"{service}/notes/n{key}", "PUT", token=WRITER_TOKEN, body=b"x")

    status = _call(f"{service}/notes/extra", "PUT", token=WRITER_TOKEN, body=b"x")

    assert status == HTTPStatus.INSUFFICIENT_STORAGE


def test_deleted_note_is_gone(service: str) -> None:
    _call(f"{service}/notes/plan", "PUT", token=WRITER_TOKEN, body=b"x")
    _call(f"{service}/notes/plan", "DELETE", token=WRITER_TOKEN)

    status = _call(f"{service}/notes/plan", token=READER_TOKEN)

    assert status == HTTPStatus.NOT_FOUND


@pytest.mark.parametrize("method", ["PUT", "DELETE"])
def test_writing_or_deleting_the_list_of_notes_is_not_allowed(service: str, method: str) -> None:
    target = f"{service}/notes"

    status = _call(target, method, token=WRITER_TOKEN, body=b"x")

    assert status == HTTPStatus.METHOD_NOT_ALLOWED


def test_key_outside_allowed_characters_is_not_a_note(service: str) -> None:
    target = f"{service}/notes/a%20b"

    status = _call(target, "PUT", token=WRITER_TOKEN, body=b"x")

    assert status == HTTPStatus.NOT_FOUND


def test_service_accepts_each_identity_mounted_by_its_name(tmp_path: Path) -> None:
    (tmp_path / "identity_reader").write_text(f"{READER_TOKEN}\n")
    (tmp_path / "sandbox_token").write_text("not an identity")

    identities = read_identities(tmp_path)

    assert identities == {READER: READER_TOKEN.encode()}


def test_length_not_ascii_digits_is_refused(service: str) -> None:
    req = urllib.request.Request(
        f"{service}/notes/plan",
        b"x",
        {"Authorization": f"Bearer {WRITER_TOKEN}", "Content-Length": "\u00b2"},
        method="PUT",
    )

    with pytest.raises(urllib.error.HTTPError) as refusal:
        urllib.request.urlopen(req)

    assert refusal.value.code == HTTPStatus.LENGTH_REQUIRED


def test_caller_that_stalls_mid_body_loses_connection(service: str) -> None:
    host, port = service.removeprefix("http://").split(":")
    hdr = f"PUT /notes/plan HTTP/1.0\r\nAuthorization: Bearer {WRITER_TOKEN}\r\nContent-Length: 16\r\n\r\n"
    with socket.create_connection((host, int(port)), timeout=CLIENT_TIMEOUT) as stalled:
        stalled.sendall(f"{hdr}abc".encode())

        reply = stalled.recv(ONE_BYTE)

    assert reply == b""


@dataclass
class AccessService:
    url: str = ""
    now: datetime = datetime(2026, 10, 11, 9, tzinfo=UTC)

    def clock(self) -> datetime:
        return self.now


@pytest.fixture
def access_service(tmp_path: Path) -> Iterator[AccessService]:
    service = AccessService()
    policy = AccessPolicy(
        resource="notes",
        owner_team="research-platform",
        approvers=(GRANTER,),
        max_requests=10,
        max_grant_seconds=7200,
        allow_ongoing=True,
    )
    for url in _serve(tmp_path, access_policy=policy, clock=service.clock):
        service.url = url
        yield service


def request_access(service: AccessService, *, token: str = NOBODY_TOKEN, right: str = "read") -> int:
    payload = json.dumps(
        {
            "rights": [right],
            "requested_until": (service.now + timedelta(hours=1)).isoformat(),
            "reason": "review notes",
        },
    ).encode()
    request = urllib.request.Request(
        f"{service.url}/requests",
        data=payload,
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(request) as reply:
        record = json.load(reply)
    return record["id"]


def test_request_approval_enforces_access_until_exact_expiry(access_service: AccessService) -> None:
    request_id = request_access(access_service)
    before = _call(f"{access_service.url}/notes", token=NOBODY_TOKEN)
    approved = _call(
        f"{access_service.url}/requests/{request_id}/decision",
        "POST",
        token=GRANTER_TOKEN,
        body=b'{"decision":"approve","reason":"needed"}',
    )
    after = _call(f"{access_service.url}/notes", token=NOBODY_TOKEN)
    access_service.now += timedelta(hours=1)
    expired = _call(f"{access_service.url}/notes", token=NOBODY_TOKEN)

    assert (before, approved, after, expired) == (
        HTTPStatus.FORBIDDEN,
        HTTPStatus.OK,
        HTTPStatus.OK,
        HTTPStatus.FORBIDDEN,
    )
    assert json.loads(_read(f"{access_service.url}/grants", token=GRANTER_TOKEN))[NOBODY] == []


@pytest.mark.parametrize("token", [NOBODY_TOKEN, WRITER_TOKEN])
def test_non_owner_decision_cannot_change_request_rights(access_service: AccessService, token: str) -> None:
    request_id = request_access(access_service)

    status = _call(
        f"{access_service.url}/requests/{request_id}/decision",
        "POST",
        token=token,
        body=b'{"decision":"approve","reason":"needed"}',
    )

    assert status == HTTPStatus.FORBIDDEN
    assert _call(f"{access_service.url}/notes", token=NOBODY_TOKEN) == HTTPStatus.FORBIDDEN


def test_denial_and_owner_queue_are_visible_without_granting_access(access_service: AccessService) -> None:
    request_id = request_access(access_service)
    denied = _call(
        f"{access_service.url}/requests/{request_id}/decision",
        "POST",
        token=GRANTER_TOKEN,
        body=b'{"decision":"deny","reason":"not justified"}',
    )

    requester = json.loads(_read(f"{access_service.url}/requests", token=NOBODY_TOKEN))
    owner = json.loads(_read(f"{access_service.url}/requests", token=GRANTER_TOKEN))

    assert denied == HTTPStatus.OK
    assert requester == owner
    assert requester[0]["status"] == "denied"
    assert requester[0]["decision_reason"] == "not justified"
    assert json.loads(_read(f"{access_service.url}/requests", token=WRITER_TOKEN)) == []
    assert _call(f"{access_service.url}/notes", token=NOBODY_TOKEN) == HTTPStatus.FORBIDDEN


def test_direct_revoke_removes_overlapping_approved_requests(access_service: AccessService) -> None:
    for _ in range(2):
        request_id = request_access(access_service)
        _call(
            f"{access_service.url}/requests/{request_id}/decision",
            "POST",
            token=GRANTER_TOKEN,
            body=b'{"decision":"approve","reason":"needed"}',
        )
    _call(
        f"{access_service.url}/grants",
        "DELETE",
        token=GRANTER_TOKEN,
        body=json.dumps({"agent": NOBODY, "rights": ["read"]}).encode(),
    )

    assert _call(f"{access_service.url}/notes", token=NOBODY_TOKEN) == HTTPStatus.FORBIDDEN


def test_request_log_links_caller_owner_and_approved_scope(
    access_service: AccessService,
    capsys: pytest.CaptureFixture[str],
) -> None:
    request_id = request_access(access_service)
    _call(
        f"{access_service.url}/requests/{request_id}/decision",
        "POST",
        token=GRANTER_TOKEN,
        body=b'{"decision":"approve","reason":"needed"}',
    )

    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    approved = next(event for event in events if event.get("event") == "access_approved")

    assert approved["caller"] == GRANTER
    assert approved["request"]["requester"] == NOBODY
    assert approved["request"]["owner_team"] == "research-platform"
    assert approved["request"]["approved_rights"] == ["read"]
    assert NOBODY_TOKEN not in json.dumps(events)


def test_temporary_approval_cannot_be_delegated_as_permanent_access(access_service: AccessService) -> None:
    request_id = request_access(access_service, token=DELEGATE_TOKEN, right="write")
    _call(
        f"{access_service.url}/requests/{request_id}/decision",
        "POST",
        token=GRANTER_TOKEN,
        body=b'{"decision":"approve","reason":"needed"}',
    )

    status = _call(
        f"{access_service.url}/grants",
        "POST",
        token=DELEGATE_TOKEN,
        body=json.dumps({"agent": NOBODY, "rights": ["write"]}).encode(),
    )

    assert status == HTTPStatus.FORBIDDEN
    assert (
        _call(f"{access_service.url}/notes/plan", "PUT", token=NOBODY_TOKEN, body=b"x")
        == HTTPStatus.FORBIDDEN
    )
