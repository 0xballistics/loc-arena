"""Fixtures: a notes-like resource owned by team ``research`` (approver role ``lead``)."""

from __future__ import annotations

from pathlib import Path

import pytest
from iam_support import NOTES, Clock

from meridian_iam.contracts import load_manifest
from meridian_iam.core import Iam


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def iam(tmp_path: Path, clock: Clock) -> Iam:
    """Principals: lead (owner/approver, authority read+write), alice and bob (requesters), notes-svc."""
    core = Iam(tmp_path / "iam.db", clock)
    for name, kind in [("lead", "agent"), ("alice", "agent"), ("bob", "agent"), ("notes-svc", "service")]:
        core.add_principal(name, kind)
    core.add_member("lead", "research", "lead", authority={"notes": ["read", "write"]})
    core.register_resource(
        load_manifest(NOTES),
        owner_team="research",
        approver_role="lead",
        max_seconds=3600,
    )
    core.bind_service("notes-svc", "notes", audience="notes")
    return core


@pytest.fixture
def tokens(iam: Iam) -> dict[str, str]:
    """Callers carry the ``iam`` and ``notes`` audiences; the service only ``iam``."""
    out = {name: iam.issue_credential(name, ["iam", "notes"]) for name in ("lead", "alice", "bob")}
    out["svc"] = iam.issue_credential("notes-svc", ["iam"])
    return out
