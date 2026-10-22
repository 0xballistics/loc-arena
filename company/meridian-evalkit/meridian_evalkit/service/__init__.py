"""The eval service: Meridian's evals, the runs on them, their results and the leaderboards.

``registry`` holds them and what a registry starts with, ``authority`` what the service needs of the central
authority that decides its calls, and ``service`` the HTTP API and the MCP tools over a registry. A run's
job, the runner the cluster runs for it, is not here yet: a run is reported by whatever job binds itself to
it.
"""

from __future__ import annotations

from meridian_evalkit.service.authority import Authority
from meridian_evalkit.service.registry import (
    Board,
    Diff,
    Eval,
    EvalkitRegistry,
    EvalRegistration,
    EvalServiceError,
    EvalSummary,
    JobBody,
    JobResources,
    Results,
    Run,
    RunStart,
    RunState,
    Seed,
    TextItem,
    load_seed,
)
from meridian_evalkit.service.service import SERVICE, build_evalkit_server, create_evalkit_api

__all__ = [
    "SERVICE",
    "Authority",
    "Board",
    "Diff",
    "Eval",
    "EvalRegistration",
    "EvalServiceError",
    "EvalSummary",
    "EvalkitRegistry",
    "JobBody",
    "JobResources",
    "Results",
    "Run",
    "RunStart",
    "RunState",
    "Seed",
    "TextItem",
    "build_evalkit_server",
    "create_evalkit_api",
    "load_seed",
]
