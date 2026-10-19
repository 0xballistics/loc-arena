"""What IAM tells a principal about access requests: the ones it may decide, and the outcomes of its own."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class InboxItem:
    """One request, as the principal reading its inbox should see it.

    ``kind`` is ``decide`` for a pending request the reader may decide, ``outcome`` for one of the reader's
    own requests that is no longer pending. ``permissions``, ``until`` and ``reason`` are what was asked;
    ``approver``, ``approved``, ``approved_until`` and ``decision_reason`` are the decision, ``None`` while
    pending (``approved`` also on a denial). Times are Unix seconds; an ``until`` of ``None`` is ongoing. Its
    fields are immutable, so an item can be kept in a set of items already delivered.
    """

    kind: Literal["decide", "outcome"]
    request: str
    requester: str
    resource: str
    permissions: tuple[str, ...]
    until: int | None
    reason: str
    state: Literal["pending", "approved", "denied"]
    approver: str | None
    approved: tuple[str, ...] | None
    approved_until: int | None
    decision_reason: str | None
