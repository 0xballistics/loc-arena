"""How a stack service joins central IAM, and the only module of the engine that imports ``meridian_iam``.

A service joins by environment: the renderer sets ``LOC_ARENA_IAM_URL``, the path of its manifest and the path
of its own IAM credential. Company packages are importable only inside a service container (the episode and
the host do not put them on the path), so the imports are made when a service starts, never at module load.
"""

from __future__ import annotations

import importlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import anyio

from loc_arena.mcp_service import IdentityVerifier

URL_VARIABLE = "LOC_ARENA_IAM_URL"
MANIFEST_VARIABLE = "LOC_ARENA_IAM_MANIFEST"
CREDENTIAL_VARIABLE = "LOC_ARENA_IAM_CREDENTIAL"


def guard_from_environment(environ: Mapping[str, str]) -> Any | None:  # noqa: ANN401 - a meridian_iam Guard
    """The IAM guard this service is configured for, or ``None`` when it is not in IAM mode."""
    if URL_VARIABLE not in environ:
        return None
    client_module = importlib.import_module("meridian_iam.client")
    contracts = importlib.import_module("meridian_iam.contracts")
    integration = importlib.import_module("meridian_iam.integration")
    manifest = contracts.load_manifest(json.loads(Path(environ[MANIFEST_VARIABLE]).read_text()))
    credential = Path(environ[CREDENTIAL_VARIABLE]).read_text().strip()
    return integration.Guard(manifest, client_module.IamClient(environ[URL_VARIABLE], credential))


def serving_options(
    service: str,
    identities: Mapping[str, bytes],
    environ: Mapping[str, str],
) -> dict[str, Any]:
    """The ``verifier`` and ``extra_middleware`` an MCP service is built with, legacy or under IAM.

    Under IAM the credential check and the per-call operation check both go to IAM, and the service needs no
    identity of its own agents; otherwise the mounted identities admit callers as before.
    """
    guard = guard_from_environment(environ)
    if guard is None:
        return {"verifier": IdentityVerifier(identities), "extra_middleware": (), "annotate": None}
    return iam_options(guard, service)


def iam_options(guard: Any, service: str) -> dict[str, Any]:  # noqa: ANN401 - a meridian_iam Guard
    """The ``verifier`` and ``extra_middleware`` that put ``guard`` in front of an MCP service."""
    mcp = importlib.import_module("meridian_iam.integration.mcp")
    return {
        "verifier": mcp.IamVerifier(guard, service),
        "extra_middleware": (mcp.tool_authorizer(guard),),
        "annotate": mcp.decision_attributes,
    }


def check_served_tools(environ: Mapping[str, str], server: Any) -> None:  # noqa: ANN401 - an MCPServer
    """Under IAM, refuse to serve unless the tools ``server`` registered are exactly the manifest's.

    A tool added without a declared operation would otherwise be reachable and unchecked by name, and a
    declared operation with no tool means the manifest describes a service that is not the one running. A
    service not under IAM is not checked.
    """
    guard = guard_from_environment(environ)
    if guard is not None:
        guard.verify_routes(set(), {tool.name for tool in anyio.run(server.list_tools)})
