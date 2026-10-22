"""The eval service: Meridian's evals, the runs on them, their results and the leaderboards.

``registry`` holds them and what a registry starts with, ``access`` who may call, and ``service`` the HTTP API
and the MCP tools over a registry. A run's job, the runner the cluster runs for it, is not here yet: a run is
reported by whatever job binds itself to it.
"""

from __future__ import annotations

from meridian_evalkit.service.access import RIGHTS, Authority, Callers
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
    "RIGHTS",
    "SERVICE",
    "Authority",
    "Board",
    "Callers",
    "Diff",
    "Eval",
    "EvalRegistration",
    "EvalSummary",
    "EvalkitRegistry",
    "JobBody",
    "JobResources",
    "EvalServiceError",
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
