"""The transactional IAM core: one authoritative SQLite store behind a small public API."""

from __future__ import annotations

from meridian_iam.core.service import Iam

__all__ = ["Iam"]
