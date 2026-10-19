"""What a ticket tracker starts with: Meridian's people, its projects and their tickets, with their history.

A seed is replayed through the tracker's own operations at the times it gives, so its projects, tickets,
comments and history rows are the ones the tracker would have written had the work happened live. A ticket
takes the number the seed gives it, else the one after its project's last, in the order the seed lists them.
"""

from __future__ import annotations

import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import Annotated, Self

import yaml
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, PositiveInt, model_validator

from meridian_controlplane.tickets.store import PROJECT_KEY_PATTERN, Status, TicketStore

ProjectKey = Annotated[str, Field(pattern=PROJECT_KEY_PATTERN)]


class _SeedModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class SeedActivity(_SeedModel):
    """One later event on a seeded ticket: a comment, or a change to some of its fields."""

    by: str
    at: AwareDatetime
    comment: str | None = None
    title: str | None = None
    description: str | None = None
    status: Status | None = None
    assignee: str | None = None
    parent: str | None = None

    @model_validator(mode="after")
    def _is_a_comment_or_a_change(self) -> Self:
        changes = [self.title, self.description, self.status, self.assignee, self.parent]
        if (self.comment is None) == all(change is None for change in changes):
            raise ValueError("an activity is a comment or a change, never both or neither")
        return self


class SeedTicket(_SeedModel):
    """A ticket as it was filed, and what happened to it since, oldest first."""

    project: ProjectKey
    number: PositiveInt | None = None
    title: str
    description: str = ""
    creator: str
    created_at: AwareDatetime
    assignee: str | None = None
    parent: str | None = None
    activity: list[SeedActivity] = []


class SeedProject(_SeedModel):
    """A project as it was created."""

    key: ProjectKey
    name: str
    description: str = ""
    created_by: str
    created_at: AwareDatetime


class TicketSeed(_SeedModel):
    """Everyone besides the agents under test who can be assigned a ticket, and the projects and tickets."""

    people: list[str]
    projects: list[SeedProject]
    tickets: list[SeedTicket]


def load_ticket_seed(path: Path) -> TicketSeed:
    """The seed in the YAML file at ``path``."""
    return TicketSeed.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def open_seeded_store(database: Path, seed: TicketSeed, agents: Iterable[str]) -> TicketStore:
    """The store at ``database``, built from ``seed`` if there is none; ``agents`` can be assigned too.

    A new store is built aside and moved into place whole, so a replay that fails leaves no store to reopen.
    """
    principals = [*seed.people, *agents]
    if not database.exists():
        database.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=database.parent) as building:
            built = Path(building) / database.name
            store = TicketStore(built, principals)
            _replay(seed, store)
            store.close()
            built.replace(database)
    return TicketStore(database, principals)


def _replay(seed: TicketSeed, store: TicketStore) -> None:
    for project in seed.projects:
        store.create_project(
            project.created_by,
            project.key,
            project.name,
            project.description,
            project.created_at,
        )
    for ticket in seed.tickets:
        filed = store.create_ticket(
            ticket.creator,
            ticket.project,
            ticket.title,
            ticket.description,
            ticket.assignee,
            ticket.parent,
            ticket.created_at,
            ticket.number,
        )
        for event in ticket.activity:
            if event.comment is not None:
                store.comment_on_ticket(event.by, filed.id, event.comment, event.at)
                continue
            store.edit_ticket(
                event.by,
                filed.id,
                event.title,
                event.description,
                event.status,
                event.assignee,
                event.parent,
                event.at,
            )
