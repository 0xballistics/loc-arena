"""The notes service under central IAM: every call is checked online; rights and requests live in IAM.

``python -m notes_iam`` serves the same routes as ``notes_service`` and keeps no rights table and no request
queue. ``/notes`` operations are authorized by the shared guard before any note is touched; ``/grants`` and
``/requests`` are compatibility routes that forward the caller's own credential to IAM, which decides. IAM
unavailable is 503 and does nothing. This module is the only one that imports the IAM client, so the plain
image (``notes_service`` alone) needs none.
"""

import argparse
import json
import os
import sys
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from http import HTTPStatus
from pathlib import Path
from typing import Any, ClassVar

from access_requests import DecisionInput, RequestInput
from notes_service import (
    BEARER,
    ENFORCED_RIGHTS,
    GRANTS_PATH,
    REQUESTS_PATH,
    NotesHandler,
    NotesServer,
    Settings,
)
from pydantic import ValidationError

from meridian_iam.client import IamClient
from meridian_iam.contracts import IamError, check_coverage, load_manifest
from meridian_iam.integration import AccessDenied, Guard

URL_VARIABLE = "LOC_ARENA_IAM_URL"
MANIFEST_VARIABLE = "LOC_ARENA_IAM_MANIFEST"
CREDENTIAL_VARIABLE = "LOC_ARENA_IAM_CREDENTIAL"  # the path of this service's own IAM credential
TIMEOUT_VARIABLE = "LOC_ARENA_IAM_TIMEOUT_SECONDS"  # a wait for one IAM answer
RETRIES_VARIABLE = "LOC_ARENA_IAM_RETRIES"  # extra attempts for a read or a decision
MAX_TIMEOUT_SECONDS = 300  # as the run config's iam.timeout_seconds allows


@dataclass(frozen=True)
class IamBinding:
    """The guard for this service's manifest and the client used to forward administration."""

    guard: Guard
    client: IamClient
    resource: str


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
    """The compatibility routes carry no client key, so each forwarded mutation gets a fresh one."""
    return uuid.uuid4().hex


def error_body(error: IamError) -> bytes:
    """The safe body for a forwarded refusal."""
    guidance = (
        error.guidance if isinstance(error, AccessDenied) else {"error": error.code, "message": str(error)}
    )
    return json.dumps(guidance).encode()


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


# What GET / adds about requests in IAM mode. Static: GET / is public, so it asks IAM nothing as the caller.
REQUESTS_DESCRIPTION = (
    "\nRights and requests live in central IAM, and a 403 names the permission you lack. "
    "POST /requests: rights, requested_until (UTC timestamp or null), reason. "
    "GET /requests: your requests, or the queue if you approve requests for these notes. "
    "POST /requests/<id>/decision: decision (approve/deny), reason, optional narrowed rights and "
    "approved_until. A request grants nothing until an approver decides it. GET / on the IAM service "
    "describes the rest.\n"
)


class IamNotesHandler(NotesHandler):
    """The same routes with every decision made by IAM: the guard for notes, IAM for rights and requests."""

    _principal: str | None = None
    _decision_id: str | None = None

    def _requests_description(self) -> str:
        return REQUESTS_DESCRIPTION

    def _identify(self) -> str | None:
        return self._principal

    binding: ClassVar[IamBinding]

    @property
    def _binding(self) -> IamBinding:
        return self.binding

    @property
    def _token(self) -> str:
        return self.headers.get("Authorization", "").removeprefix(BEARER)

    def _enforce(self) -> bool:
        """Ask IAM about this exact call; send the refusal and return ``False`` unless it is allowed."""
        try:
            decision = self._binding.guard.check_http(
                self.command,
                self.path,
                self.headers.get("Authorization"),
            )
        except IamError as error:
            self._principal = getattr(error, "principal", None)  # a denial still names who was denied
            self._decision_id = getattr(error, "decision_id", None)
            self._refuse(error)
            return False
        self._principal = None if decision is None else decision.principal
        self._decision_id = None if decision is None else decision.decision_id
        return True

    def _record(self, **fields: object) -> None:
        extra = {"decision": self._decision_id} if self._decision_id else {}
        super()._record(**fields, **extra)

    def _refuse(self, error: IamError) -> None:
        self._send(HTTPStatus(error.status), error_body(error))

    def _admit(self, right: str | None) -> bool:
        return True  # the guard already decided; there is no local rights table to consult

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        """Notes, health and description as before; the rights and the requests come from IAM."""
        if not self._enforce():
            return
        if self.path == GRANTS_PATH:
            self._forward(lambda c, t, r: c.grants_for(t, r), HTTPStatus.OK)
        elif self.path == REQUESTS_PATH:
            self._list_requests_from_iam()
        else:
            super().do_GET()

    def do_PUT(self) -> None:  # noqa: N802 - http.server API
        """Write one note once IAM allows it."""
        if self._enforce():
            super().do_PUT()

    def do_DELETE(self) -> None:  # noqa: N802 - http.server API
        """Revoke rights through IAM, or remove one note once IAM allows it."""
        if not self._enforce():
            return
        if self.path == GRANTS_PATH:
            self._change_rights_in_iam(granting=False)
        else:
            super().do_DELETE()

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        """Grant, request and decide through IAM."""
        if not self._enforce():
            return
        if self.path == GRANTS_PATH:
            self._change_rights_in_iam(granting=True)
        elif self.path == REQUESTS_PATH:
            self._submit_to_iam()
        else:
            self._decide_in_iam()

    def _forward(self, call: Callable[[IamClient, str, str], object], status: HTTPStatus) -> None:
        try:
            result = call(self._binding.client, self._token, self._binding.resource)
        except IamError as error:
            self._refuse(error)
            return
        self._send(status, json.dumps(result).encode())

    def _change_rights_in_iam(self, *, granting: bool) -> None:
        if (body := self._read_body()) is None:
            return
        try:
            request = json.loads(body)
            agent, rights = str(request["agent"]), [str(right) for right in request["rights"]]
        except (ValueError, KeyError, TypeError):
            self._send(HTTPStatus.BAD_REQUEST)
            return
        binding = self._binding
        change = {"principal": agent, "resource": binding.resource, "permissions": rights}
        try:
            if granting:
                binding.client.grant(self._token, {**change, "until": None}, idempotency_key())
            else:
                binding.client.revoke(self._token, change, idempotency_key())
            now = binding.client.grants_for(self._token, binding.resource).get(agent, [])
        except IamError as error:
            self._refuse(error)
            return
        self._record(
            event="grant" if granting else "revoke",
            granter=self._caller,
            agent=agent,
            rights=sorted(rights),
        )
        self._send(HTTPStatus.OK, json.dumps(now).encode())

    def _submit_to_iam(self) -> None:
        if (body := self._read_body()) is None:
            return
        try:
            asked = RequestInput.model_validate_json(body)
            binding = self._binding
            payload = {
                "resource": binding.resource,
                "permissions": list(asked.rights),
                "reason": asked.reason,
                "until": to_epoch(asked.requested_until),
            }
            made = binding.client.submit_request(self._token, payload, idempotency_key())
            row = binding.client.get_request(self._token, made["request"])
        except ValidationError:
            self._send(HTTPStatus.BAD_REQUEST, b"invalid access request payload")
            return
        except IamError as error:
            self._refuse(error)
            return
        record = as_record(row)
        self._record(event="access_requested", caller=self._caller, request=record)
        told = {"approvers": made.get("approvers", []), "next": made.get("next")}  # whom to ask, from IAM
        self._send(HTTPStatus.CREATED, json.dumps({**record, **told}).encode())

    def _decide_in_iam(self) -> None:
        if (body := self._read_body()) is None:
            return
        request_id = self.path.split("/")[2]
        try:
            decision = DecisionInput.model_validate_json(body)
            binding = self._binding
            approve = decision.decision == "approve"
            if not approve and (decision.rights is not None or decision.approved_until is not None):
                self._send(HTTPStatus.BAD_REQUEST, b"denial cannot include a grant")
                return
            binding.client.decide(
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
            row = binding.client.get_request(self._token, request_id)
        except ValidationError:
            self._send(HTTPStatus.BAD_REQUEST, b"invalid access request payload")
            return
        except IamError as error:
            self._refuse(error)
            return
        record = as_record(row)
        self._record(
            event="access_approved" if approve else "access_denied",
            caller=self._caller,
            request=record,
        )
        self._send(HTTPStatus.OK, json.dumps(record).encode())

    def _list_requests_from_iam(self) -> None:
        """An approver sees the resource's queue; any other caller sees its own requests."""
        binding = self._binding
        try:
            try:
                rows = binding.client.owner_queue(self._token, binding.resource)
            except IamError as error:
                if error.status != HTTPStatus.FORBIDDEN:
                    raise
                rows = binding.client.my_requests(self._token)
        except IamError as error:
            self._refuse(error)
            return
        self._send(HTTPStatus.OK, json.dumps([as_record(row) for row in rows]).encode())


def make_iam_handler(binding: IamBinding) -> type[IamNotesHandler]:
    """The IAM handler bound to ``binding``; handlers are built per request, so it is a class attribute."""
    return type("BoundIamNotesHandler", (IamNotesHandler,), {"binding": binding})


def parse_settings(arguments: list[str]) -> tuple[Settings, IamBinding]:
    """The settings and the IAM binding from the command line and the environment."""
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
    settings = Settings(
        parsed.port,
        {},
        ENFORCED_RIGHTS,
        {},
        False,
        False,
        parsed.max_notes,
        parsed.max_note_bytes,
        parsed.request_timeout_seconds,
    )
    return settings, IamBinding(Guard(manifest, client), client, manifest.resource)


def serve(arguments: list[str]) -> NotesServer:
    """The server for ``arguments``, not yet serving."""
    settings, binding = parse_settings(arguments)
    return NotesServer(settings, handler=make_iam_handler(binding))


if __name__ == "__main__":
    serve(sys.argv[1:]).serve_forever()
