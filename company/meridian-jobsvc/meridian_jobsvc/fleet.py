"""What the job service needs of its fleet: on each node, a daemon that runs the node's one job as a process.

The service runs no job itself. A deployment hands it a client of each node's daemon, which starts the job
it is given in the node's workspace with the environment it is given (the job's own credentials), says whether
the job's process still runs and how it ended, serves the tails of its output, stops it when asked, and resets
the node for the next job. The service reads the daemons on each of its passes, so its view of a job can lag
the process.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, ConfigDict, SecretStr

from meridian_jobsvc.types import Node


class NodeUnavailable(RuntimeError):  # noqa: N818 - reads as a state, not an Error type
    """A node's daemon did not answer, or refused: the service tries again on its next pass."""


class ProcessState(StrEnum):
    """Where a node's job is: none given since the node's last reset, its process running, or ended."""

    IDLE = "idle"
    RUNNING = "running"
    EXITED = "exited"


class ProcessStatus(BaseModel):
    """What a node's daemon reports: the job it was given since its last reset, and how its process is."""

    model_config = ConfigDict(frozen=True)

    job_id: str | None
    state: ProcessState
    exit_code: int | None = None  # once it exited; negative for the signal that ended it


class ProcessOutput(BaseModel):
    """The tails of the stdout and stderr of a node's job."""

    model_config = ConfigDict(frozen=True)

    job_id: str | None
    stdout: str
    stderr: str


class NodeDaemon(Protocol):
    """The daemon on one node; each call raises ``NodeUnavailable`` when the daemon does not answer."""

    @property
    def node(self) -> Node:
        """The node: its id, and the cpu and memory a job placed on it gets."""
        ...

    def start(
        self,
        job_id: str,
        command: list[str],
        owner: str,
        environment: Mapping[str, SecretStr],
    ) -> None:
        """Start ``command`` as the process of ``owner``'s job ``job_id``, on a node that holds no job.

        The process gets ``environment``, the job's credentials, which the daemon never writes down.
        """
        ...

    def status(self) -> ProcessStatus:
        """The node's job and how its process is."""
        ...

    def output(self) -> ProcessOutput:
        """The tails of the node's job's stdout and stderr."""
        ...

    def stop(self) -> None:
        """End the node's job's process and every process it started."""
        ...

    def reset(self) -> None:
        """End whatever runs on the node and empty its workspace, so it can take the next job."""
        ...
