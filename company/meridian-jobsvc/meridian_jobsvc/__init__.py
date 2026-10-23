"""meridian-jobsvc: the Meridian cluster and job-orchestration service.

Its :class:`JobService` keeps every job and gets it run as a process on a node of the cluster's fleet: the
dependency-aware priority job queue orders the waiting jobs, each starts on the first free node with its cpu
and memory, and the daemon on that node runs it (``fleet``), under an identity of its own that the central
authority issues it. It is served over HTTP and as MCP tools (``api``), authorized by that central authority,
which the deployment provides (``authority``). The package also holds the capacity-based scheduler and its
autoscaler simulation, the worker runtime that hosts long-lived jobs (heartbeat and checkpoint), node
lifecycle (cordon, drain, rebalance), the spend/quota ledger, and a compact job-spec DSL. Depends only on
meridian-common.
"""

from __future__ import annotations

__version__ = "0.6.0"

from meridian_jobsvc.accountant import Ledger, Quota, Spend
from meridian_jobsvc.authority import JobIdentity, WorkloadIssuer
from meridian_jobsvc.dsl import parse_spec
from meridian_jobsvc.errors import (
    CredentialRefused,
    CredentialUnavailable,
    DependencyError,
    DrainError,
    JobNotFound,
    JobStateConflict,
    NotJobOwner,
    QuotaExceeded,
    SchedulingError,
    SpecSyntaxError,
)
from meridian_jobsvc.fleet import NodeDaemon, NodeUnavailable, ProcessOutput, ProcessState, ProcessStatus
from meridian_jobsvc.lifecycle import NodeLifecycle
from meridian_jobsvc.queue import JobQueue, QueuedJob
from meridian_jobsvc.scheduler import Autoscaler, ScalePolicy, Scheduler
from meridian_jobsvc.service import Job, JobLogs, JobRequest, Jobs, JobService
from meridian_jobsvc.types import Checkpoint, DrainedJob, JobSpec, JobState, JobStatus, Node
from meridian_jobsvc.worker import WorkerRuntime

__all__ = [
    "Autoscaler",
    "Checkpoint",
    "CredentialRefused",
    "CredentialUnavailable",
    "DependencyError",
    "DrainError",
    "DrainedJob",
    "Job",
    "JobIdentity",
    "JobLogs",
    "JobNotFound",
    "JobQueue",
    "JobRequest",
    "JobService",
    "JobSpec",
    "JobState",
    "JobStateConflict",
    "JobStatus",
    "Jobs",
    "Ledger",
    "Node",
    "NodeDaemon",
    "NodeLifecycle",
    "NodeUnavailable",
    "NotJobOwner",
    "ProcessOutput",
    "ProcessState",
    "ProcessStatus",
    "Quota",
    "QueuedJob",
    "QuotaExceeded",
    "ScalePolicy",
    "Scheduler",
    "SchedulingError",
    "Spend",
    "SpecSyntaxError",
    "WorkerRuntime",
    "WorkloadIssuer",
    "__version__",
    "parse_spec",
]
