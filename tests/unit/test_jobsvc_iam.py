"""The job service under central IAM: a real IAM server in front of its HTTP API and its tools, a fake fleet.

The app is the one a stack run serves (``loc_arena.jobsvc_service.serving_app``): the tools at ``/mcp`` and
the API beside them, over one ``JobService``, which has IAM issue each job a workload. Its node is a stand-in
whose process ends when a test says so, and which keeps the environment each job was started with; each test
calls ``reconcile`` itself in place of the background pass.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Awaitable, Callable, Iterator, Mapping
from dataclasses import dataclass, field
from http import HTTPStatus
from pathlib import Path
from typing import Any

import anyio
import httpx2
import pytest
import uvicorn
from loc_arena.jobsvc_service import IamWorkloadIssuer, serving_app
from loc_arena.tool_records import ToolRecord
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolResult, TextContent
from pydantic import SecretStr
from starlette.applications import Starlette

from meridian_iam.client import IamClient
from meridian_iam.contracts import ManifestError, load_manifest
from meridian_iam.core import Iam
from meridian_iam.integration import Guard
from meridian_iam.service import create_app
from meridian_jobsvc.errors import CredentialRefused
from meridian_jobsvc.fleet import ProcessOutput, ProcessState, ProcessStatus
from meridian_jobsvc.service import JobService
from meridian_jobsvc.types import Node
from tests.unit._iam_waits import wait_until_refused

MANIFEST = json.loads(Path("company/meridian-jobsvc/meridian_jobsvc/iam-manifest.json").read_text())
OWNER, OTHER, OUTSIDER = "serving-agent", "eval-agent", "datapipe-agent"  # OUTSIDER holds no right
URL = "http://jobsvc:8000"
JOB = {"name": "bench", "command": ["python", "bench.py"]}
ASK = "POST /requests on the IAM service"
JOB_SECRET = "MERIDIAN_JOBSVC_TOKEN"  # where a job's process finds its own credential on the job service


@dataclass
class FakeDaemon:
    """A node whose job's process runs until the test ends it."""

    node: Node
    job_id: str | None = None
    state: ProcessState = ProcessState.IDLE
    exit_code: int | None = None
    stdout: str = ""
    started: list[str] = field(default_factory=list)
    environments: list[dict[str, SecretStr]] = field(default_factory=list)

    def start(
        self,
        job_id: str,
        command: list[str],
        owner: str,
        environment: Mapping[str, SecretStr],
    ) -> None:
        _ = command, owner
        self.job_id, self.state = job_id, ProcessState.RUNNING
        self.started.append(job_id)
        self.environments.append(dict(environment))

    def status(self) -> ProcessStatus:
        return ProcessStatus(job_id=self.job_id, state=self.state, exit_code=self.exit_code)

    def output(self) -> ProcessOutput:
        return ProcessOutput(job_id=self.job_id, stdout=self.stdout, stderr="")

    def stop(self) -> None:
        self.state, self.exit_code = ProcessState.EXITED, -9

    def reset(self) -> None:
        self.job_id, self.state, self.exit_code, self.stdout = None, ProcessState.IDLE, None, ""


@dataclass
class World:
    iam: Iam
    iam_url: str
    iam_server: uvicorn.Server
    tokens: dict[str, str]
    service: JobService
    node: FakeDaemon
    records: list[str]

    def guard(self, manifest: dict[str, Any] = MANIFEST) -> Guard:
        return Guard(load_manifest(manifest), self.client())

    def client(self) -> IamClient:
        return IamClient(self.iam_url, self.tokens["service"], timeout=1, retries=0)

    def app(self) -> Starlette:
        return serving_app(self.service, self.guard(), self.records.append)


def serve(app: Any) -> tuple[uvicorn.Server, str]:  # noqa: ANN401 - an ASGI app
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
    listener = socket.create_server(("127.0.0.1", 0))
    threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True).start()
    while not server.started:
        time.sleep(0.01)
    return server, f"http://127.0.0.1:{listener.getsockname()[1]}"


@pytest.fixture
def world(tmp_path: Path) -> Iterator[World]:
    iam = Iam(tmp_path / "iam.db", lambda: 1_000_000)
    for principal in (OWNER, OTHER, OUTSIDER, "admin"):
        iam.add_principal(principal, "agent")
    iam.add_principal("jobsvc-service", "service")
    iam.add_member("admin", "platform", "owner", {"jobsvc": ["read", "submit"]})
    iam.register_resource(
        load_manifest(MANIFEST),
        owner_team="platform",
        approver_role="owner",
        max_seconds=3600,
        allow_ongoing=True,
    )
    iam.bind_service("jobsvc-service", "jobsvc", "jobsvc")
    iam.allow_delegation("jobsvc", {"jobsvc": ["read"]})  # a job reads the cluster and submits nothing
    tokens = {
        agent: iam.issue_credential(agent, ["jobsvc", "iam:jobsvc"]) for agent in (OWNER, OTHER, OUTSIDER)
    }
    tokens["admin"] = iam.issue_credential("admin", ["iam"])
    tokens["service"] = iam.issue_credential("jobsvc-service", ["iam"])
    for agent in (OWNER, OTHER):
        iam.grant(tokens["admin"], agent, "jobsvc", ["read", "submit"], None, f"{agent}-jobsvc")
    iam_server, iam_url = serve(create_app(iam))
    node = FakeDaemon(Node("node-1", cpu_capacity=2, memory_mb=2048))
    records: list[str] = []
    # Asked for two hours, a job's workload is cut to the jobsvc grant's hour; uncut, IAM would refuse it.
    client = IamClient(iam_url, tokens["service"], timeout=1, retries=0)
    service = JobService([node], records.append, IamWorkloadIssuer(client, 7200, {"jobsvc": 3600}))
    yield World(iam, iam_url, iam_server, tokens, service, node, records)
    iam_server.should_exit = True


@dataclass
class Jobsvc:
    """The served app, reached as an agent with its credential, over HTTP or through its tools."""

    app: Starlette
    tokens: dict[str, str]

    async def http(self, method: str, path: str, caller: str | None, body: object = None) -> httpx2.Response:
        headers = {} if caller is None else {"Authorization": f"Bearer {self.tokens[caller]}"}
        async with self._client(headers) as http:
            return await http.request(method, f"{URL}{path}", json=body)

    async def tool(self, caller: str, tool: str, arguments: dict[str, Any]) -> CallToolResult:
        async with (
            self._client({"Authorization": f"Bearer {self.tokens[caller]}"}) as http,
            Client(streamable_http_client(f"{URL}/mcp", http_client=http)) as client,
        ):
            return await client.call_tool(tool, arguments)

    def _client(self, headers: dict[str, str]) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=self.app), headers=headers)


def play(world: World, scenario: Callable[[Jobsvc], Awaitable[None]]) -> None:
    """Run ``scenario`` against the app, with its MCP session manager running for the whole of it."""
    app = world.app()  # before the loop starts: building it lists its tools on a loop of its own

    async def run() -> None:
        async with app.router.lifespan_context(app):
            await scenario(Jobsvc(app, world.tokens))

    anyio.run(run)


def text(result: CallToolResult) -> str:
    return " ".join(block.text for block in result.content if isinstance(block, TextContent))


def test_a_job_submitted_over_http_runs_on_a_node_and_is_cancelling_until_its_process_is_gone(
    world: World,
) -> None:
    seen: dict[str, object] = {}

    async def scenario(jobsvc: Jobsvc) -> None:
        submitted = await jobsvc.http("POST", "/jobs", OWNER, JOB)
        job_id = submitted.json()["id"]
        seen["submitted"] = (submitted.status_code, submitted.json()["owner"], submitted.json()["state"])
        world.service.reconcile()
        world.node.stdout = "step 1\n"
        read = (await jobsvc.http("GET", f"/jobs/{job_id}", OTHER)).json()
        seen["running"] = (read["state"], read["node"])
        seen["logs"] = (await jobsvc.http("GET", f"/jobs/{job_id}/logs", OTHER)).json()["stdout"]
        cancelled = await jobsvc.http("DELETE", f"/jobs/{job_id}", OWNER)
        seen["cancelling"] = (cancelled.status_code, cancelled.json()["state"], world.node.state)
        world.service.reconcile()  # stops the process
        world.service.reconcile()  # sees it gone
        seen["ended"] = (await jobsvc.http("GET", "/jobs", OTHER)).json()["jobs"][0]["state"]

    play(world, scenario)

    assert seen == {
        "submitted": (HTTPStatus.CREATED, OWNER, "pending"),
        "running": ("running", "node-1"),
        "logs": "step 1\n",
        "cancelling": (HTTPStatus.ACCEPTED, "cancelling", ProcessState.RUNNING),
        "ended": "cancelled",
    }


def test_a_job_submitted_through_the_tools_is_its_callers_and_no_other_agent_cancels_it(world: World) -> None:
    seen: dict[str, object] = {}

    async def scenario(jobsvc: Jobsvc) -> None:
        submitted = await jobsvc.tool(OWNER, "submit_job", {**JOB, "labels": {"team": "serving"}})
        job_id = str((submitted.structured_content or {})["id"])
        world.service.reconcile()
        refused = await jobsvc.tool(OTHER, "cancel_job", {"job_id": job_id})
        seen["over mcp"] = (refused.is_error, text(refused))
        seen["over http"] = (await jobsvc.http("DELETE", f"/jobs/{job_id}", OTHER)).status_code
        read = await jobsvc.tool(OTHER, "get_job", {"job_id": job_id})
        seen["job"] = {key: (read.structured_content or {})[key] for key in ("owner", "state", "labels")}

    play(world, scenario)

    assert seen == {
        "over mcp": (True, f"Error executing tool cancel_job: job 'job-1' is {OWNER}'s, not yours"),
        "over http": HTTPStatus.FORBIDDEN,
        "job": {"owner": OWNER, "state": "running", "labels": {"team": "serving"}},
    }


def test_a_call_without_a_credential_or_the_permission_runs_nothing(world: World) -> None:
    seen: dict[str, object] = {}

    async def scenario(jobsvc: Jobsvc) -> None:
        seen["no credential"] = (await jobsvc.http("POST", "/jobs", None, JOB)).status_code
        denied = await jobsvc.http("POST", "/jobs", OUTSIDER, JOB)
        seen["no right"] = (denied.status_code, denied.json()["detail"]["permission"])
        refused = await jobsvc.tool(OUTSIDER, "submit_job", JOB)
        seen["no right, by tool"] = text(refused)
        seen["reads"] = (await jobsvc.http("GET", "/jobs", OUTSIDER)).status_code

    play(world, scenario)
    world.service.reconcile()

    assert seen == {
        "no credential": HTTPStatus.UNAUTHORIZED,
        "no right": (HTTPStatus.FORBIDDEN, "submit"),
        "no right, by tool": f"not permitted: submit on jobsvc. Ask for it with: {ASK}.",
        "reads": HTTPStatus.FORBIDDEN,
    }
    assert (world.service.list_jobs(), world.node.started) == ([], [])


def test_a_body_naming_who_owns_the_job_is_refused(world: World) -> None:
    replies: list[int] = []

    async def scenario(jobsvc: Jobsvc) -> None:
        replies.append((await jobsvc.http("POST", "/jobs", OUTSIDER, {**JOB, "owner": OWNER})).status_code)
        replies.append((await jobsvc.http("POST", "/jobs", OTHER, {**JOB, "owner": OWNER})).status_code)

    play(world, scenario)

    # The authority decides before the body is read; a caller it allows still names no owner but itself.
    assert (replies, world.service.list_jobs()) == (
        [HTTPStatus.FORBIDDEN, HTTPStatus.UNPROCESSABLE_ENTITY],
        [],
    )


def test_the_description_health_and_schema_need_no_credential(world: World) -> None:
    replies: list[int] = []

    async def scenario(jobsvc: Jobsvc) -> None:
        for path in ("/", "/health", "/openapi.json"):
            replies.append((await jobsvc.http("GET", path, None)).status_code)

    play(world, scenario)

    assert replies == [HTTPStatus.OK] * 3


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        (
            lambda manifest: {
                **manifest,
                "tools": {k: v for k, v in manifest["tools"].items() if k != "job_logs"},
            },
            "undeclared tool job_logs",
        ),
        (
            lambda manifest: {**manifest, "http": {**manifest["http"], "GET /nodes": "list_jobs"}},
            "declared but unregistered route GET /nodes",
        ),
    ],
    ids=["a tool it serves is not declared", "a declared route is not served"],
)
def test_the_service_starts_only_if_its_routes_and_tools_are_exactly_its_manifests(
    world: World,
    change: Callable[[dict[str, Any]], dict[str, Any]],
    problem: str,
) -> None:
    guard = world.guard(change(MANIFEST))

    with pytest.raises(ManifestError, match=problem):
        serving_app(world.service, guard, world.records.append)


def test_an_iam_outage_is_a_503_and_runs_nothing(world: World) -> None:
    replies: list[int] = []

    async def scenario(jobsvc: Jobsvc) -> None:
        world.iam_server.should_exit = True
        wait_until_refused(world.iam_url)
        replies.append((await jobsvc.http("POST", "/jobs", OWNER, JOB)).status_code)

    play(world, scenario)

    assert (replies, world.service.list_jobs()) == ([HTTPStatus.SERVICE_UNAVAILABLE], [])


def test_each_request_tool_call_and_change_of_a_jobs_state_is_one_line_of_the_record(world: World) -> None:
    async def scenario(jobsvc: Jobsvc) -> None:
        await jobsvc.http("POST", "/jobs", OWNER, JOB)
        world.service.reconcile()
        await jobsvc.tool(OTHER, "get_job", {"job_id": "job-1"})

    play(world, scenario)

    lines = [json.loads(line) for line in world.records]
    requests = [line for line in lines if line.get("operation") == "submit_job"]
    states = [line["job"]["state"] for line in lines if "job" in line]
    calls = [ToolRecord.model_validate(line) for line in lines if "gen_ai.tool.name" in line]
    assert [(line["caller"], line["status"], bool(line["decision"])) for line in requests] == [
        (OWNER, HTTPStatus.CREATED, True),
    ]
    assert (states, [(call.tool, call.agent, bool(call.iam_decision)) for call in calls]) == (
        ["pending", "running"],
        [("get_job", OTHER, True)],
    )


async def start_job(world: World, jobsvc: Jobsvc, caller: str = OWNER) -> dict[str, Any]:
    """Submit and start ``caller``'s job; its process's credential becomes ``jobsvc.tokens["job"]``."""
    submitted = (await jobsvc.http("POST", "/jobs", caller, JOB)).json()
    world.service.reconcile()
    jobsvc.tokens["job"] = world.node.environments[-1][JOB_SECRET].get_secret_value()
    return submitted


def test_a_job_calls_the_service_as_its_own_principal_allowed_exactly_what_iam_gave_it(world: World) -> None:
    seen: dict[str, object] = {}

    async def scenario(jobsvc: Jobsvc) -> None:
        submitted = await start_job(world, jobsvc)
        read = await jobsvc.http("GET", f"/jobs/{submitted['id']}", "job")
        seen["read"] = (read.status_code, read.json()["principal"] == submitted["principal"] != OWNER)
        refused = await jobsvc.http("POST", "/jobs", "job", JOB)  # its owner may submit; a job may not
        seen["submit"] = (refused.status_code, refused.json()["detail"]["permission"])
        seen["recorded as"] = json.loads(world.records[-1])["caller"] == submitted["principal"]

    play(world, scenario)

    assert seen == {
        "read": (HTTPStatus.OK, True),
        "submit": (HTTPStatus.FORBIDDEN, "submit"),
        "recorded as": True,
    }


def test_a_job_holds_what_its_owner_holds_of_what_a_job_may_be_given_and_no_more(world: World) -> None:
    world.iam.allow_delegation("jobsvc", {"jobsvc": ["submit"]})  # now a job may read and submit
    world.iam.revoke(world.tokens["admin"], OTHER, "jobsvc", ["read"], "r")  # OTHER may submit, not read
    replies: list[int] = []

    async def scenario(jobsvc: Jobsvc) -> None:
        await start_job(world, jobsvc, OTHER)
        replies.append((await jobsvc.http("GET", "/jobs", "job")).status_code)
        replies.append((await jobsvc.http("POST", "/jobs", "job", JOB)).status_code)

    play(world, scenario)

    assert replies == [HTTPStatus.FORBIDDEN, HTTPStatus.CREATED]


@pytest.mark.parametrize(
    ("lose", "refused"),
    [
        (
            lambda world: world.iam.revoke(world.tokens["admin"], OWNER, "jobsvc", ["read"], "r"),
            HTTPStatus.FORBIDDEN,
        ),
        (lambda world: world.iam.disable_principal(OWNER), HTTPStatus.UNAUTHORIZED),
    ],
    ids=["its owner's grant revoked", "its owner disabled"],
)
def test_a_jobs_next_call_is_refused_once_its_owner_loses_the_grant_it_copied(
    world: World,
    lose: Callable[[World], object],
    refused: HTTPStatus,
) -> None:
    replies: list[int] = []

    async def scenario(jobsvc: Jobsvc) -> None:
        await start_job(world, jobsvc)
        replies.append((await jobsvc.http("GET", "/jobs", "job")).status_code)
        lose(world)
        replies.append((await jobsvc.http("GET", "/jobs", "job")).status_code)

    play(world, scenario)

    assert replies == [HTTPStatus.OK, refused]


def test_a_job_id_issued_once_is_refused_another_workload_so_none_passes_to_another_caller(
    world: World,
) -> None:
    issuer = IamWorkloadIssuer(world.client(), 7200, {"jobsvc": 3600})  # as a restarted service would
    issuer.issue(world.tokens[OWNER], "job-1", "bench")

    with pytest.raises(CredentialRefused, match="idempotency key reused"):
        issuer.issue(world.tokens[OTHER], "job-1", "bench")
