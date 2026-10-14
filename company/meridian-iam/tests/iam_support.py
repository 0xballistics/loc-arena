"""Test support shared by the IAM tests: a notes-like manifest and a fake clock."""

from __future__ import annotations

from typing import Any

NOTES: dict[str, Any] = {
    "schema_version": 1,
    "resource": "notes",
    "operations": {
        "read_note": {"permission": "read", "requestable": True, "target": "key"},
        "write_note": {"permission": "write", "requestable": True, "target": "key"},
        "admin": {"permission": "manage"},
        "health": {"public": True},
    },
    "http": {
        "GET /notes/{key}": "read_note",
        "PUT /notes/{key}": "write_note",
        "POST /admin": "admin",
        "GET /health": "health",
    },
}


class Clock:
    """Mutable integer-second clock."""

    def __init__(self) -> None:
        """Start at a fixed epoch."""
        self.now = 1000

    def __call__(self) -> int:
        """Return the current fake time."""
        return self.now
