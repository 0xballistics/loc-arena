"""What IAM is seeded with for one play, rendered on the host from the run config.

The honest twin's document is rendered from the honest config, so the covert tools it drops carry no grant.
"""

from __future__ import annotations

import json

from loc_arena.config import RunConfig
from loc_arena.iam_seed import REPOSITORY, load_seed
from loc_arena.task import resolve_scenario

BOOTSTRAP_SECRET = "iam_bootstrap"  # noqa: S105 - the name of a mounted file, not a secret
BOOTSTRAP_VARIABLE = "LOC_ARENA_IAM_BOOTSTRAP"  # where compose reads each play's bootstrap document from
SERVICE_CREDENTIAL_PREFIX = "iam_service_"  # a managed service's own IAM credential, by service
CREDENTIAL_FILE = "iam_credential"  # that credential, as the managed service itself finds it
IAM_AUDIENCE = "iam"
INBOX_KEY_PREFIX = "iam_inbox_"  # an agent's key to its own IAM inbox, by agent: what the tools gateway sends


def service_credential_variable(service: str) -> str:
    """Where compose reads ``service``'s own IAM credential from, on the host."""
    return f"LOC_ARENA_IAM_SERVICE_{service.upper().replace('-', '_')}"


def inbox_key_variable(agent: str) -> str:
    """Where compose reads ``agent``'s inbox key from, on the host."""
    return f"LOC_ARENA_IAM_INBOX_{agent.upper().replace('-', '_')}"


def managed_services(config: RunConfig) -> tuple[str, ...]:
    """The live services of ``config`` that central IAM manages (none unless it is enabled)."""
    if not config.settings.iam.enabled:
        return ()
    seed = load_seed(resolve_scenario(config))
    return () if seed is None else tuple(name for name in seed.resources if name in _live_names(config))


def _live_names(config: RunConfig) -> set[str]:
    return {service.name for service in config.live_services}


def render_bootstrap(config: RunConfig) -> str:
    """The document IAM seeds itself from at start, for this ``config`` (apply the play's mode first).

    Per managed service: its manifest path, owner policy, approver (the scenario's, never an agent of the
    run) and delegation, and each agent's baseline permissions: its starting rights and the permission of each
    tool it holds.
    Dropping the honest twin's covert tools from ``config`` drops their grants with them.
    """
    seed = load_seed(resolve_scenario(config))
    if seed is None:
        raise ValueError("no iam seed")
    resources, grants = {}, {}
    for name, resource in seed.resources.items():
        manifest = json.loads((REPOSITORY / resource.manifest).read_text())
        # A tool of an operation open to any known caller (``authenticated``) needs no grant.
        permission_of_tool = {
            tool: permission
            for tool, operation in manifest.get("tools", {}).items()
            if (permission := manifest["operations"][operation].get("permission")) is not None
        }
        resources[name] = resource.model_dump()
        grants[name] = {}
        for agent in config.agents:
            held = set(agent.sandbox.rights.get(name, ())) | {
                permission_of_tool[tool]
                for tool in agent.sandbox.tools.get(name, ())
                if tool in permission_of_tool
            }
            if held:
                grants[name][agent.id] = sorted(held)
    document = {
        "agents": [agent.id for agent in config.agents],
        "operators": list(seed.operators),
        "resources": resources,
        "grants": grants,
    }
    return json.dumps(document, sort_keys=True)


def manifest_path(config: RunConfig, service: str) -> str:
    """The manifest of managed ``service``, relative to the repository root."""
    seed = load_seed(resolve_scenario(config))
    if seed is None or service not in seed.resources:
        raise ValueError(f"{service!r} is not a service the iam seed manages")
    return seed.resources[service].manifest
