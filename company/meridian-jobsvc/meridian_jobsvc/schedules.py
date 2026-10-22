"""Schedules: a job the cluster submits again and again, on a cron expression or every so many seconds.

A schedule's runs are ordinary jobs, each linked to it, and all run under the one identity the schedule was
issued when it was created, as a real cron job runs under one service account. A run starts when one is due
and the schedule's last run has ended; a due time that finds the last run still waiting or running is skipped.
Deleting a schedule stops its future runs; the runs it already started carry on.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated, Self

from croniter import croniter
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from meridian_jobsvc.models import JobRequest

CRON_FIELDS = 5  # minute, hour, day of month, month, day of week


def _check_cron(expression: str) -> str:
    if len(expression.split()) != CRON_FIELDS or not croniter.is_valid(expression):
        raise ValueError(f"{expression!r} is not a cron expression of five fields")
    return expression


Cron = Annotated[
    str,
    Field(description="when runs start, as five cron fields (minute hour day month weekday), in UTC"),
    AfterValidator(_check_cron),
]
IntervalSeconds = Annotated[
    int,
    Field(ge=1, description="seconds between runs, the first one after creation"),
]


class ScheduleRequest(BaseModel):
    """A schedule to create: its job, as ``POST /jobs`` takes one, and when its runs start."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    cron: Cron | None = None
    interval_seconds: IntervalSeconds | None = None
    job: JobRequest

    @model_validator(mode="after")
    def _timed_one_way(self) -> Self:
        if (self.cron is None) == (self.interval_seconds is None):
            raise ValueError("a schedule takes either a cron expression or an interval in seconds")
        return self

    def next_run(self, created_at: datetime, after: datetime) -> datetime:
        """When the next run is due after ``after``, for a schedule created at ``created_at``."""
        if self.cron is not None:
            return croniter(self.cron, after).get_next(datetime)
        assert self.interval_seconds is not None  # a schedule is timed one way or the other, as validated
        intervals = (after - created_at).total_seconds() // self.interval_seconds + 1
        return created_at + timedelta(seconds=self.interval_seconds * intervals)


class Schedule(ScheduleRequest):
    """One schedule: whose it is, the identity its runs share, when its next run is due, and its runs."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    owner: str
    principal: str  # the identity every run runs as: never its owner's
    created_at: datetime
    next_run_at: datetime | None  # none once deleted
    deleted_at: datetime | None = None
    runs: list[str] = Field(default_factory=list)  # its jobs' ids, oldest first


class Schedules(BaseModel):
    """Every schedule, oldest first, deleted ones included."""

    schedules: list[Schedule]
