"""The typed decision IAM returns for one protected call."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Decision:
    """One allow/deny verdict for one operation on one target; not a session grant, no bearer material."""

    decision_id: str
    principal: str
    resource: str
    operation: str
    target: str | None
    allow: bool
    reason: str

    def matches(self, resource: str, operation: str, target: str | None) -> bool:
        """Whether this decision is for the exact call the service is about to execute."""
        return (self.resource, self.operation, self.target) == (resource, operation, target)
