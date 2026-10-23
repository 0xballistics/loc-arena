"""The cluster's own seed: the teams whose jobs it runs, who is on each, and the work it starts with.

The service starts from ``seed.yaml`` beside this module, until the company's directory supplies teams and its
history the work. A team is what a job's CPU-hours count against and what the cluster's caps bound; nothing a
submitter sends names it. The seed's jobs and schedules are other teams' work, already on the cluster when it
starts: each its owner's and marked seed on its record, and holding no credential of its own, since its owner
never presented one to the service. A seeded job's ``depends_on`` names earlier seeded jobs by the ids the
queue gives them in the seed's order, ``job-1`` first.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import yaml
from pydantic import BaseModel, ConfigDict, Field

from meridian_jobsvc.models import JobRequest
from meridian_jobsvc.schedules import ScheduleRequest

SEED_FILE = Path(__file__).with_name("seed.yaml")
Name = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9.-]{0,62}$")]


class SeededJob(BaseModel):
    """A job on the cluster when it starts: whose it is, whether its owner holds it, and the job itself."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    owner: Name
    held: bool = False
    job: JobRequest


class SeededSchedule(BaseModel):
    """A schedule on the cluster when it starts: whose it is, and the schedule itself."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    owner: Name
    schedule: ScheduleRequest


class ClusterSeed(BaseModel):
    """The seed file: each team and the principals on it, and the jobs and schedules it starts with."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    teams: dict[Name, list[Name]]
    jobs: list[SeededJob] = Field(default_factory=list)
    schedules: list[SeededSchedule] = Field(default_factory=list)


def load_cluster_seed(seed_file: Path = SEED_FILE) -> ClusterSeed:
    """The seed in ``seed_file``."""
    return ClusterSeed.model_validate(yaml.safe_load(seed_file.read_text()))


def load_teams(seed_file: Path = SEED_FILE) -> dict[str, str]:
    """Each principal's team, from ``seed_file``; a principal on two teams is a ``ValueError``."""
    teams: dict[str, str] = {}
    for team, members in load_cluster_seed(seed_file).teams.items():
        for member in members:
            if teams.setdefault(member, team) != team:
                raise ValueError(f"{member!r} is on two teams: {teams[member]!r} and {team!r}")
    return teams
