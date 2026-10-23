"""The job service's records and its nodes' process facts, built into events after play.

An attack run plays in process; the records are written as a stack run copies them out
(``services/jobsvc.log``, ``services/node-<n>.log``), at times inside the episode's play window.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from http import HTTPStatus
from pathlib import Path

import pytest
from loc_arena.gateway.core import API_KEY_VARIABLE, GATEWAY_URL_VARIABLE
from loc_arena.harness import build_recorded_run_events

from meridian_common.jobclient.models import JobState
from meridian_jobsvc.service import Job
from tests.integration._records_support import (
    LIVE,
    LOOK_AROUND,
    MIRROR,
    SEALED,
    events_of,
    play,
    read_unattributed_reasons,
    read_window,
    write_log,
)

OWNER = "serving-agent"
JOB_OPERATIONS = {"submit_job", "list_jobs", "get_job", "job_logs", "cancel_job"}
STEP = timedelta(milliseconds=1)


@pytest.fixture(autouse=True)
def no_live_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(API_KEY_VARIABLE, raising=False)
    monkeypatch.delenv(GATEWAY_URL_VARIABLE, raising=False)


@pytest.fixture
def run(tmp_path: Path) -> Path:
    return play(tmp_path / "run", episode=[LOOK_AROUND])


def request_line(at: datetime, method: str, status: int, operation: str, arguments: dict[str, object]) -> str:
    """serving-agent's request over HTTP, as the job service records it."""
    return json.dumps(
        {
            "wall_ts": at.isoformat(),
            "caller": OWNER,
            "method": method,
            "path": "/jobs",
            "status": status,
            "decision": "d-1",
            "operation": operation,
            "arguments": arguments,
        },
    )


def tool_line(at: datetime, tool: str, arguments: dict[str, object], **outcome: object) -> str:
    """serving-agent's tool call, as the job service's recorder writes it: by default answered."""
    return json.dumps(
        {
            "time": at.isoformat(),
            "gen_ai.tool.name": tool,
            "gen_ai.tool.call.arguments": arguments,
            "gen_ai.agent.name": OWNER,
            **(outcome or {"gen_ai.tool.call.result": {"id": "job-1"}}),
        },
    )


def state_line(at: datetime, state: JobState, node: str | None = None) -> str:
    """A change of job-1's state, as the job service records it."""
    job = Job(
        id="job-1",
        name="bench",
        command=["python", "bench.py"],
        cpu=1,
        memory_mb=512,
        priority=0,
        depends_on=[],
        labels={},
        owner=OWNER,
        state=state,
        node=node,
        submitted_at=at,
    )
    return json.dumps({"wall_ts": at.isoformat(), "job": job.model_dump(mode="json")})


def fact_line(at: datetime, phase: str, **fact: object) -> str:
    """A process fact of job-1 on node-1, as its daemon writes it."""
    process = {"node": "node-1", "phase": phase, "job_id": "job-1", "owner": OWNER, **fact}
    return json.dumps({"wall_ts": at.isoformat(), "process": process})


def test_a_call_that_submits_or_cancels_a_job_is_an_action_on_both_logs_and_an_answered_read_is_none(
    run: Path,
) -> None:
    started, _ = read_window(run / "episode")
    write_log(
        run,
        "jobsvc",
        request_line(started, "POST", HTTPStatus.CREATED, "submit_job", {"name": "bench"}),
        request_line(started + STEP, "GET", HTTPStatus.OK, "list_jobs", {}),
        tool_line(started + 2 * STEP, "get_job", {"job_id": "job-1"}),
        tool_line(started + 3 * STEP, "cancel_job", {"job_id": "job-1"}),
        tool_line(started + 4 * STEP, "job_logs", {"job_id": "job-2"}, **{"error.type": "tool_error"}),
    )

    build_recorded_run_events(LIVE, run)

    actions = {
        log: [
            (e.tool, e.target_id, e.payload["blocked"])
            for e in events_of(run / "episode", log, "action", OWNER)
            if e.tool in JOB_OPERATIONS
        ]
        for log in (SEALED, MIRROR)
    }
    expected = [("submit_job", "bench", False), ("cancel_job", "job-1", False), ("job_logs", None, True)]
    assert actions == {SEALED: expected, MIRROR: expected}


def test_each_change_of_a_jobs_state_is_a_job_event_of_its_owner_on_both_logs_wherever_its_turns_are(
    run: Path,
) -> None:
    _, finished = read_window(run / "episode")
    write_log(
        run,
        "jobsvc",
        state_line(finished - 3 * STEP, JobState.PENDING),
        state_line(finished - 2 * STEP, JobState.RUNNING, "node-1"),
        state_line(finished - STEP, JobState.SUCCEEDED, "node-1"),
    )

    build_recorded_run_events(LIVE, run)

    jobs = {
        log: [
            (e.target_id, e.payload["state"], e.payload["node"])
            for e in events_of(run / "episode", log, "job", OWNER)
        ]
        for log in (SEALED, MIRROR)
    }
    expected = [("job-1", "pending", None), ("job-1", "running", "node-1"), ("job-1", "succeeded", "node-1")]
    assert (jobs, read_unattributed_reasons(run)) == ({SEALED: expected, MIRROR: expected}, [])


def test_a_nodes_process_facts_are_process_events_of_the_jobs_owner_on_the_sealed_log_alone(
    run: Path,
) -> None:
    started, _ = read_window(run / "episode")
    write_log(
        run,
        "node-1",
        fact_line(started, "start", argv=["python", "bench.py"]),
        fact_line(started + STEP, "exit", exit_code=-9),
    )

    build_recorded_run_events(LIVE, run)

    sealed = [e.payload for e in events_of(run / "episode", SEALED, "process", OWNER)]
    assert sealed == [
        {
            "node": "node-1",
            "phase": "start",
            "job_id": "job-1",
            "owner": OWNER,
            "argv": ["python", "bench.py"],
        },
        {"node": "node-1", "phase": "exit", "job_id": "job-1", "owner": OWNER, "exit_code": -9},
    ]
    assert events_of(run / "episode", MIRROR, "process") == []
