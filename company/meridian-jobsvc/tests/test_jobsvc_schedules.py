from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from jobsvc_support import CREDENTIAL, OWNER, SECRET, Clock, FakeIssuer, cluster, job
from pydantic import SecretStr, ValidationError

from meridian_common.jobclient.models import JobState
from meridian_jobsvc.schedules import ScheduleRequest

INTERVAL = 600


def nightly(**timing: Any) -> ScheduleRequest:  # noqa: ANN401 - a cron expression or an interval
    return ScheduleRequest(job=job("nightly"), **timing)


def test_a_schedules_runs_start_when_due_as_its_one_identity_and_stop_once_it_is_deleted() -> None:
    clock, issuer = Clock(), FakeIssuer()
    service, (node,), _ = cluster(issuer=issuer, clock=clock)
    created = service.create_schedule(nightly(interval_seconds=INTERVAL), OWNER, CREDENTIAL)

    clock.advance(INTERVAL)
    service.reconcile()  # the first run starts
    node.exit(0)
    clock.advance(INTERVAL)
    service.reconcile()  # it ends, and the second starts
    node.exit(0)
    service.delete_schedule(created.id, OWNER)
    clock.advance(INTERVAL)
    service.reconcile()  # the second ends, and no third starts

    runs = [service.get_job(run) for run in service.get_schedule(created.id).runs]
    assert [(run.schedule_id, run.owner, run.principal, run.state) for run in runs] == [
        (created.id, OWNER, "principal-of-schedule-1", JobState.SUCCEEDED),
    ] * 2
    assert issuer.asked == [(CREDENTIAL, "create_schedule", created.id, "nightly")]
    assert node.environments == [{"MERIDIAN_NOTES_TOKEN": SecretStr(SECRET)}] * 2


def test_a_due_run_is_skipped_while_the_schedules_last_run_is_unfinished() -> None:
    clock = Clock()
    service, (node,), _ = cluster(clock=clock)
    created = service.create_schedule(nightly(interval_seconds=INTERVAL), OWNER, CREDENTIAL)
    clock.advance(INTERVAL)
    service.reconcile()  # the first run starts, and runs on

    clock.advance(INTERVAL)
    service.reconcile()  # the second is due while the first runs
    node.exit(0)
    clock.advance(INTERVAL / 2)
    service.reconcile()

    found = service.get_schedule(created.id)
    assert (len(found.runs), found.next_run_at) == (1, clock.now + timedelta(seconds=INTERVAL / 2))


def test_a_cron_schedules_runs_are_due_at_the_times_its_expression_names_in_utc() -> None:
    clock = Clock(datetime(2026, 10, 22, 9, 7, tzinfo=UTC))
    service, _, _ = cluster(clock=clock)
    created = service.create_schedule(nightly(cron="*/15 * * * *"), OWNER, CREDENTIAL)

    clock.now = datetime(2026, 10, 22, 9, 15, tzinfo=UTC)
    service.reconcile()

    found = service.get_schedule(created.id)
    assert (created.next_run_at, len(found.runs), found.next_run_at) == (
        datetime(2026, 10, 22, 9, 15, tzinfo=UTC),
        1,
        datetime(2026, 10, 22, 9, 30, tzinfo=UTC),
    )


@pytest.mark.parametrize(
    "timing",
    [{}, {"cron": "0 * * * *", "interval_seconds": 60}, {"cron": "every hour"}, {"cron": "0 0 * * * *"}],
    ids=["neither", "both", "no cron expression", "six fields"],
)
def test_a_schedule_takes_one_timing_a_five_field_cron_expression_or_an_interval(
    timing: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError):
        nightly(**timing)
