"""A YAML loader that remembers where each entry came from.

PyYAML -- the only YAML library available anywhere this repo runs; see root
`AGENTS.md`'s sandbox note -- discards source position the moment a mapping
or sequence is constructed. `ruamel.yaml` tracks this natively and would
make this file unnecessary, but it is not installed in the dev sandbox this
package was first built in and is not assumed for the target WSL2 machine
either, so this repo does not depend on it.

Strict registry validation needs the position back. `docs/PLAN-M0.md` task 4
promises "a deliberately misspelled field fails loudly with the file and
line", and a bare `pydantic.ValidationError` names a field path, never a
line. This module does the minimum to keep that promise.

Deliberately scoped to *top-level entries*, not every nested field: an item
in a top-level list (a model in `models.yaml`, a tool in `tools.yaml`, a
rule in `policy.yaml`, an event in `events.yaml`, a template in
`templates.yaml`), or a value in a top-level mapping (a profile in
`profiles.yaml`, keyed by profile name). That is the granularity a registry
entry is actually identified by -- a model id, a tool name, a profile name
-- and it is what `citadel_platform.registry.loader` reports against. A
fully general recursive line-tracker (one line number per nested field, the
way `ruamel.yaml`'s round-trip loader works) would need to reimplement much
of what that library already does, for a promise this package does not
make.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Tuple

import yaml

# PyYAML ships no type information (see pyproject.toml's mypy override for this
# module), so mypy sees `yaml.SafeLoader` as `Any` -- and --strict's
# disallow_subclassing_any rightly objects to subclassing that, since it would make
# every method inherited from it silently untyped too. There is no way to give a
# third-party base class real types without vendoring a stub package this sandbox
# cannot install (see root AGENTS.md's sandbox note); PyYAML's own loader-customisation
# mechanism is inheritance, so subclassing it is not optional. This is the one, narrow,
# unavoidable case for that ignore in the whole codebase.


class _LineTrackingLoader(yaml.SafeLoader):  # type: ignore[misc]
    """A `SafeLoader` whose mapping/sequence constructors also record the
    1-based source line each one started on, in `self.line_of` -- keyed by
    `id()` of the constructed object, never as an extra key inside the
    object itself. An injected key would be indistinguishable from a real
    YAML field and would (rightly) trip strict "unknown field" validation
    the instant pydantic saw it.
    """

    line_of: Dict[int, int]


def _construct_mapping(loader: _LineTrackingLoader, node: yaml.MappingNode) -> Any:
    mapping = yaml.SafeLoader.construct_mapping(loader, node, deep=True)
    loader.line_of[id(mapping)] = node.start_mark.line + 1
    return mapping


def _construct_sequence(loader: _LineTrackingLoader, node: yaml.SequenceNode) -> Any:
    sequence = yaml.SafeLoader.construct_sequence(loader, node, deep=True)
    loader.line_of[id(sequence)] = node.start_mark.line + 1
    return sequence


_LineTrackingLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping
)
_LineTrackingLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_SEQUENCE_TAG, _construct_sequence
)


def load_yaml_with_lines(path: Path) -> Tuple[Any, Dict[int, int]]:
    """Parse `path`. Returns `(document, line_of)`.

    `line_of` maps `id(obj)` to the 1-based source line for every
    mapping/sequence PyYAML constructed while parsing this document, at
    every depth -- not just the top level. Look up a specific entry's line
    with `line_of.get(id(entry))` immediately after this call, before any
    copy, re-construction or mutation of `entry`: `id()` identifies a living
    Python object, not a value, so it is only meaningful while the object
    from *this* parse is still the one you are holding (keep `document`, or
    the entry itself, referenced -- this function does not).

    Raises `yaml.YAMLError` on malformed YAML, uninterpreted -- the caller
    (`citadel_platform.registry.loader`) wraps it in a `RegistryError` that
    names the file.
    """
    loader = _LineTrackingLoader(path.read_text(encoding="utf-8"))
    loader.line_of = {}
    try:
        document = loader.get_single_data()
    finally:
        loader.dispose()
    return document, loader.line_of


__all__ = ["load_yaml_with_lines"]
