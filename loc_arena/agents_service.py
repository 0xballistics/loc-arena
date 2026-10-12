"""The agents service as a stack run's live service: Meridian's agent registry, which is company code.

A stack run starts it as the scenario's ``agents`` entry: ``python -m loc_arena.agents_service <run config>
<entry name>``. It serves ``meridian_controlplane.agents`` with every agent's identity mounted in the secrets
directory and the starting rights the run config gives each agent, and writes the audit record to stdout.
"""

from __future__ import annotations

import functools
import sys

import uvicorn

from loc_arena.config import load_run_config
from loc_arena.mcp_service import ALL_ADDRESSES, read_identities
from meridian_controlplane.agents import (
    AccessControl,
    AgentRegistry,
    create_agents_service_app,
    load_seeded_agents,
)


def main(arguments: list[str]) -> None:
    """Serve entry ``arguments[1]`` of the run config at ``arguments[0]``, once an identity is mounted."""
    run_config = load_run_config(arguments[0])
    service_entry = next(service for service in run_config.live_services if service.name == arguments[1])
    identities_by_agent = read_identities(run_config.settings.gateway.secrets_dir)
    if not identities_by_agent:
        raise SystemExit(f"no identity in {run_config.settings.gateway.secrets_dir}")
    access_control = AccessControl(
        identities_by_caller=identities_by_agent,
        declared_rights=service_entry.rights,
        starting_rights_by_caller={
            agent.id: agent.sandbox.rights.get(service_entry.name, ()) for agent in run_config.agents
        },
        grant_right_is_grantable=service_entry.transitive,
    )
    app = create_agents_service_app(
        AgentRegistry(load_seeded_agents()),
        access_control,
        write_audit_line=functools.partial(print, flush=True),
    )
    uvicorn.run(app, host=ALL_ADDRESSES, port=service_entry.port, log_level="warning", access_log=False)


if __name__ == "__main__":
    main(sys.argv[1:])
