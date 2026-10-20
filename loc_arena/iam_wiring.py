"""How a stack service joins central IAM, and the only module of the engine that imports ``meridian_iam``.

A service joins by environment (``service_environment``): the renderer sets ``LOC_ARENA_IAM_URL``, the path
of its manifest, the path of its own IAM credential, and how long and how often it asks IAM. Company packages
are importable only inside a service container (the episode and the host do not put them on the path), so the
imports are made when a service starts, never at module load.
"""

from __future__ import annotations

import importlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import anyio
from mcp.server import MCPServer
from starlette.applications import Starlette

from loc_arena.mcp_service import IdentityVerifier, build_app
from loc_arena.settings import IamSettings

URL_VARIABLE = "LOC_ARENA_IAM_URL"
MANIFEST_VARIABLE = "LOC_ARENA_IAM_MANIFEST"
CREDENTIAL_VARIABLE = "LOC_ARENA_IAM_CREDENTIAL"
TIMEOUT_VARIABLE = "LOC_ARENA_IAM_TIMEOUT_SECONDS"
RETRIES_VARIABLE = "LOC_ARENA_IAM_RETRIES"


def service_environment(settings: IamSettings, manifest: str, credential: str) -> dict[str, str]:
    """What a managed service's environment holds to join IAM: what ``guard_from_environment`` reads."""
    return {
        URL_VARIABLE: f"http://{settings.service}:{settings.port}",
        MANIFEST_VARIABLE: manifest,
        CREDENTIAL_VARIABLE: credential,
        TIMEOUT_VARIABLE: str(settings.timeout_seconds),
        RETRIES_VARIABLE: str(settings.retries),
    }


def guard_from_environment(environ: Mapping[str, str]) -> Any | None:  # noqa: ANN401 - a meridian_iam Guard
    """The IAM guard this service is configured for, or ``None`` when it is not in IAM mode.

    In IAM mode a service without a valid timeout and retries does not start: the settings' bounds hold here.
    """
    if URL_VARIABLE not in environ:
        return None
    try:
        limits = IamSettings.model_validate(
            {"timeout_seconds": float(environ[TIMEOUT_VARIABLE]), "retries": int(environ[RETRIES_VARIABLE])},
        )
    except (KeyError, ValueError) as error:  # pydantic's ValidationError is a ValueError
        wanted = f"{TIMEOUT_VARIABLE} and {RETRIES_VARIABLE}"
        raise SystemExit(f"central IAM needs a valid {wanted}: {error}") from None
    client_module = importlib.import_module("meridian_iam.client")
    contracts = importlib.import_module("meridian_iam.contracts")
    integration = importlib.import_module("meridian_iam.integration")
    manifest = contracts.load_manifest(json.loads(Path(environ[MANIFEST_VARIABLE]).read_text()))
    credential = Path(environ[CREDENTIAL_VARIABLE]).read_text().strip()
    client = client_module.IamClient(
        environ[URL_VARIABLE],
        credential,
        timeout=limits.timeout_seconds,
        retries=limits.retries,
    )
    return integration.Guard(manifest, client)


def serving_options(
    service: str,
    identities: Mapping[str, bytes],
    guard: Any | None,  # noqa: ANN401 - a meridian_iam Guard
) -> dict[str, Any]:
    """The ``verifier`` and ``extra_middleware`` an MCP service is built with: legacy, or IAM's ``guard``.

    Under IAM the credential check and the per-call operation check both go to IAM, and the service needs no
    identity of its own agents; otherwise the mounted identities admit callers as before.
    """
    if guard is None:
        return {"verifier": IdentityVerifier(identities), "extra_middleware": (), "annotate": None}
    return iam_options(guard, service)


def build_service_app(guard: Any | None, server: MCPServer, service: str) -> Starlette:  # noqa: ANN401 - a Guard
    """The app serving ``server`` as ``service``; under IAM's ``guard``, only if its tools are the manifest's.

    A tool added without a declared operation would otherwise be reachable and unchecked by name, and a
    declared operation with no tool means the manifest describes a service that is not the one running. Under
    IAM the app also answers 503 when IAM cannot verify a caller, so the same guard that builds the service's
    verifier (``serving_options``) must build its app. A service not under IAM is not checked.
    """
    app = build_app(server, service)
    if guard is None:
        return app
    guard.verify_routes(set(), {tool.name for tool in anyio.run(server.list_tools)})
    return importlib.import_module("meridian_iam.integration.mcp").answer_outages(app)


def load_iam_authority(guard: Any) -> Any:  # noqa: ANN401 - a meridian_iam Guard, and the CentralAuthority over it
    """The agents service's ``CentralAuthority`` over ``guard``."""
    return importlib.import_module("loc_arena.agents_iam").IamAuthority(guard)


def iam_options(guard: Any, service: str) -> dict[str, Any]:  # noqa: ANN401 - a meridian_iam Guard
    """The ``verifier`` and ``extra_middleware`` that put ``guard`` in front of an MCP service."""
    mcp = importlib.import_module("meridian_iam.integration.mcp")
    return {
        "verifier": mcp.IamVerifier(guard, service),
        "extra_middleware": (mcp.tool_authorizer(guard),),
        "annotate": mcp.decision_attributes,
    }
