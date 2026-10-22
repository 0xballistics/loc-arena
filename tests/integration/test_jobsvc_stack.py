"""The job service in real containers: a job submitted over HTTP and through the tools runs as a real process.

Brings up central IAM, the job service with its two nodes and serving-agent's sandbox from the compose file a
stack run renders, with one play's secrets, and the tools gateway for the test that calls through it.
serving-agent submits from its sandbox with its own identity and through the tools gateway with its sandbox
token, as its harness would; another agent's identity, which holds no right on the service, stands in for a
non-owner. The services' logs are then built into events as after play, and the job service and its fleet are
recreated with the twin's secrets as before the honest twin.
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import time
from collections.abc import Iterator
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from typing import Any

import pytest
import yaml
from loc_arena.config import load_run_config, sandbox_service
from loc_arena.episode_stack import (
    REPOSITORY,
    issue_identities,
    issue_secrets,
    render_compose,
    token_variable,
)
from loc_arena.gateway.core import API_KEY_VARIABLE
from loc_arena.logging_.events import AppendOnlyLog, Event, read_events
from loc_arena.recorded_events import SERVICE_LOGS, build_recorded_events
from loc_arena.task import SNAPSHOT_FILE, ClockReading, SnapshotFile
from loc_arena.tools_gateway import MCP_PATH, TOOLS_GATEWAY_SERVICE

from tests.integration._docker_support import image_exists

RUN = "aurora-efficiency.iam"
CONFIG = load_run_config(REPOSITORY / "configs" / f"{RUN}.yaml")
OWNER, OTHER = "serving-agent", "eval-agent"  # eval-agent holds an identity on the job service and no right
SANDBOX = sandbox_service(OWNER)
FLEET = ["jobsvc", "node-1", "node-2"]
WAIT_SECONDS = 60

pytestmark = pytest.mark.skipif(
    not image_exists(CONFIG.settings.stack.image),
    reason=f"needs Docker and the {CONFIG.settings.stack.image} image",
)

# Run in a container with python: one call of the job service's API, as the holder of ``sys.argv[3]``.
CALL = """
import json, sys, urllib.error, urllib.request
method, path, token, body = sys.argv[1:5]
headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
request = urllib.request.Request(f"http://jobsvc:8000{path}", body.encode() or None, headers, method=method)
try:
    with urllib.request.urlopen(request, timeout=30) as reply:
        print(json.dumps([reply.status, json.loads(reply.read())]))
except urllib.error.HTTPError as error:
    print(json.dumps([error.code, error.read().decode()]))
"""
# Run in the IAM container (the engine image): a tool call with the MCP client the agents use.
TOOL_CALL = """
import json, sys
from loc_arena.scaffold.mcp_client import McpTools, over_http
url, token, tool, arguments = sys.argv[1:5]
print(json.dumps(McpTools(lambda: over_http(url, token, 30)).call(tool, json.loads(arguments))))
"""


@dataclass
class Stack:
    """The running project, the secrets of the play it runs, and how to reach the job service inside it."""

    compose: list[str]
    play: dict[str, str]
    started: float  # when the project came up, so every record it wrote falls in a window from here

    def run(self, *arguments: str) -> str:
        done = subprocess.run(
            [*self.compose, *arguments],
            env={**os.environ, **self.play},
            capture_output=True,
            text=True,
            check=False,
        )
        assert done.returncode == 0, f"{' '.join(arguments[:4])}: {done.stderr[-2000:]}"
        return done.stdout

    def identity(self, agent: str) -> str:
        found = next(i for i in issue_identities(CONFIG) if (i.service, i.agent_id) == ("jobsvc", agent))
        return self.play[found.variable]

    def http(self, agent: str, method: str, path: str, body: object = None) -> tuple[int, Any]:
        """``agent``'s call of the job service from serving-agent's sandbox, with ``agent``'s identity."""
        payload = "" if body is None else json.dumps(body)
        output = self.run(
            "exec",
            "-T",
            SANDBOX,
            "python",
            "-c",
            CALL,
            method,
            path,
            self.identity(agent),
            payload,
        )
        status, reply = json.loads(output.strip().splitlines()[-1])
        return status, reply

    def tool(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """serving-agent's call of ``tool`` through the tools gateway, with its sandbox token."""
        url = f"http://{TOOLS_GATEWAY_SERVICE}:{CONFIG.settings.stack.tools_gateway_port}{MCP_PATH}"
        token = self.play[token_variable(OWNER)]
        output = self.run(
            "exec",
            "-T",
            "iam",
            "python",
            "-c",
            TOOL_CALL,
            url,
            token,
            tool,
            json.dumps(arguments),
        )
        return json.loads(output.strip().splitlines()[-1])

    def wait_for(self, job_id: str, *states: str) -> dict[str, Any]:
        """The job once it is in one of ``states``, read by its owner."""
        deadline = time.monotonic() + WAIT_SECONDS
        while (job := self.http(OWNER, "GET", f"/jobs/{job_id}")[1])["state"] not in states:
            assert time.monotonic() < deadline, job
            time.sleep(0.5)
        return job


@pytest.fixture(scope="module")
def stack(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Stack]:
    directory = tmp_path_factory.mktemp("jobsvc-stack")
    (directory / "compose.yaml").write_text(yaml.safe_dump(render_compose(CONFIG, REPOSITORY, RUN, [])))
    attack, _ = issue_secrets(CONFIG)
    project = f"locarena-jobsvc{secrets.token_hex(3)}"
    compose = ["docker", "compose", "-p", project, "-f", str(directory / "compose.yaml")]
    running = Stack(
        [*compose, "--project-directory", str(directory)],
        {API_KEY_VARIABLE: secrets.token_urlsafe(24), **attack},
        time.time(),
    )
    try:
        running.run("build", "jobsvc", "node-1", SANDBOX)
        running.run("up", "--detach", "--wait", "--no-build", "jobsvc", SANDBOX)  # with IAM and the nodes
        yield running
    finally:
        running.run("down", "--volumes", "--remove-orphans")


def test_a_job_submitted_from_a_sandbox_runs_as_a_process_on_a_node_and_its_logs_are_its_output(
    stack: Stack,
) -> None:
    command = ["sh", "-c", "echo ran on $(hostname) in $(pwd); sleep 30"]
    status, submitted = stack.http(OWNER, "POST", "/jobs", {"name": "bench", "command": command})

    running = stack.wait_for(submitted["id"], "running")
    time.sleep(1)  # the process prints at once; the daemon serves what it has printed so far
    logs = stack.http(OWNER, "GET", f"/jobs/{submitted['id']}/logs")[1]["stdout"]

    assert (status, submitted["owner"], running["node"]) == (HTTPStatus.CREATED, OWNER, "node-1")
    assert logs.startswith("ran on ") and logs.endswith(" in /workspace\n")
    stack.http(OWNER, "DELETE", f"/jobs/{submitted['id']}")
    stack.wait_for(submitted["id"], "cancelled")


def test_a_job_submitted_through_the_tools_gateway_is_cancelled_by_its_owner_alone(stack: Stack) -> None:
    stack.run("up", "--detach", "--wait", "--no-build", TOOLS_GATEWAY_SERVICE)  # and every tool service
    submitted = stack.tool("submit_job", {"name": "loop", "command": ["sleep", "60"]})
    stack.wait_for(submitted["id"], "running")

    refused = stack.http(OTHER, "DELETE", f"/jobs/{submitted['id']}")[0]
    cancelling = stack.tool("cancel_job", {"job_id": submitted["id"]})["state"]
    ended = stack.wait_for(submitted["id"], "cancelled", "failed", "succeeded")

    assert (refused, cancelling, ended["state"]) == (HTTPStatus.FORBIDDEN, "cancelling", "cancelled")


def test_after_play_the_sealed_log_has_the_action_job_and_process_events_and_the_mirror_the_first_two(
    stack: Stack,
    tmp_path: Path,
) -> None:
    status, submitted = stack.http(
        OWNER,
        "POST",
        "/jobs",
        {"name": "quick", "command": ["sh", "-c", "exit 3"]},
    )
    assert status == HTTPStatus.CREATED, submitted
    stack.wait_for(submitted["id"], "failed")
    run_directory = tmp_path / "run"
    (run_directory / SERVICE_LOGS).mkdir(parents=True)
    for service in FLEET:
        (run_directory / SERVICE_LOGS / f"{service}.log").write_text(
            stack.run("logs", "--no-log-prefix", service),
        )
    phase = record_phase_around(run_directory / "episode", stack.started, time.time())

    build_recorded_events(run_directory, CONFIG, {"episode": None})

    def kinds(log: str) -> set[str]:  # the job's events: its submission addresses it by its name
        return {
            event.kind for event in read_events(phase / log) if event.target_id in {submitted["id"], "quick"}
        }

    assert (kinds("events.sealed.jsonl"), kinds("events.mirror.jsonl")) == (
        {"action", "job", "process"},
        {"action", "job"},
    )


def test_the_honest_twin_starts_with_an_empty_fleet_and_no_job(stack: Stack) -> None:
    running = stack.http(OWNER, "POST", "/jobs", {"name": "left", "command": ["sleep", "60"]})[1]
    stack.wait_for(running["id"], "running")
    containers = stack.run("ps", "--quiet", *FLEET).split()
    _, twin = issue_secrets(CONFIG)
    stack.play = {**stack.play, **twin}

    # As before the twin: IAM, the job service and its nodes recreated empty, with the twin's secrets.
    stack.run(
        "up",
        "--detach",
        "--wait",
        "--force-recreate",
        "--renew-anon-volumes",
        "--no-deps",
        "iam",
        *FLEET,
    )
    stack.run("up", "--detach", "--wait", "--force-recreate", "--no-deps", SANDBOX)

    assert stack.http(OWNER, "GET", "/jobs") == (HTTPStatus.OK, {"jobs": []})
    assert set(stack.run("ps", "--quiet", *FLEET).split()).isdisjoint(containers)  # no node of the attack's


def record_phase_around(phase: Path, start: float, end: float) -> Path:
    """A played episode's directory whose play window runs from ``start`` to ``end``, with empty logs."""
    phase.mkdir(parents=True)
    for log in ("events.sealed.jsonl", "events.mirror.jsonl"):
        AppendOnlyLog(phase / log, "episode-1").append(
            Event("episode-1", 0, 100.0, "system", "sealed", "tick"),
        )
    snapshot = SnapshotFile(
        sealed_path=Path("events.sealed.jsonl"),
        mirror_path=Path("events.mirror.jsonl"),
        checkout=Path("checkout"),
        deadline_ts=10_000.0,
        mode="attack",
        clock=(ClockReading(wall=start, ts=100.0),),
        finished_at=end,
    )
    (phase / SNAPSHOT_FILE).write_text(snapshot.model_dump_json())
    return phase
