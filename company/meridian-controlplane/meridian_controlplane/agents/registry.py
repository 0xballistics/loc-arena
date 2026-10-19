"""The registry of the agents Meridian runs: each agent's owner, its versions, and whether it is retired.

Every change to an agent's definition adds a version; earlier versions never change. A retired agent keeps
its versions and takes no new one. The registry lives in memory and starts with the agents in ``seed.yaml``.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from http import HTTPStatus
from pathlib import Path
from typing import Annotated

import yaml
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

SEEDED_AGENTS_FILE = Path(__file__).with_name("seed.yaml")
AgentName = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]*$")]


class AgentDefinition(BaseModel):
    """What an agent runs: its model and its system prompt."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model: str = Field(min_length=1)
    system_prompt: str


class AgentRegistration(AgentDefinition):
    """A new agent: its name and its first definition."""

    name: AgentName


class SeededAgent(AgentRegistration):
    """An agent the registry starts with, and the team that owns it."""

    owner: str = Field(min_length=1)


class AgentVersion(AgentDefinition):
    """One version of an agent's definition, numbered from 1, and who made it when."""

    number: int
    created_by: str
    created_at: datetime


class AgentSummary(BaseModel):
    """An agent as the API returns it: its owner, whether it is retired, and its latest version."""

    name: str
    owner: str
    retired: bool
    latest_version: AgentVersion


@dataclass
class _RegisteredAgent:
    owner: str
    versions: list[AgentVersion]
    retired: bool = False


def load_seeded_agents(seed_file: Path = SEEDED_AGENTS_FILE) -> list[SeededAgent]:
    """The agents listed in ``seed_file``."""
    return TypeAdapter(list[SeededAgent]).validate_python(yaml.safe_load(seed_file.read_text()))


class AgentRegistry:
    """The registered agents by name. A refused change raises ``HTTPException`` with the status to answer."""

    def __init__(self, seeded_agents: Iterable[SeededAgent]) -> None:
        """Start with ``seeded_agents``, each registered by its owner."""
        self._agents_by_name: dict[str, _RegisteredAgent] = {}
        for seeded_agent in seeded_agents:
            self.register(seeded_agent, registered_by=seeded_agent.owner)

    def list_agents(self) -> list[AgentSummary]:
        """Every agent, retired ones included, by name."""
        return [self._summarize_agent(name) for name in sorted(self._agents_by_name)]

    def get_agent(self, name: str) -> AgentSummary:
        """The agent named ``name``; 404 if there is none."""
        self._find_agent(name)
        return self._summarize_agent(name)

    def list_versions(self, name: str) -> list[AgentVersion]:
        """Every version of the agent named ``name``, oldest first; 404 if there is none."""
        return list(self._find_agent(name).versions)

    def register(self, registration: AgentRegistration, registered_by: str) -> AgentSummary:
        """Register a new agent at version 1, owned by ``registered_by``; 409 if the name is taken."""
        if registration.name in self._agents_by_name:
            raise HTTPException(HTTPStatus.CONFLICT, f"agent {registration.name!r} already exists")
        first_version = _make_version(registration, number=1, created_by=registered_by)
        self._agents_by_name[registration.name] = _RegisteredAgent(registered_by, [first_version])
        return self._summarize_agent(registration.name)

    def add_version(self, name: str, definition: AgentDefinition, created_by: str) -> AgentSummary:
        """Give the agent ``definition`` as its next version; 404 if there is none, 409 if it is retired."""
        agent = self._find_agent_not_retired(name)
        agent.versions.append(
            _make_version(definition, number=len(agent.versions) + 1, created_by=created_by),
        )
        return self._summarize_agent(name)

    def retire(self, name: str) -> AgentSummary:
        """Retire the agent; 404 if there is none, 409 if it is already retired."""
        self._find_agent_not_retired(name).retired = True
        return self._summarize_agent(name)

    def _find_agent(self, name: str) -> _RegisteredAgent:
        if name not in self._agents_by_name:
            raise HTTPException(HTTPStatus.NOT_FOUND, f"no agent named {name!r}")
        return self._agents_by_name[name]

    def _find_agent_not_retired(self, name: str) -> _RegisteredAgent:
        agent = self._find_agent(name)
        if agent.retired:
            raise HTTPException(HTTPStatus.CONFLICT, f"agent {name!r} is retired")
        return agent

    def _summarize_agent(self, name: str) -> AgentSummary:
        agent = self._agents_by_name[name]
        return AgentSummary(
            name=name,
            owner=agent.owner,
            retired=agent.retired,
            latest_version=agent.versions[-1],
        )


def _make_version(definition: AgentDefinition, number: int, created_by: str) -> AgentVersion:
    return AgentVersion(
        model=definition.model,
        system_prompt=definition.system_prompt,
        number=number,
        created_by=created_by,
        created_at=datetime.now(UTC),
    )
