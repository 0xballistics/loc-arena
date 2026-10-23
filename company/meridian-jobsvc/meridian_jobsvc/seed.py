"""The cluster's own seed: the teams whose jobs it runs, and who is on each.

The service starts from ``seed.yaml`` beside this module, until the company's directory supplies teams. A team
is what a job's CPU-hours count against and what the cluster's caps bound; nothing a submitter sends names it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import yaml
from pydantic import BaseModel, ConfigDict, Field

SEED_FILE = Path(__file__).with_name("seed.yaml")
Name = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9.-]{0,62}$")]


class ClusterSeed(BaseModel):
    """The seed file: each team, and the principals on it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    teams: dict[Name, list[Name]]


def load_teams(seed_file: Path = SEED_FILE) -> dict[str, str]:
    """Each principal's team, from ``seed_file``; a principal on two teams is a ``ValueError``."""
    seed = ClusterSeed.model_validate(yaml.safe_load(seed_file.read_text()))
    teams: dict[str, str] = {}
    for team, members in seed.teams.items():
        for member in members:
            if teams.setdefault(member, team) != team:
                raise ValueError(f"{member!r} is on two teams: {teams[member]!r} and {team!r}")
    return teams
