from __future__ import annotations

import threading
import time
from typing import Any

import pytest
from jobsvc_support import CREDENTIAL, OTHER, OWNER, SECRET, FakeDaemon, FakeIssuer, cluster, job
from pydantic import SecretStr, ValidationError

from meridian_common.jobclient.models import JobState
from meridian_jobsvc.errors import (
    CredentialRefused,
    DependencyError,
    JobStateConflict,
    NotJobOwner,
    PriorityError,
    SchedulingError,
    SubmissionRefused,
)
from meridian_jobsvc.fleet import ProcessState
from meridian_jobsvc.service import ClusterPolicy, JobService
from meridian_jobsvc.types import Node


def test_a_job_runs_on_the_first_free_node_it_fits_and_succeeds_when_its_process_exits_0() -> None:
    service, (small, large), _ = cluster(Node("node-1", 1, 1024), Node("node-2", 4, 8192))
    submitted = service.submit(job(cpu=2, memory_mb=4096), OWNER, CREDENTIAL)

    service.reconcile()
    running = service.get_job(submitted.id)
    large.exit(0)
    service.reconcile()

    ended = service.get_job(submitted.id)
    assert (small.started, large.started) == ([], [(submitted.id, ["python", "bench.py"], OWNER)])
    assert (running.state, running.node) == (JobState.RUNNING, "node-2")
    assert (ended.state, ended.exit_code, large.state) == (JobState.SUCCEEDED, 0, ProcessState.IDLE)


def test_a_job_whose_process_fails_has_failed_and_so_has_every_job_waiting_on_it() -> None:
    service, (node,), _ = cluster()
    first = service.submit(job("prepare"), OWNER, CREDENTIAL)
    then = service.submit(job("train", depends_on=[first.id]), OWNER, CREDENTIAL)

    service.reconcile()
    node.exit(3)
    service.reconcile()

    states = [(found.state, found.exit_code) for found in service.list_jobs()]
    assert states == [(JobState.FAILED, 3), (JobState.FAILED, None)]
    assert [started for started, _, _ in node.started] == [first.id]  # the dependent never ran
    assert service.get_job(then.id).message == "a job it depends on did not succeed"


def test_a_node_holds_one_job_at_a_time_and_ready_jobs_start_by_priority_then_in_order() -> None:
    service, (node,), _ = cluster()
    early = service.submit(job("early"), OWNER, CREDENTIAL)
    late = service.submit(job("late"), OWNER, CREDENTIAL)
    urgent = service.submit(job("urgent", priority=5), OWNER, CREDENTIAL)

    service.reconcile()
    node.exit(0)
    service.reconcile()
    node.exit(0)
    service.reconcile()

    assert [started for started, _, _ in node.started] == [urgent.id, early.id, late.id]
    assert service.get_job(late.id).state is JobState.RUNNING


@pytest.mark.parametrize(
    ("fields", "refusal"),
    [
        ({"cpu": 3}, SchedulingError),
        ({"memory_mb": 4096}, SchedulingError),
        ({"depends_on": ["job-9"]}, DependencyError),
        ({"priority": 11}, PriorityError),
    ],
    ids=[
        "more cpus than any node",
        "more memory than any node",
        "a dependency that is no job",
        "above the highest priority",
    ],
)
def test_a_job_that_can_never_start_is_refused_when_submitted(
    fields: dict[str, Any],
    refusal: type[Exception],
) -> None:
    service, _, records = cluster(policy=ClusterPolicy(max_priority=10))

    with pytest.raises(refusal):
        service.submit(job(**fields), OWNER, CREDENTIAL)

    assert (service.list_jobs(), records) == ([], [])


def test_a_cancelled_running_job_is_cancelling_until_its_process_is_gone_then_cancelled() -> None:
    service, (node,), records = cluster()
    submitted = service.submit(job(), OWNER, CREDENTIAL)
    service.reconcile()

    cancelling = service.cancel(submitted.id, OWNER)
    still_running = node.state
    service.reconcile()  # stops the process
    service.reconcile()  # sees it gone

    assert (cancelling.state, still_running) == (JobState.CANCELLING, ProcessState.RUNNING)
    assert [record["state"] for record in records] == ["pending", "running", "cancelling", "cancelled"]
    assert (service.get_job(submitted.id).exit_code, node.state) == (-9, ProcessState.IDLE)


def test_a_waiting_job_cancelled_never_starts() -> None:
    service, (node,), _ = cluster()
    busy = service.submit(job("busy"), OWNER, CREDENTIAL)
    waiting = service.submit(job("waiting"), OWNER, CREDENTIAL)
    service.reconcile()

    service.cancel(waiting.id, OWNER)
    node.exit(0)
    service.reconcile()

    assert service.get_job(waiting.id).state is JobState.CANCELLED
    assert [started for started, _, _ in node.started] == [busy.id]


def test_only_a_jobs_owner_cancels_it_and_never_once_it_has_ended() -> None:
    service, (node,), _ = cluster()
    submitted = service.submit(job(), OWNER, CREDENTIAL)
    service.reconcile()

    with pytest.raises(NotJobOwner):
        service.cancel(submitted.id, OTHER)
    node.exit(0)
    service.reconcile()
    with pytest.raises(JobStateConflict):
        service.cancel(submitted.id, OWNER)


def test_a_jobs_logs_are_read_from_its_node_while_it_runs_and_kept_once_it_has_ended() -> None:
    service, (node,), _ = cluster()
    submitted = service.submit(job(), OWNER, CREDENTIAL)
    service.reconcile()
    node.stdout = "step 1\n"

    live = service.logs(submitted.id).stdout
    node.exit(0, "step 2\n")
    service.reconcile()

    assert (live, service.logs(submitted.id).stdout, node.stdout) == ("step 1\n", "step 1\nstep 2\n", "")


def test_a_job_whose_node_lost_it_has_failed() -> None:
    service, (node,), _ = cluster()
    submitted = service.submit(job(), OWNER, CREDENTIAL)
    service.reconcile()

    node.reset()  # its daemon restarted: it holds no job
    service.reconcile()

    lost = service.get_job(submitted.id)
    assert (lost.state, lost.exit_code, lost.message) == (JobState.FAILED, None, "its node lost it")


def test_a_job_its_node_did_not_start_has_failed_and_a_quiet_node_is_passed_over() -> None:
    service, (node,), _ = cluster()
    node.answers = False
    submitted = service.submit(job(), OWNER, CREDENTIAL)

    service.reconcile()
    service.reconcile()  # nothing to follow on the quiet node, and nothing more to start

    failed = service.get_job(submitted.id)
    assert (failed.state, failed.message) == (
        JobState.FAILED,
        "its node did not start it: node-1 does not answer",
    )


def test_a_pass_over_the_fleet_that_fails_does_not_stop_the_passes_after_it() -> None:
    service, (node,), _ = cluster()
    submitted = service.submit(job(), OWNER, CREDENTIAL)
    service.reconcile()
    node.exit(0)
    node.failures.append(RuntimeError("an answer no pass expected"))

    threading.Thread(target=service.run, args=(0.01,), daemon=True).start()
    deadline = time.monotonic() + 5
    while service.get_job(submitted.id).state is JobState.RUNNING and time.monotonic() < deadline:
        time.sleep(0.01)

    assert service.get_job(submitted.id).state is JobState.SUCCEEDED


def test_a_job_runs_as_its_own_identity_whose_secrets_reach_its_process_and_no_record() -> None:
    issuer = FakeIssuer()
    lines: list[str] = []
    daemon = FakeDaemon(Node("node-1", cpu_capacity=2, memory_mb=2048))
    service = JobService([daemon], lines.append, issuer)

    submitted = service.submit(job("bench"), OWNER, CREDENTIAL)
    service.reconcile()

    assert (submitted.owner, submitted.principal) == (OWNER, "principal-of-job-1")
    assert issuer.asked == [(CREDENTIAL, "submit_job", "job-1", "bench")]
    assert daemon.environments == [{"MERIDIAN_NOTES_TOKEN": SecretStr(SECRET)}]
    assert SECRET not in "".join(lines) + service.get_job(submitted.id).model_dump_json()


def test_a_job_refused_its_identity_is_never_queued_and_the_next_one_runs() -> None:
    issuer = FakeIssuer(refuses=True)
    service, (node,), records = cluster(issuer=issuer)

    with pytest.raises(CredentialRefused):
        service.submit(job("refused"), OWNER, CREDENTIAL)
    issuer.refuses = False
    accepted = service.submit(job("accepted"), OWNER, CREDENTIAL)
    service.reconcile()

    assert [found.id for found in service.list_jobs()] == [accepted.id]
    assert [started for started, _, _ in node.started] == [accepted.id]
    assert [record["name"] for record in records] == ["accepted", "accepted"]


@pytest.mark.parametrize(
    ("waits_on_a_failed_job", "then", "started_with"),
    [(False, JobState.RUNNING, [{"MERIDIAN_NOTES_TOKEN": SecretStr(SECRET)}]), (True, JobState.FAILED, [])],
    ids=["ready to start", "waiting on a failed job"],
)
def test_a_pass_while_a_job_is_issued_its_identity_leaves_it_to_a_pass_after(
    waits_on_a_failed_job: bool,
    then: JobState,
    started_with: list[dict[str, SecretStr]],
) -> None:
    issuer = FakeIssuer()
    service, (node,), _ = cluster(issuer=issuer)
    depends_on = []
    if waits_on_a_failed_job:
        depends_on = [service.submit(job("prepare"), OWNER, CREDENTIAL).id]
        service.reconcile()
        node.exit(1)
        service.reconcile()

    issuer.meanwhile = service.reconcile  # the background pass, while the authority answers
    submitted = service.submit(job("train", depends_on=depends_on), OWNER, CREDENTIAL)
    issuer.meanwhile = None
    service.reconcile()

    assert (service.get_job(submitted.id).state, node.environments[len(depends_on) :]) == (then, started_with)


def test_a_jobs_own_variables_reach_its_process_beside_its_credentials() -> None:
    service, (node,), _ = cluster()

    service.submit(job(environment={"BATCH_SIZE": "8"}), OWNER, CREDENTIAL)
    service.reconcile()

    assert node.environments == [{"BATCH_SIZE": SecretStr("8"), "MERIDIAN_NOTES_TOKEN": SecretStr(SECRET)}]


@pytest.mark.parametrize(
    "name",
    ["MERIDIAN_NOTES_TOKEN", "MERIDIAN_GATEWAY_URL", "HOME", "PATH", "TMPDIR", "1ST"],
    ids=["a credential's", "the gateway's", "HOME", "PATH", "TMPDIR", "no variable name"],
)
def test_a_job_naming_a_variable_the_cluster_sets_or_no_variable_is_refused(name: str) -> None:
    with pytest.raises(ValidationError):
        job(environment={name: "set by the job"})


def test_the_deployment_is_asked_of_the_agent_a_job_works_for_and_its_refusal_queues_nothing() -> None:
    asked: list[str] = []

    def admit(submitter: str) -> None:
        asked.append(submitter)
        if submitter == OTHER:
            raise SubmissionRefused("submit_job requires submit_job")

    service, _, records = cluster(admit_submitter=admit)
    parent = service.submit(job("parent"), OWNER, CREDENTIAL)
    service.submit(job("child"), parent.principal, "the parent's own credential")

    with pytest.raises(SubmissionRefused):
        service.submit(job("refused"), OTHER, CREDENTIAL)

    assert (asked, [record["name"] for record in records]) == ([OWNER, OWNER, OTHER], ["parent", "child"])
