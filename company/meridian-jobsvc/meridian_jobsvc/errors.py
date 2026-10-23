"""Jobsvc-specific errors, extending the shared Meridian hierarchy.

The cluster service raises these so callers can tell a missing-job fault from an ownership or job-state
fault, a dependency fault, and a scheduling, drain, quota, or spec-syntax fault. They all derive from
:class:`meridian_common.errors.MeridianError`, so a generic handler still catches them, while the stable
``code`` keys alerts on the specific stage.
"""

from __future__ import annotations

from meridian_common.errors import AuthError, CapacityError, MeridianError, NotFoundError, ValidationError


class JobNotFound(NotFoundError):  # noqa: N818 - reads as a state, not an Error type
    """A referenced job id is not known to the service."""

    code = "jobsvc.job_not_found"


class NotJobOwner(AuthError):  # noqa: N818 - reads as a state, not an Error type
    """The caller asked to change a job it does not own."""

    code = "jobsvc.not_owner"


class JobStateConflict(MeridianError):  # noqa: N818 - reads as a state, not an Error type
    """The job is in a state that does not allow the change, such as cancelling a job that has ended."""

    code = "jobsvc.job_state"


class DependencyError(ValidationError):
    """A job's ``depends_on`` set is unsatisfiable: an unknown dependency or a dependency cycle."""

    code = "jobsvc.dependency"


class SchedulingError(CapacityError):
    """A job cannot be placed because its resource ask exceeds every node's total capacity."""

    code = "jobsvc.scheduling"


class DrainError(MeridianError):
    """A node lifecycle operation (cordon, drain, rebalance) was asked to act on an unknown node."""

    code = "jobsvc.drain"


class QuotaExceeded(CapacityError):  # noqa: N818 - reads as a state, not an Error type
    """A charge would push an identity's spend past its configured quota."""

    code = "jobsvc.quota"


class SpecSyntaxError(ValidationError):
    """A job-spec DSL string was malformed and could not be parsed into a :class:`JobSpec`."""

    code = "jobsvc.spec_syntax"
