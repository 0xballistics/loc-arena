"""The agents service: the registry of the agents Meridian runs, with access control and an audit record.

``registry`` holds the agents and their versions, ``access_control`` who may do what, and ``service`` the HTTP
API over both.
"""

from __future__ import annotations

from meridian_controlplane.agents.access_control import ENFORCED_RIGHTS, AccessControl, RightsChange
from meridian_controlplane.agents.authority import CentralAuthority
from meridian_controlplane.agents.registry import (
    AgentDefinition,
    AgentRegistration,
    AgentRegistry,
    AgentSummary,
    AgentVersion,
    SeededAgent,
    load_seeded_agents,
)
from meridian_controlplane.agents.service import create_agents_service_app

__all__ = [
    "ENFORCED_RIGHTS",
    "AccessControl",
    "AgentDefinition",
    "AgentRegistration",
    "AgentRegistry",
    "AgentSummary",
    "AgentVersion",
    "CentralAuthority",
    "RightsChange",
    "SeededAgent",
    "create_agents_service_app",
    "load_seeded_agents",
]
