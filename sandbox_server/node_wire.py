"""What the job service and a node's daemon say to each other: the paths and their bodies.

The reply bodies carry the same field names the job service's fleet protocol reads (``job_id``, ``state``,
``exit_code``; ``job_id``, ``stdout``, ``stderr``), so the engine's daemon client validates them straight
into that protocol's types.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, SecretStr

START_PATH = "/jobs"
STATUS_PATH = "/status"
OUTPUT_PATH = "/output"
STOP_PATH = "/stop"
RESET_PATH = "/reset"


class StartJob(BaseModel):
    """Start ``job_id``'s command as the node's one process, run by ``owner`` in the node's workspace."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    job_id: str = Field(min_length=1, description="The job the process belongs to.")
    argv: list[str] = Field(min_length=1, description="The program and its arguments.")
    owner: str = Field(min_length=1, description="The principal whose job this is, for the process fact.")
    environment: dict[str, SecretStr] = Field(
        description="Variables the process gets: the job's own credentials, never written in a fact.",
    )


class NodeProcessState(StrEnum):
    """Where the node's job is: none since the last reset, its process running, or ended."""

    IDLE = "idle"
    RUNNING = "running"
    EXITED = "exited"


class NodeProcessStatus(BaseModel):
    """What the daemon reports: the job it holds since its last reset, and how its process is."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    job_id: str | None
    state: NodeProcessState
    exit_code: int | None = None  # once it exited; negative for the signal that ended it


class NodeProcessOutput(BaseModel):
    """The tails of the node's job's stdout and stderr."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    job_id: str | None
    stdout: str
    stderr: str
