"""The job service: Meridian's cluster, which keeps track of every job and gets it run on a node of its fleet.

``JobService`` admits each job to the dependency-aware priority queue. Each ``reconcile`` pass starts the
ready ones, in the queue's order, on the first free node whose cpu and memory fit; a node holds one job at a
time. The service runs nothing itself: a daemon on each node runs the job's command as a process
(``meridian_jobsvc.fleet``), and each pass reads the daemons, so a job's state follows its process, never a
poll of the service. A job runs until its process exits, then has ``succeeded`` on exit code 0 and
``failed`` otherwise; a cancelled job is ``cancelling`` until its process is gone, then ``cancelled``; a job
waiting on one that did not succeed fails. Only a job's owner may cancel it. Each change of a job's state is
one JSON line of the service's record, holding the job as it is then.

Every job runs as an identity of its own, never as its submitter: before a job is queued, the central
authority issues it one from the credential its submitter presented (``meridian_jobsvc.authority``). Its
process gets each of its secrets in an environment variable (``credential_variable``), and the service keeps
them only until the process has them; no record or answer holds one.
"""

from __future__ import annotations

import contextlib
import json
import threading
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, SecretStr

from meridian_common.jobclient.models import JobSpec, JobState
from meridian_jobsvc.authority import JobIdentity, WorkloadIssuer
from meridian_jobsvc.errors import JobNotFound, JobStateConflict, NotJobOwner, SchedulingError
from meridian_jobsvc.fleet import NodeDaemon, NodeUnavailable, ProcessOutput, ProcessState
from meridian_jobsvc.queue.jobqueue import JobQueue
from meridian_jobsvc.types import Node

MAX_COMMAND_CHARACTERS = 32_000  # every argument of a command together
_PLACED = (JobState.RUNNING, JobState.CANCELLING)


def _limit_command(command: list[str]) -> list[str]:
    if sum(map(len, command)) > MAX_COMMAND_CHARACTERS:
        raise ValueError(f"the command's arguments hold more than {MAX_COMMAND_CHARACTERS} characters")
    return command


JobName = Annotated[str, Field(min_length=1, max_length=200, description="what the job does, for people")]
Command = Annotated[
    list[str],
    Field(min_length=1, description="the program and its arguments, run in the job's own empty workspace"),
    AfterValidator(_limit_command),
]
Cpu = Annotated[float, Field(gt=0, description="the cpus it needs: its node gives it at least that many")]
MemoryMb = Annotated[int, Field(gt=0, description="the memory it needs, in MB")]
Priority = Annotated[int, Field(description="among jobs ready to start, a higher one starts first")]
DependsOn = Annotated[list[str], Field(description="ids of jobs that must succeed before this one starts")]
Labels = Annotated[dict[str, str], Field(max_length=32, description="labels for people and tools")]


class JobRequest(BaseModel):
    """A job to run: its command, the cpu and memory it needs, its priority, what it waits for, its labels."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: JobName
    command: Command
    cpu: Cpu = 1.0
    memory_mb: MemoryMb = 512
    priority: Priority = 0
    depends_on: DependsOn = Field(default_factory=list)
    labels: Labels = Field(default_factory=dict)


class Job(BaseModel):
    """One job: what it asked for, whose it is, the identity it runs as, where it was placed, how it went."""

    id: str
    name: str
    command: list[str]
    cpu: float
    memory_mb: int
    priority: int
    depends_on: list[str]
    labels: dict[str, str]
    owner: str
    principal: str  # the job's own identity, which its calls carry: never its owner's
    state: JobState
    node: str | None = None
    submitted_at: datetime
    started_at: datetime | None = None
    ended_at: datetime | None = None
    exit_code: int | None = None  # negative for the signal that ended its process
    message: str = ""


class Jobs(BaseModel):
    """Every job, oldest first."""

    jobs: list[Job]


class JobLogs(BaseModel):
    """The tails of a job's stdout and stderr: empty for a job that never ran."""

    job_id: str
    stdout: str
    stderr: str


class JobService:
    """Every job by id, the queue of those waiting to start, and the fleet that runs them."""

    def __init__(
        self,
        daemons: Sequence[NodeDaemon],
        write_record: Callable[[str], None],
        issuer: WorkloadIssuer,
    ) -> None:
        """Run jobs on the nodes of ``daemons``, first free one first, each as an identity from ``issuer``.

        Each change of a job's state is written with ``write_record``.
        """
        self._daemons = {daemon.node.node_id: daemon for daemon in daemons}
        self._write_record = write_record
        self._issuer = issuer
        self._queue = JobQueue()
        self._jobs: dict[str, Job] = {}
        self._identities: dict[str, JobIdentity] = {}  # each queued job's, until its process has it
        self._placed: dict[str, str] = {}  # the id of the job on each node that holds one
        self._outputs: dict[str, ProcessOutput] = {}  # each ended job's output, kept once its node is reset
        self._lock = threading.Lock()  # never held while a node's daemon is called
        self._changed = threading.Event()  # a job was submitted or cancelled: the next pass comes at once

    def submit(self, request: JobRequest, owner: str, credential: str | None) -> Job:
        """Queue ``request`` as ``owner``'s job: it starts once its dependencies succeed and a node is free.

        It runs as the identity the authority issues it, under its id and its name, from ``credential``: the
        one ``owner`` presented. Raises ``SchedulingError`` for a job larger than every node,
        ``DependencyError`` for a dependency that is no job, and ``CredentialRefused`` or
        ``CredentialUnavailable`` when it could not be issued its identity; a refused job is not queued.
        """
        spec = JobSpec(
            name=request.name,
            command=request.command,
            cpu=request.cpu,
            memory_mb=request.memory_mb,
            priority=request.priority,
            depends_on=tuple(request.depends_on),
            labels=request.labels,
        )
        if not any(_fits(spec, daemon.node) for daemon in self._daemons.values()):
            raise SchedulingError(
                f"job {request.name!r} asks for {request.cpu:g} cpus and {request.memory_mb} MB, more than "
                "any node of the cluster has",
                path=request.name,
            )
        with self._lock:
            job_id = self._queue.submit(spec)  # no pass places it until it is in the registry, once issued
        try:
            identity = self._issuer.issue(credential, job_id, request.name)
        except Exception:
            with self._lock:
                self._queue.dispatch(job_id)
                self._queue.notify_failed(job_id)
            raise
        with self._lock:
            self._identities[job_id] = identity
            job = Job(
                id=job_id,
                **request.model_dump(),
                owner=owner,
                principal=identity.principal,
                state=JobState.PENDING,
                submitted_at=datetime.now(UTC),
            )
            job = self._record(job)
        self._changed.set()
        return job

    def list_jobs(self) -> list[Job]:
        """Every job, oldest first."""
        with self._lock:
            return list(self._jobs.values())

    def get_job(self, job_id: str) -> Job:
        """The job ``job_id``; raises ``JobNotFound`` if there is none."""
        with self._lock:
            return self._find(job_id)

    def logs(self, job_id: str) -> JobLogs:
        """The tails of the job's output: read from its node while it runs, else as it was when it ended.

        Raises ``NodeUnavailable`` when the node of a running job does not answer.
        """
        with self._lock:
            job = self._find(job_id)
            output = self._outputs.get(job_id)
        if output is None and job.node is not None and job.state in _PLACED:
            live = self._daemons[job.node].output()
            with self._lock:  # the job may have ended meanwhile, and its node taken the next one
                output = self._outputs.get(job_id, live if live.job_id == job_id else None)
        output = output or ProcessOutput(job_id=job_id, stdout="", stderr="")
        return JobLogs(job_id=job_id, stdout=output.stdout, stderr=output.stderr)

    def cancel(self, job_id: str, caller: str) -> Job:
        """Cancel ``caller``'s job: one waiting to start at once, a running one once its process is gone.

        Raises ``NotJobOwner`` for another's job and ``JobStateConflict`` for one that has ended; cancelling a
        job again while its process is ending changes nothing.
        """
        with self._lock:
            job = self._find(job_id)
            if job.owner != caller:
                raise NotJobOwner(f"job {job_id!r} is {job.owner}'s, not yours", path=job_id)
            if job.state is JobState.PENDING:
                self._queue.dispatch(job_id)
                self._queue.notify_failed(job_id)
                self._identities.pop(job_id, None)
                job = self._record(job, state=JobState.CANCELLED, ended_at=datetime.now(UTC))
            elif job.state is JobState.RUNNING:
                job = self._record(job, state=JobState.CANCELLING)
            elif job.state is not JobState.CANCELLING:
                raise JobStateConflict(f"job {job_id!r} has ended: it is {job.state.value}", path=job_id)
        self._changed.set()
        return job

    def reconcile(self) -> None:
        """One pass over the fleet: end each job whose process ended, stop cancelled ones, start ready ones.

        A node whose daemon does not answer is passed over until the next pass.
        """
        for node_id, daemon in self._daemons.items():
            with self._lock:
                job = self._jobs.get(self._placed.get(node_id, ""))
            if job is not None:
                with contextlib.suppress(NodeUnavailable):
                    self._follow(daemon, job)
        for daemon, job, identity in self._place_ready_jobs():
            try:
                daemon.start(job.id, job.command, job.owner, job_environment(identity))
            except NodeUnavailable as error:
                with contextlib.suppress(NodeUnavailable):
                    daemon.reset()  # in case it started the job but the answer was lost
                self._end(job.id, JobState.FAILED, None, f"its node did not start it: {error}")

    def run(self, poll_seconds: float) -> None:
        """Reconcile forever: every ``poll_seconds``, and at once after a job is submitted or cancelled."""
        while True:
            self._changed.clear()
            self.reconcile()
            self._changed.wait(poll_seconds)

    def _follow(self, daemon: NodeDaemon, job: Job) -> None:
        """Stop a cancelled job's process; once it is gone, keep the job's output, end it, free its node."""
        status = daemon.status()
        if status.job_id == job.id and status.state is ProcessState.RUNNING:
            if job.state is JobState.CANCELLING:
                daemon.stop()
            return
        lost = status.job_id != job.id  # its daemon holds no such job: it restarted, and the process is gone
        output = ProcessOutput(job_id=job.id, stdout="", stderr="") if lost else daemon.output()
        daemon.reset()
        with self._lock:
            self._outputs[job.id] = output
        if lost:
            self._end(job.id, JobState.FAILED, None, "its node lost it")
        else:
            state = JobState.SUCCEEDED if status.exit_code == 0 else JobState.FAILED
            self._end(job.id, state, status.exit_code, "")

    def _place_ready_jobs(self) -> list[tuple[NodeDaemon, Job, JobIdentity]]:
        """Fail the jobs waiting on one that failed; place each ready job on the first free node it fits.

        Each placed job comes with its identity, which the service forgets once handed over. A job still being
        issued its identity is not in the registry yet, and is left to a later pass.
        """
        with self._lock:
            for blocked in self._queue.blocked():
                if blocked.job_id not in self._jobs:
                    continue
                self._queue.dispatch(blocked.job_id)
                self._queue.notify_failed(blocked.job_id)
                self._identities.pop(blocked.job_id, None)
                self._record(
                    self._jobs[blocked.job_id],
                    state=JobState.FAILED,
                    ended_at=datetime.now(UTC),
                    message="a job it depends on did not succeed",
                )
            free = [node_id for node_id in self._daemons if node_id not in self._placed]
            placed = []
            for ready in self._queue.ready():
                if ready.job_id not in self._jobs:
                    continue
                fitting = (node_id for node_id in free if _fits(ready.spec, self._daemons[node_id].node))
                node_id = next(fitting, None)
                if node_id is None:
                    continue
                free.remove(node_id)
                self._queue.dispatch(ready.job_id)
                self._placed[node_id] = ready.job_id
                job = self._record(
                    self._jobs[ready.job_id],
                    state=JobState.RUNNING,
                    node=node_id,
                    started_at=datetime.now(UTC),
                )
                placed.append((self._daemons[node_id], job, self._identities.pop(ready.job_id)))
        return placed

    def _end(self, job_id: str, state: JobState, exit_code: int | None, message: str) -> None:
        """Record that a placed job ended in ``state``, release the jobs waiting on it, and free its node.

        A job being cancelled ends ``cancelled``, however its process ended.
        """
        with self._lock:
            job = self._jobs[job_id]
            state = JobState.CANCELLED if job.state is JobState.CANCELLING else state
            if state is JobState.SUCCEEDED:
                self._queue.notify_succeeded(job_id)
            else:
                self._queue.notify_failed(job_id)
            if job.node is not None:
                self._placed.pop(job.node, None)
            self._record(job, state=state, ended_at=datetime.now(UTC), exit_code=exit_code, message=message)

    def _find(self, job_id: str) -> Job:
        if job_id not in self._jobs:
            raise JobNotFound(f"no job {job_id!r}", path=job_id)
        return self._jobs[job_id]

    def _record(self, job: Job, **changes: object) -> Job:
        """Keep ``job`` with ``changes``, and write it on the record; called with the lock held."""
        job = job.model_copy(update=changes)
        self._jobs[job.id] = job
        line = {"wall_ts": datetime.now(UTC).isoformat(), "job": job.model_dump(mode="json")}
        self._write_record(json.dumps(line))
        return job


def credential_variable(service: str) -> str:
    """The environment variable holding a job's secret on ``service``: ``MERIDIAN_NOTES_TOKEN`` for notes."""
    return f"MERIDIAN_{service.upper().replace('-', '_')}_TOKEN"


def job_environment(identity: JobIdentity) -> dict[str, SecretStr]:
    """What a job's process gets of its identity: each secret in its service's variable."""
    return {credential_variable(service): secret for service, secret in identity.secrets.items()}


def _fits(spec: JobSpec, node: Node) -> bool:
    """Whether ``node`` has the cpus and memory ``spec`` asks for."""
    return spec.cpu <= node.cpu_capacity and spec.memory_mb <= node.memory_mb
