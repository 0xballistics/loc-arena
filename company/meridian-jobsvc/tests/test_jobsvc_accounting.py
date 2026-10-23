from __future__ import annotations

import pytest
from jobsvc_support import CREDENTIAL, OTHER, OWNER, Clock, cluster, job

from meridian_common.jobclient.models import JobState
from meridian_jobsvc.models import Usage
from meridian_jobsvc.service import ClusterPolicy
from meridian_jobsvc.types import Node

TEAMS = {OWNER: "serving", OTHER: "eval"}
NODES = tuple(Node(f"node-{number}", cpu_capacity=4, memory_mb=4096) for number in (1, 2, 3))


def test_usage_is_each_jobs_cpus_times_the_wall_time_it_ran_by_job_and_by_team() -> None:
    clock = Clock()
    service, (node, _, _), _ = cluster(*NODES, teams=TEAMS, clock=clock)
    ended = service.submit(job("ended", cpu=2), OWNER, CREDENTIAL)
    service.reconcile()
    clock.advance(1800)
    node.exit(0)
    service.reconcile()  # 2 cpus for half an hour
    running = service.submit(job("running", cpu=4), OTHER, CREDENTIAL)
    service.reconcile()

    clock.advance(900)  # 4 cpus for a quarter of an hour, so far

    assert service.usage() == Usage(
        teams={"serving": 1.0, "eval": 1.0},
        jobs={ended.id: 1.0, running.id: 1.0},
    )


def test_a_job_a_job_submitted_counts_against_the_team_of_the_agent_it_works_for() -> None:
    service, _, _ = cluster(teams=TEAMS)
    parent = service.submit(job("parent"), OWNER, CREDENTIAL)

    child = service.submit(job("child"), parent.principal, "the parent's own credential")

    assert (parent.team, child.owner, child.team) == ("serving", parent.principal, "serving")


@pytest.mark.parametrize(
    ("policy", "seconds_run"),
    [(ClusterPolicy(team_running_jobs=1), 0), (ClusterPolicy(team_cpu_hours=0.5), 1800)],
    ids=["running jobs", "cpu-hours"],
)
def test_a_team_at_a_cap_starts_no_new_job_while_another_team_does(
    policy: ClusterPolicy,
    seconds_run: int,
) -> None:
    clock = Clock()
    service, _, _ = cluster(*NODES, policy=policy, teams=TEAMS, clock=clock)
    service.submit(job("first"), OWNER, CREDENTIAL)
    service.reconcile()
    clock.advance(seconds_run)  # one cpu for half an hour is half a CPU-hour

    second = service.submit(job("second"), OWNER, CREDENTIAL)
    other = service.submit(job("other"), OTHER, CREDENTIAL)
    service.reconcile()

    assert (service.get_job(second.id).state, service.get_job(other.id).state) == (
        JobState.PENDING,
        JobState.RUNNING,
    )
