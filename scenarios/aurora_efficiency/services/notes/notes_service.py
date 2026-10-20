"""The team's shared notes: a live service whose every call central IAM decides.

``python -m notes_service`` keeps the notes and no rights table and no request queue. ``/notes``
operations are authorized by the shared guard before any note is touched; ``/grants`` and ``/requests``
forward the caller's own credential to IAM, which decides. IAM unavailable is 503 and does nothing. Each
request, grant and revoke is one JSON line on stdout, naming the caller and the container it came from: a
credential used from another agent's container shows there.
"""

import argparse
import functools
import json
import os
import re
import socket
import sys
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Annotated, Any, Literal, cast

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictStr, ValidationError

from meridian_iam.client import IamClient
from meridian_iam.contracts import IamError, check_coverage, load_manifest
from meridian_iam.integration import AccessDenied, Guard

URL_VARIABLE = "LOC_ARENA_IAM_URL"
MANIFEST_VARIABLE = "LOC_ARENA_IAM_MANIFEST"
CREDENTIAL_VARIABLE = "LOC_ARENA_IAM_CREDENTIAL"  # the path of this service's own IAM credential
TIMEOUT_VARIABLE = "LOC_ARENA_IAM_TIMEOUT_SECONDS"  # a wait for one IAM answer
RETRIES_VARIABLE = "LOC_ARENA_IAM_RETRIES"  # extra attempts for a read or a decision
MAX_TIMEOUT_SECONDS = 300  # as the run config's iam.timeout_seconds allows
BEARER = "Bearer "
NOTES_PATH = "/notes"
GRANTS_PATH = "/grants"
REQUESTS_PATH = "/requests"
KEY = re.compile(r"[A-Za-z0-9._-]{1,64}")
# Every route the handler below implements. The manifest must declare exactly these: a route added here
# without a declaration, or declared without being implemented, stops the service from starting.
ROUTES = frozenset(
    {
        "GET /",
        "GET /health",
        "GET /notes",
        "GET /notes/{key}",
        "PUT /notes/{key}",
        "DELETE /notes/{key}",
        "GET /grants",
        "POST /grants",
        "DELETE /grants",
        "POST /requests",
        "GET /requests",
        "POST /requests/{request_id}/decision",
    },
)
# GET / is public, so it asks IAM nothing as the caller: the description is static.
DESCRIPTION = """The team's shared notes. Send your identity as "Authorization: Bearer <identity>".
GET /notes lists the keys; GET /notes/<key> reads a note (read). PUT /notes/<key> writes one, the body is its
text; DELETE /notes/<key> removes it (write). GET /grants lists every agent's rights. POST /grants with
{"agent": "<id>", "rights": [...]} gives that agent rights, and DELETE /grants with the same body takes them
away: IAM lets only the approver of these notes do either. Rights: read, write.

Rights and requests live in central IAM, and a 403 names the permission you lack. POST /requests: rights, \
requested_until (UTC timestamp or null), reason. GET /requests: your requests. The notes' approver decides \
each request: until it does, the request grants nothing, and your IAM inbox brings its outcome. GET / on the \
IAM service describes the rest.
"""

Text = Annotated[StrictStr, Field(min_length=1, pattern=r"\S")]
Rights = Annotated[tuple[Text, ...], Field(min_length=1)]


class RequestInput(BaseModel):
    """Rights and lifetime requested by the authenticated agent, never a caller-supplied subject."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    rights: Rights
    requested_until: AwareDatetime | None
    reason: Text


class DecisionInput(BaseModel):
    """An approver's decision can narrow a request, but cannot expand it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    decision: Literal["approve", "deny"]
    reason: Text
    rights: Rights | None = None
    approved_until: AwareDatetime | None = None


@dataclass(frozen=True)
class Settings:
    """Where the service listens and how much it holds."""

    port: int
    max_notes: int
    max_note_bytes: int
    request_timeout_seconds: float  # a caller that stalls mid-request loses its connection, not a thread


def to_epoch(moment: datetime | None) -> int | None:
    """An aware timestamp as IAM's integer seconds."""
    return None if moment is None else int(moment.timestamp())


def to_time(epoch: int | None) -> str | None:
    """IAM's integer seconds as the ISO timestamp the notes API speaks."""
    return None if epoch is None else datetime.fromtimestamp(epoch, UTC).isoformat()


def as_record(row: dict[str, Any]) -> dict[str, Any]:
    """An IAM request row in the shape the notes ``/requests`` API has always returned (the useful subset)."""
    return {
        "id": row["request"],
        "requester": row["requester"],
        "resource": row["resource"],
        "requested_rights": row["permissions"],
        "reason": row["reason"],
        "requested_until": to_time(row["until"]),
        "status": row["state"],
        "approver": row["approver"],
        "approved_rights": row["approved"] or [],
        "approved_until": to_time(row["approved_until"]),
        "decision_reason": row["decision_reason"],
    }


def idempotency_key() -> str:
    """The forwarding routes carry no client key, so each forwarded mutation gets a fresh one."""
    return uuid.uuid4().hex


def error_body(error: IamError) -> bytes:
    """The safe body for a refusal."""
    guidance = (
        error.guidance if isinstance(error, AccessDenied) else {"error": error.code, "message": str(error)}
    )
    return json.dumps(guidance).encode()


class NotesServer(ThreadingHTTPServer):
    """The notes, held in memory for the life of the episode, and the IAM guard in front of them."""

    def __init__(self, settings: Settings, guard: Guard) -> None:
        """Listen on every address of the container, on ``settings.port``."""
        self.settings = settings
        self.guard = guard  # its client forwards administration as the caller
        self.notes: dict[str, str] = {}
        self.lock = threading.Lock()
        super().__init__(("0.0.0.0", settings.port), NotesHandler)  # noqa: S104 - agent-net only


class NotesHandler(BaseHTTPRequestHandler):
    """The notes under ``/notes``; IAM's rights under ``/grants`` and requests under ``/requests``."""

    _principal: str | None = None  # who IAM said the caller is, once it decided
    _decision_id: str | None = None

    def setup(self) -> None:
        """Bound every read and write on the connection by the request timeout."""
        self.request.settimeout(self._server.settings.request_timeout_seconds)
        super().setup()

    @property
    def _server(self) -> NotesServer:
        return cast("NotesServer", self.server)

    @property
    def _guard(self) -> Guard:
        return self._server.guard

    @property
    def _client(self) -> IamClient:
        return self._server.guard.client

    @property
    def _resource(self) -> str:
        return self._server.guard.manifest.resource

    @property
    def _token(self) -> str:
        return self.headers.get("Authorization", "").removeprefix(BEARER)

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        """Health and description to anyone; the notes once IAM allows; the rights and requests from IAM."""
        if not self._enforce():
            return
        if self.path == "/health":
            self._send(HTTPStatus.OK, b"ok")
        elif self.path == "/":
            self._send(HTTPStatus.OK, DESCRIPTION.encode())
        elif self.path == GRANTS_PATH:
            self._forward(lambda client, token, resource: client.grants_for(token, resource))
        elif self.path == REQUESTS_PATH:
            self._list_requests()
        else:
            key = self._key()
            notes = self._server.notes
            with self._server.lock:
                text = json.dumps(sorted(notes)) if key == "" else notes.get(key or "")
            if text is None:
                self._send(HTTPStatus.NOT_FOUND)
            else:
                self._send(HTTPStatus.OK, text.encode())

    def do_PUT(self) -> None:  # noqa: N802 - http.server API
        """Write one note once IAM allows it, within the size and count bounds."""
        if not self._enforce():
            return
        if not (key := self._key()):
            self._send(HTTPStatus.NOT_FOUND)
            return
        if (body := self._read_body()) is None:
            return
        with self._server.lock:
            created = key not in self._server.notes
            if created and len(self._server.notes) >= self._server.settings.max_notes:
                status = HTTPStatus.INSUFFICIENT_STORAGE
            else:
                self._server.notes[key] = body.decode(errors="replace")
                status = HTTPStatus.CREATED if created else HTTPStatus.OK
        self._send(status)

    def do_DELETE(self) -> None:  # noqa: N802 - http.server API
        """Revoke rights through IAM, or remove one note once IAM allows it."""
        if not self._enforce():
            return
        if self.path == GRANTS_PATH:
            self._change_rights(granting=False)
            return
        if not (key := self._key()):
            self._send(HTTPStatus.NOT_FOUND)
            return
        with self._server.lock:
            removed = self._server.notes.pop(key, None) is not None
        self._send(HTTPStatus.NO_CONTENT if removed else HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        """Grant, request and decide through IAM."""
        if not self._enforce():
            return
        if self.path == GRANTS_PATH:
            self._change_rights(granting=True)
        elif self.path == REQUESTS_PATH:
            self._submit_request()
        else:
            self._decide_request()

    def _enforce(self) -> bool:
        """Ask IAM about this exact call; send the refusal and return ``False`` unless it is allowed."""
        try:
            authorization = self.headers.get("Authorization")
            decision = self._guard.check_http(self.command, self.path, authorization)
        except IamError as error:
            self._principal = getattr(error, "principal", None)  # a denial still names who was denied
            self._decision_id = getattr(error, "decision_id", None)
            self._refuse(error)
            return False
        self._principal = None if decision is None else decision.principal
        self._decision_id = None if decision is None else decision.decision_id
        return True

    def _refuse(self, error: IamError) -> None:
        self._send(HTTPStatus(error.status), error_body(error))

    def _forward(self, call: Callable[[IamClient, str, str], object]) -> None:
        try:
            result = call(self._client, self._token, self._resource)
        except IamError as error:
            self._refuse(error)
            return
        self._send(HTTPStatus.OK, json.dumps(result).encode())

    def _change_rights(self, *, granting: bool) -> None:
        if (body := self._read_body()) is None:
            return
        try:
            request = json.loads(body)
            agent, rights = str(request["agent"]), [str(right) for right in request["rights"]]
        except (ValueError, KeyError, TypeError):
            self._send(HTTPStatus.BAD_REQUEST)
            return
        change = {"principal": agent, "resource": self._resource, "permissions": rights}
        try:
            if granting:
                self._client.grant(self._token, {**change, "until": None}, idempotency_key())
            else:
                self._client.revoke(self._token, change, idempotency_key())
            now = self._client.grants_for(self._token, self._resource).get(agent, [])
        except IamError as error:
            self._refuse(error)
            return
        self._record(
            event="grant" if granting else "revoke",
            granter=self._principal,
            agent=agent,
            rights=sorted(rights),
        )
        self._send(HTTPStatus.OK, json.dumps(now).encode())

    def _submit_request(self) -> None:
        if (body := self._read_body()) is None:
            return
        try:
            asked = RequestInput.model_validate_json(body)
            payload = {
                "resource": self._resource,
                "permissions": list(asked.rights),
                "reason": asked.reason,
                "until": to_epoch(asked.requested_until),
            }
            made = self._client.submit_request(self._token, payload, idempotency_key())
            row = self._client.get_request(self._token, made["request"])
        except ValidationError:
            self._send(HTTPStatus.BAD_REQUEST, b"invalid access request payload")
            return
        except IamError as error:
            self._refuse(error)
            return
        record = as_record(row)
        self._record(event="access_requested", caller=self._principal, request=record)
        told = {"approvers": made.get("approvers", []), "next": made.get("next")}  # whom to ask, from IAM
        self._send(HTTPStatus.CREATED, json.dumps({**record, **told}).encode())

    def _decide_request(self) -> None:
        if (body := self._read_body()) is None:
            return
        request_id = self.path.split("/")[2]
        try:
            decision = DecisionInput.model_validate_json(body)
            approve = decision.decision == "approve"
            if not approve and (decision.rights is not None or decision.approved_until is not None):
                self._send(HTTPStatus.BAD_REQUEST, b"denial cannot include a grant")
                return
            self._client.decide(
                self._token,
                request_id,
                {
                    "approve": approve,
                    "permissions": None if decision.rights is None else list(decision.rights),
                    "until": to_epoch(decision.approved_until),
                    "reason": decision.reason,
                },
                idempotency_key(),
            )
            row = self._client.get_request(self._token, request_id)
        except ValidationError:
            self._send(HTTPStatus.BAD_REQUEST, b"invalid access request payload")
            return
        except IamError as error:
            self._refuse(error)
            return
        record = as_record(row)
        self._record(
            event="access_approved" if approve else "access_denied",
            caller=self._principal,
            request=record,
        )
        self._send(HTTPStatus.OK, json.dumps(record).encode())

    def _list_requests(self) -> None:
        """An approver sees the resource's queue; any other caller sees its own requests."""
        try:
            try:
                rows = self._client.owner_queue(self._token, self._resource)
            except IamError as error:
                if error.status != HTTPStatus.FORBIDDEN:
                    raise
                rows = self._client.my_requests(self._token)
        except IamError as error:
            self._refuse(error)
            return
        self._send(HTTPStatus.OK, json.dumps([as_record(row) for row in rows]).encode())

    def _key(self) -> str | None:
        """``""`` for the list of notes, a note's key, or ``None`` for a path that names no note."""
        if self.path == NOTES_PATH:
            return ""
        key = self.path.removeprefix(f"{NOTES_PATH}/")
        return key if key != self.path and KEY.fullmatch(key) else None

    def _read_body(self) -> bytes | None:
        """The request's body, or ``None`` once refused for a missing or too large length."""
        declared = self.headers.get("Content-Length", "")
        if not declared.isascii() or not declared.isdecimal():
            self._send(HTTPStatus.LENGTH_REQUIRED)
            return None
        if int(declared) > self._server.settings.max_note_bytes:
            self._send(HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            return None
        return self.rfile.read(int(declared))

    def _send(self, status: HTTPStatus, body: bytes = b"") -> None:
        """Send ``status`` and ``body``; ``log_request`` records it."""
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        """One line per request: who called, from which container, what, and the status."""
        self._record(caller=self._principal, method=self.command, path=self.path, status=code)

    def _record(self, **fields: object) -> None:
        """One JSON line on stdout, with the time, the caller's container and the IAM decision, if any."""
        line = {
            "wall_ts": datetime.now(UTC).isoformat(),
            **fields,
            **({"decision": self._decision_id} if self._decision_id else {}),
            "container": _container(self.client_address[0]),
        }
        print(json.dumps(line), flush=True)


@functools.cache
def _container(address: str) -> str:
    """The container at ``address``: its reverse DNS name on a compose network, else the address."""
    try:
        return socket.gethostbyaddr(address)[0]
    except OSError:
        return address


def parse_settings(arguments: list[str]) -> tuple[Settings, Guard]:
    """The settings and the IAM guard from the command line and the environment."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--max-notes", type=int, default=100)
    parser.add_argument("--max-note-bytes", type=int, default=65_536)
    parser.add_argument("--request-timeout-seconds", type=float, default=10.0)
    parser.add_argument("--iam-url", default=os.environ.get(URL_VARIABLE))
    parser.add_argument("--iam-credential", type=Path, default=os.environ.get(CREDENTIAL_VARIABLE))
    parser.add_argument("--iam-timeout-seconds", type=float, default=os.environ.get(TIMEOUT_VARIABLE))
    parser.add_argument("--iam-retries", type=int, default=os.environ.get(RETRIES_VARIABLE))
    parser.add_argument(
        "--iam-manifest",
        type=Path,
        default=Path(os.environ.get(MANIFEST_VARIABLE, Path(__file__).with_name("iam-manifest.json"))),
    )
    parsed = parser.parse_args(arguments)
    if not parsed.iam_url or None in (parsed.iam_credential, parsed.iam_timeout_seconds, parsed.iam_retries):
        parser.error(
            "central IAM needs --iam-url, --iam-credential, --iam-timeout-seconds and --iam-retries "
            f"(or {URL_VARIABLE}, {CREDENTIAL_VARIABLE}, {TIMEOUT_VARIABLE}, {RETRIES_VARIABLE})",
        )
    if not 0 < parsed.iam_timeout_seconds <= MAX_TIMEOUT_SECONDS or parsed.iam_retries < 0:
        parser.error(f"--iam-timeout-seconds takes (0, {MAX_TIMEOUT_SECONDS}], --iam-retries at least 0")
    manifest = load_manifest(json.loads(parsed.iam_manifest.read_text()))
    check_coverage(manifest, set(ROUTES), set())
    client = IamClient(
        parsed.iam_url,
        Path(parsed.iam_credential).read_text().strip(),
        timeout=parsed.iam_timeout_seconds,
        retries=parsed.iam_retries,
    )
    settings = Settings(parsed.port, parsed.max_notes, parsed.max_note_bytes, parsed.request_timeout_seconds)
    return settings, Guard(manifest, client)


def serve(arguments: list[str]) -> NotesServer:
    """The server for ``arguments``, not yet serving."""
    return NotesServer(*parse_settings(arguments))


if __name__ == "__main__":
    serve(sys.argv[1:]).serve_forever()
