"""The cluster ledger: per-account compute and inference spend with quota enforcement.

Each account accrues :class:`Spend` (compute units and inference units); the job service charges each job's
CPU-hours to its team's account, from the wall time it ran. An optional :class:`Quota` caps each dimension; a
charge that would push either dimension past its cap is rejected with :class:`QuotaExceeded` and the ledger
is left unchanged, so accounting stays consistent under a rejected charge. :meth:`report` returns a stable,
sorted snapshot for billing and dashboards.
"""

from __future__ import annotations

from dataclasses import dataclass

from meridian_jobsvc.errors import QuotaExceeded


@dataclass(frozen=True)
class Spend:
    """Accrued spend for one account: compute units and inference units."""

    compute: float = 0.0
    inference: float = 0.0

    def plus(self, *, compute: float = 0.0, inference: float = 0.0) -> Spend:
        """This spend with ``compute`` and ``inference`` added."""
        return Spend(compute=self.compute + compute, inference=self.inference + inference)


@dataclass(frozen=True)
class Quota:
    """A per-account cap on compute and inference units (``None`` means unbounded on that dimension)."""

    compute: float | None = None
    inference: float | None = None

    def admits(self, spend: Spend) -> bool:
        """Whether ``spend`` stays within both caps."""
        if self.compute is not None and spend.compute > self.compute:
            return False
        return not (self.inference is not None and spend.inference > self.inference)


class Ledger:
    """Records per-account spend and enforces per-account quotas."""

    def __init__(self) -> None:
        """Start with no recorded spend and no quotas set."""
        self._spend: dict[str, Spend] = {}
        self._quota: dict[str, Quota] = {}

    def set_quota(
        self,
        account: str,
        *,
        compute: float | None = None,
        inference: float | None = None,
    ) -> None:
        """Set (or replace) the quota for ``account``."""
        self._quota[account] = Quota(compute=compute, inference=inference)

    def quota(self, account: str) -> Quota:
        """The quota for ``account`` (an unbounded quota if none was set)."""
        return self._quota.get(account, Quota())

    def charge(self, account: str, *, compute: float = 0.0, inference: float = 0.0) -> Spend:
        """Add a charge to ``account`` and return the new total.

        Raises:
            QuotaExceeded: if the charge would push either dimension past the account's quota; the ledger
                is left unchanged.
        """
        if compute < 0 or inference < 0:
            raise QuotaExceeded(f"charge for {account!r} must be non-negative", path=account)
        current = self._spend.get(account, Spend())
        updated = current.plus(compute=compute, inference=inference)
        quota = self.quota(account)
        if not quota.admits(updated):
            raise QuotaExceeded(
                f"charge would exceed quota for {account!r}",
                path=account,
                compute=updated.compute,
                inference=updated.inference,
            )
        self._spend[account] = updated
        return updated

    def spent(self, account: str) -> Spend:
        """The spend accrued by ``account`` so far."""
        return self._spend.get(account, Spend())

    def remaining(self, account: str) -> Quota:
        """How much of each capped dimension ``account`` has left (``None`` where uncapped)."""
        quota = self.quota(account)
        spent = self.spent(account)
        compute = None if quota.compute is None else max(quota.compute - spent.compute, 0.0)
        inference = None if quota.inference is None else max(quota.inference - spent.inference, 0.0)
        return Quota(compute=compute, inference=inference)

    def report(self) -> dict[str, Spend]:
        """A snapshot of every account's spend, keyed by account in sorted order."""
        return {account: self._spend[account] for account in sorted(self._spend)}

    def total(self) -> Spend:
        """The aggregate spend across every account."""
        total = Spend()
        for spend in self._spend.values():
            total = total.plus(compute=spend.compute, inference=spend.inference)
        return total
