"""Jobsvc-specific errors, extending the shared Meridian hierarchy.

The cluster service raises these so callers can tell a missing job, schedule or node from an ownership or
job-state fault, a dependency or priority fault, a submitter the deployment refuses, a fault issuing a job its
own credential, and a scheduling, drain, quota, or spec-syntax fault. They all derive from
:class:`meridian_common.errors.MeridianError`, so a generic handler still catches them, while the stable
``code`` keys alerts on the specific stage.
"""

from __future__ import annotations

from meridian_common.errors import (
    AuthError,
    CapacityError,
    MeridianError,
    NotFoundError,
    TransportError,
    ValidationError,
)


class JobNotFound(NotFoundError):  # noqa: N818 - reads as a state, not an Error type
    """A referenced job id is not known to the service."""

    code = "jobsvc.job_not_found"


class ScheduleNotFound(NotFoundError):  # noqa: N818 - reads as a state, not an Error type
    """A referenced schedule id is not known to the service."""

    code = "jobsvc.schedule_not_found"


class NodeNotFound(NotFoundError):  # noqa: N818 - reads as a state, not an Error type
    """A referenced node is not one of the cluster's."""

    code = "jobsvc.node_not_found"


class NotJobOwner(AuthError):  # noqa: N818 - reads as a state, not an Error type
    """The caller asked to change a job, or a schedule, it does not own."""

    code = "jobsvc.not_owner"


class SubmissionRefused(AuthError):  # noqa: N818 - reads as a state, not an Error type
    """The deployment refused the submitter, beside what the central authority allows: nothing was queued."""

    code = "jobsvc.submission_refused"


class CredentialRefused(AuthError):  # noqa: N818 - reads as a state, not an Error type
    """The central authority refused a job its own credential, so it was not queued."""

    code = "jobsvc.credential_refused"


class CredentialUnavailable(TransportError):  # noqa: N818 - reads as a state, not an Error type
    """The central authority did not answer when asked for a job's credential, so the job was not queued."""

    code = "jobsvc.credential_unavailable"


class JobStateConflict(MeridianError):  # noqa: N818 - reads as a state, not an Error type
    """The job is in a state that does not allow the change, such as cancelling a job that has ended."""

    code = "jobsvc.job_state"


class DependencyError(ValidationError):
    """A job's ``depends_on`` set is unsatisfiable: an unknown dependency or a dependency cycle."""

    code = "jobsvc.dependency"


class PriorityError(ValidationError):
    """A job asked for a priority above the highest the cluster allows."""

    code = "jobsvc.priority"


class SchedulingError(CapacityError):
    """A job cannot be placed because its resource ask exceeds every node's total capacity."""

    code = "jobsvc.scheduling"


class DrainError(MeridianError):
    """A node lifecycle operation (cordon, drain, rebalance) was asked to act on an unknown node."""

    code = "jobsvc.drain"


class QuotaExceeded(CapacityError):  # noqa: N818 - reads as a state, not an Error type
    """A charge would push an account's spend past its configured quota."""

    code = "jobsvc.quota"


class SpecSyntaxError(ValidationError):
    """A job-spec DSL string was malformed and could not be parsed into a :class:`JobSpec`."""

    code = "jobsvc.spec_syntax"
