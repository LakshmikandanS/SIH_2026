"""The classification lattice.

Ported near-verbatim from the prototype's `contracts/classification.py`
(`packages/contracts/AGENTS.md` porting table: "lattice with explicit
comparison; unknown markings denied" -- no change on the way in). Split into
its own file for the same reason the prototype split it: the policy
evaluator's `resource.classification` vs `actor`/`tool` ceiling comparisons
(`citadel_tools`, `registry/policy.yaml`) and the Decision Receipt's
`resource_digest()` (`receipts.py`, this package) both need exactly this
lattice, and neither needs the rest of `state_machines.py` to get it. Both
must agree on the same ordering, which is the whole reason it lives in
`citadel_contracts` rather than behind an import of either package.

`registry/profiles.yaml`'s `classification_ceiling` (lowercase in YAML by
that file's own convention -- `public`, `confidential`) is compared through
this same `rank()`, uppercased once at the boundary that reads the registry
-- never through raw string or enum comparison, and never by a value the
lattice does not define. `demo-local`'s ceiling is `confidential` (the top
of the lattice -- the sovereign box has no cap) and `hpc-eval`'s is `public`
(ADR-0003); both are checked here, the same function, either direction.
"""

from __future__ import annotations


class Classification:
    """Ordered classification lattice: PUBLIC < INTERNAL < CONFIDENTIAL."""

    PUBLIC = "PUBLIC"
    INTERNAL = "INTERNAL"
    CONFIDENTIAL = "CONFIDENTIAL"

    _ORDER = {PUBLIC: 0, INTERNAL: 1, CONFIDENTIAL: 2}

    @classmethod
    def rank(cls, level: str) -> int:
        try:
            return cls._ORDER[level]
        except KeyError:  # fail closed on an unknown marking
            raise ValueError(f"unknown classification {level!r}") from None

    @classmethod
    def exceeds(cls, resource_level: str, task_level: str) -> bool:
        """True when `resource_level` is above `task_level` (a DENY)."""
        return cls.rank(resource_level) > cls.rank(task_level)


__all__ = ["Classification"]
