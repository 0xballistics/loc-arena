"""What the job service's tests share: a stand-in authority, nodes the test ends jobs on, and a clock."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import SecretStr

from meridian_jobsvc.authority import JobIdentity
from meridian_jobsvc.errors import CredentialRefused
from meridian_jobsvc.fleet import NodeUnavailable, ProcessOutput, ProcessState, ProcessStatus
from meridian_jobsvc.models import JobRequest
from meridian_jobsvc.service import JobService
from meridian_jobsvc.types import Node

OWNER, OTHER = "raj", "lena"
CREDENTIAL = "raj's credential"
SECRET = "a secret of the job's own on the notes service"


@dataclass
class FakeIssuer:
    """An authority that gives each job or schedule a principal of its own and a secret, unless it refuses.

    ``meanwhile`` runs while it answers, as the service's background pass may.
    """

    refuses: bool = False
    asked: list[tuple[str | None, str, str, str]] = field(default_factory=list)
    meanwhile: Callable[[], None] | None = None

    def issue(self, credential: str | None, operation: str, reference: str, purpose: str) -> JobIdentity:
        self.asked.append((credential, operation, reference, purpose))
        if self.meanwhile is not None:
            self.meanwhile()
        if self.refuses:
            raise CredentialRefused("nothing you hold may be given to a job")
        return JobIdentity(principal=f"principal-of-{reference}", secrets={"notes": SecretStr(SECRET)})


@dataclass
class FakeDaemon:
    """A node whose job's process runs until the test ends it."""

    node: Node
    job_id: str | None = None
    state: ProcessState = ProcessState.IDLE
    exit_code: int | None = None
    stdout: str = ""
    answers: bool = True
    failures: list[Exception] = field(default_factory=list)  # raised by its next calls, one each
    started: list[tuple[str, list[str], str]] = field(default_factory=list)
    environments: list[dict[str, str]] = field(default_factory=list)
    attempts: list[int] = field(default_factory=list)

    def start(  # noqa: PLR0913 - the protocol's
        self,
        job_id: str,
        command: list[str],
        owner: str,
        environment: Mapping[str, SecretStr],
        attempt: int,
    ) -> None:
        self._answer()
        self.job_id, self.state = job_id, ProcessState.RUNNING
        self.started.append((job_id, command, owner))
        self.environments.append({name: secret.get_secret_value() for name, secret in environment.items()})
        self.attempts.append(attempt)

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
        if self.failures:
            raise self.failures.pop()
        if not self.answers:
            raise NodeUnavailable(f"{self.node.node_id} does not answer")


@dataclass
class Clock:
    """The service's clock, which the test moves on from a known start."""

    now: datetime = datetime(2026, 10, 22, 9, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def job(name: str = "bench", **fields: Any) -> JobRequest:  # noqa: ANN401 - any field of a job
    return JobRequest.model_validate({"name": name, "command": ["python", "bench.py"], **fields})


def cluster(
    *nodes: Node,
    issuer: FakeIssuer | None = None,
    **options: Any,  # noqa: ANN401 - the service's own options: its policy, teams, admission and clock
) -> tuple[JobService, list[FakeDaemon], list[dict[str, object]]]:
    """A service over a fake of each of ``nodes`` (one of 2 cpus and 2048 MB by default), and its record."""
    daemons = [FakeDaemon(node) for node in nodes or (Node("node-1", cpu_capacity=2, memory_mb=2048),)]
    records: list[dict[str, object]] = []
    service = JobService(
        daemons,
        lambda line: records.append(json.loads(line)["job"]),
        issuer or FakeIssuer(),
        **options,
    )
    return service, daemons, records
