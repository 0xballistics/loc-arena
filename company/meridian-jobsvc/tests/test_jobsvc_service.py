from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

import pytest
from pydantic import SecretStr

from meridian_common.jobclient.models import JobState
from meridian_jobsvc.authority import JobIdentity
from meridian_jobsvc.errors import (
    CredentialRefused,
    DependencyError,
    JobStateConflict,
    NotJobOwner,
    SchedulingError,
)
from meridian_jobsvc.fleet import NodeUnavailable, ProcessOutput, ProcessState, ProcessStatus
from meridian_jobsvc.service import JobRequest, JobService
from meridian_jobsvc.types import Node

OWNER, OTHER = "raj", "lena"
CREDENTIAL = "raj's credential"
SECRET = "a secret of the job's own on the notes service"


@dataclass
class FakeIssuer:
    """An authority that gives each job a principal of its own and one secret, unless told to refuse.

    ``meanwhile`` runs while it answers, as the service's background pass may.
    """

    refuses: bool = False
    asked: list[tuple[str | None, str, str]] = field(default_factory=list)
    meanwhile: Callable[[], None] | None = None

    def issue(self, credential: str | None, job_id: str, purpose: str) -> JobIdentity:
        self.asked.append((credential, job_id, purpose))
        if self.meanwhile is not None:
            self.meanwhile()
        if self.refuses:
            raise CredentialRefused("nothing you hold may be given to a job")
        return JobIdentity(principal=f"principal-of-{job_id}", secrets={"notes": SecretStr(SECRET)})


@dataclass
class FakeDaemon:
    """A node whose job's process runs until the test ends it."""

    node: Node
    job_id: str | None = None
    state: ProcessState = ProcessState.IDLE
    exit_code: int | None = None
    stdout: str = ""
    answers: bool = True
    started: list[tuple[str, list[str], str]] = field(default_factory=list)
    environments: list[dict[str, str]] = field(default_factory=list)

    def start(
        self,
        job_id: str,
        command: list[str],
        owner: str,
        environment: Mapping[str, SecretStr],
    ) -> None:
        self._answer()
        self.job_id, self.state = job_id, ProcessState.RUNNING
        self.started.append((job_id, command, owner))
        self.environments.append({name: secret.get_secret_value() for name, secret in environment.items()})

    def status(self) -> ProcessStatus:
        self._answer()
        return ProcessStatus(job_id=self.job_id, state=self.state, exit_code=self.exit_code)

    def output(self) -> ProcessOutput:
        self._answer()
        return ProcessOutput(job_id=self.job_id, stdout=self.stdout, stderr="")

    def stop(self) -> None:
        self._answer()
        self.exit(-9)

    def reset(self) -> None:
        self._answer()
        self.job_id, self.state, self.exit_code, self.stdout = None, ProcessState.IDLE, None, ""

    def exit(self, code: int, stdout: str = "") -> None:
        self.state, self.exit_code, self.stdout = ProcessState.EXITED, code, self.stdout + stdout

    def _answer(self) -> None:
        if not self.answers:
            raise NodeUnavailable(f"{self.node.node_id} does not answer")


def job(name: str = "bench", **fields: Any) -> JobRequest:  # noqa: ANN401 - any field of a job
    return JobRequest.model_validate({"name": name, "command": ["python", "bench.py"], **fields})


def cluster(
    *nodes: Node,
    issuer: FakeIssuer | None = None,
) -> tuple[JobService, list[FakeDaemon], list[dict[str, object]]]:
    daemons = [FakeDaemon(node) for node in nodes or (Node("node-1", cpu_capacity=2, memory_mb=2048),)]
    records: list[dict[str, object]] = []
    service = JobService(
        daemons,
        lambda line: records.append(json.loads(line)["job"]),
        issuer or FakeIssuer(),
    )
    return service, daemons, records


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
    ],
    ids=["more cpus than any node", "more memory than any node", "a dependency that is no job"],
)
def test_a_job_that_can_never_start_is_refused_when_submitted(
    fields: dict[str, Any],
    refusal: type[Exception],
) -> None:
    service, _, records = cluster()

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


def test_a_job_runs_as_its_own_identity_whose_secrets_reach_its_process_and_no_record() -> None:
    issuer = FakeIssuer()
    lines: list[str] = []
    daemon = FakeDaemon(Node("node-1", cpu_capacity=2, memory_mb=2048))
    service = JobService([daemon], lines.append, issuer)

    submitted = service.submit(job("bench"), OWNER, CREDENTIAL)
    service.reconcile()

    assert (submitted.owner, submitted.principal) == (OWNER, "principal-of-job-1")
    assert issuer.asked == [(CREDENTIAL, "job-1", "bench")]
    assert daemon.environments == [{"MERIDIAN_NOTES_TOKEN": SECRET}]
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
    [(False, JobState.RUNNING, [{"MERIDIAN_NOTES_TOKEN": SECRET}]), (True, JobState.FAILED, [])],
    ids=["ready to start", "waiting on a failed job"],
)
def test_a_pass_while_a_job_is_issued_its_identity_leaves_it_to_a_pass_after(
    waits_on_a_failed_job: bool,
    then: JobState,
    started_with: list[dict[str, str]],
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
