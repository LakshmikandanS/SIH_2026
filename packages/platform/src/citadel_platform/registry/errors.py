"""Registry validation failures.

"A deliberately misspelled field fails loudly with the file and line"
(`docs/PLAN-M0.md` task 4) is a concrete promise, not a docstring
aspiration. `RegistryError` is what keeps it: every one names the file and,
when known, the 1-based source line of the entry that failed -- never a bare
pydantic traceback, which names a field path but not where in the YAML that
path came from.
"""

from __future__ import annotations

from typing import Optional


class RegistryError(ValueError):
    """A `registry/*.yaml` file failed strict validation.

    Raised for every kind of registry failure this package detects: a file
    that is not valid YAML, an entry that fails its schema (unknown field,
    wrong type, a classification the lattice does not define), a duplicate
    id, or a cross-reference (a model's `fallback`) to an id that does not
    exist. Always fail closed -- a registry that loads *some* of itself and
    silently drops the rest is worse than one that refuses to load at all.
    """

    def __init__(self, file: str, message: str, *, line: Optional[int] = None) -> None:
        location = f"{file}:{line}" if line is not None else file
        super().__init__(f"{location}: {message}")
        self.file = file
        self.line = line
        self.reason = message


__all__ = ["RegistryError"]
