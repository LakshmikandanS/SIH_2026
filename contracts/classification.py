"""The classification lattice.

Ported from `app/db/state_machines.py::Classification` (P1 §1: "Moving the
existing definitions here is not a rename for tidiness"). Split into its own
file because two independent things need it from P1 onward -- the Policy
Engine's existing `resource.classification > task.classification` check, and
the Decision Receipt's `resource_digest()` -- and neither needs the rest of
`state_machines.py` to get it. Both must agree on exactly this lattice, which
is the whole reason it belongs in `contracts/` rather than behind an import
of the application package.

Comparison is needed by the Policy Engine (design doc §6.7) and the Verifier
(§6.10); defined once, here, unchanged from the original.
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
        """True when `resource_level` is above `task_level` (a DENY in §6.7)."""
        return cls.rank(resource_level) > cls.rank(task_level)


__all__ = ["Classification"]
