"""What a resource service imports: the guard and its HTTP/ASGI adapters."""

from __future__ import annotations

from meridian_iam.integration.guard import AccessDenied, Guard

__all__ = ["AccessDenied", "Guard"]
