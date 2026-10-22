"""What a scenario seeds into central IAM: which live services are managed, and who owns each.

A pack's ``iam:`` block names, per live service, the manifest the service ships, the owner policy its
requests follow and the approver that decides them: a principal of the company, never an agent under test,
with the permissions it approves (it denies the rest). Each agent's starting rights and tools on a service,
from the run config, become its baseline grants.
The block is inert only where the run config turns ``iam.enabled`` off (an in-process run). Every service an
agent reaches, by tools or by rights, must be one of its resources.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Collection
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr
from scenarios.loader import EngineModule, LiveService, Scenario

from loc_arena.settings import IamSettings

if TYPE_CHECKING:  # the config module imports this one, so the type is for annotations only
    from loc_arena.config import AgentConfig

IAM_RIGHT = "request"  # IAM declares one right only so the stack issues every agent an identity on it
INBOX_TOOL = "access_inbox"  # the one tool IAM serves, through the tools gateway: each agent's own inbox
REPOSITORY = (
    Path(__file__).resolve().parents[1]
)  # manifest paths are relative to it; the engine image mirrors it


class ApproverSeed(BaseModel):
    """The principal that decides a service's access requests, and what it approves when asked."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    principal: Annotated[StrictStr, Field(pattern=r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")] = Field(
        description="Its principal in IAM: a member of the owner team, and never an agent of the run.",
    )
    approves: tuple[StrictStr, ...] = Field(
        description="The requestable permissions it approves; it denies a request for any other.",
    )


class ResourceSeed(BaseModel):
    """One managed service's manifest, owner policy and approver."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    manifest: StrictStr = Field(description="The service's manifest, relative to the repository root.")
    owner_team: Annotated[StrictStr, Field(min_length=1)]
    approver_role: Annotated[StrictStr, Field(min_length=1)]
    approver: ApproverSeed
    max_seconds: Annotated[StrictInt, Field(gt=0)] = Field(
        description="The longest finite grant a request may ask.",
    )
    allow_ongoing: StrictBool = False
    max_pending: Annotated[StrictInt, Field(gt=0)] = 20
    records_actions: StrictBool = Field(
        default=False,
        description="Whether the service writes its own operation record of each call it forwards to IAM, "
        "which makes IAM's event for it a duplicate; a service that does not (notes) leaves IAM's event as "
        "the only record.",
    )


class IamSeed(BaseModel):
    """The pack's ``iam:`` block."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    resources: dict[StrictStr, ResourceSeed]
    operators: tuple[StrictStr, ...] = Field(
        default=(),
        description="Agents who may disable a principal through IAM. None unless the scenario names them.",
    )


def load_seed(scenario: Scenario | None) -> IamSeed | None:
    """The scenario's IAM seed, or ``None`` when it declares none."""
    declared = None if scenario is None else scenario.meta.get("iam")
    return None if declared is None else IamSeed.model_validate(declared)


def find_seed_problem(
    seed: IamSeed,
    live_services: tuple[LiveService, ...],
    agents: Collection[str],
) -> str | None:
    """What is wrong with a seed against the scenario's services and the run's ``agents``, else ``None``."""
    live = {service.name: service for service in live_services}
    if unknown := sorted(set(seed.resources) - set(live)):
        return f"the iam seed names services the scenario does not run: {unknown}"
    roles: dict[str, tuple[str, str]] = {}
    for name, resource in seed.resources.items():
        path = REPOSITORY / resource.manifest
        if not path.is_file():
            return f"the iam seed for {name!r} names no manifest file: {resource.manifest}"
        if problem := _manifest_problem(name, path, live[name].rights, resource.approver):
            return problem
        principal, role = resource.approver.principal, (resource.owner_team, resource.approver_role)
        if principal in agents:
            return f"the approver of {name!r} is {principal!r}, an agent under test"
        if principal in {
            f"{service}-service" for service in seed.resources
        }:  # IAM names each service's own so
            return f"the approver of {name!r} is named {principal!r}, as a service's own principal is"
        if (
            roles.setdefault(principal, role) != role
        ):  # one membership per team: a second would replace the first
            return f"approver {principal!r} holds two roles in one owner team: {roles[principal]} and {role}"
    reached = {service.name for service in live_services if service.tools or service.rights}
    if unmanaged := sorted(reached - set(seed.resources)):
        return f"agents reach services outside central IAM; give each an iam: resource: {unmanaged}"
    return None


class _ManifestHead(BaseModel):
    """What the host reads of a manifest to render the bootstrap; the full check is the service's, at start.

    The engine cannot import ``meridian_iam`` (the episode container has no company packages), so it parses
    the JSON into this typed shape at the boundary and reports a manifest it could not render before any
    bootstrap.
    """

    model_config = ConfigDict(extra="ignore")

    resource: StrictStr
    operations: dict[StrictStr, dict[StrictStr, object]] = Field(min_length=1)
    tools: dict[StrictStr, StrictStr] = Field(default_factory=dict)


def _manifest_problem(name: str, path: Path, rights: Collection[str], approver: ApproverSeed) -> str | None:
    """What is wrong with the manifest a seed names, else ``None``.

    Unreadable, malformed or another resource's; never checking one of the service's ``rights`` (a right no
    operation checks could be granted and never enforced); or not letting anyone request what the approver
    approves.
    """
    try:
        head = _ManifestHead.model_validate_json(path.read_text())
    except (OSError, ValueError) as error:
        return f"the iam manifest of {name!r} ({path.name}) is not a valid manifest: {error}"
    if head.resource != name:
        return f"the iam manifest of {name!r} declares resource {head.resource!r}"
    if unknown := sorted(set(head.tools.values()) - set(head.operations)):
        return f"the iam manifest of {name!r} binds tools to operations it does not declare: {unknown}"
    checked = {spec.get("permission") for spec in head.operations.values()}
    if unchecked := sorted(set(rights) - checked):
        return f"the service {name!r} declares rights its iam manifest never checks: {unchecked}"
    requestable = {spec.get("permission") for spec in head.operations.values() if spec.get("requestable")}
    if unrequestable := sorted(set(approver.approves) - requestable):
        return f"the approver of {name!r} approves permissions nobody can request: {unrequestable}"
    return None


def add_iam_service(
    live_services: tuple[LiveService, ...],
    scenario: Scenario | None,
    settings: IamSettings,
    agents: Collection[str],
) -> tuple[LiveService, ...]:
    """``live_services`` plus central IAM as one more of them, when the run config enables it.

    Raises ``ValueError`` when the scenario seeds nothing for IAM to manage, or the seed is wrong.
    """
    if not settings.enabled:
        return live_services
    seed = load_seed(scenario)
    if seed is None:
        raise ValueError("iam.enabled needs the scenario pack to declare an iam: block")
    if problem := find_seed_problem(seed, live_services, agents):
        raise ValueError(problem)
    if any(service.name == settings.service for service in live_services):
        raise ValueError(f"the scenario already runs a service named {settings.service!r}")
    iam = LiveService(
        name=settings.service,
        source=EngineModule("loc_arena.iam_service"),
        port=settings.port,
        command=None,
        healthcheck=None,
        rights=(IAM_RIGHT,),
        description="Central IAM: GET / says how to see your access and ask for more; approvers decide.",
        tools=(INBOX_TOOL,),
    )
    return (*live_services, iam)


def offer_inbox(agents: tuple[AgentConfig, ...], settings: IamSettings) -> tuple[AgentConfig, ...]:
    """``agents``, each also offered IAM's ``access_inbox`` when the run config enables IAM.

    Every agent has an inbox: the tools gateway delivers it at each turn's start with the agent's inbox key,
    which reads that inbox and nothing else. Any IAM tools the config lists are kept, so the config's own
    validation still sees them. Without IAM the agents are returned as they are.
    """
    if not settings.enabled:
        return agents

    def offered(tools: dict[str, tuple[str, ...]]) -> dict[str, tuple[str, ...]]:
        listed = tools.get(settings.service, ())
        return {**tools, settings.service: listed if INBOX_TOOL in listed else (*listed, INBOX_TOOL)}

    return tuple(
        dataclasses.replace(
            agent,
            sandbox=dataclasses.replace(agent.sandbox, tools=offered(agent.sandbox.tools)),
        )
        for agent in agents
    )
