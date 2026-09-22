"""The one place `registry/*.yaml` gets read.

`load_registry` is the entry point: given a profile name and the path to the
`registry/` directory, it loads and strictly validates every registry file,
cross-checks what can only be checked once the whole file is in hand (no
duplicate ids, no `fallback` pointing at a model that does not exist), and
returns one immutable `Registry` -- or raises `RegistryError` naming the
file and line of the first problem. There is no partial success: a registry
that loads most of itself and silently drops the rest is the "routing
decision nobody can explain" `registry/AGENTS.md` warns about.

Two files (`registry/events.yaml`, and the model/tool/policy/template files)
are profile-*independent* -- only `models.<profile>.yaml` varies by
profile, per `profiles.yaml` itself, which is the only file naming a
per-profile filename anywhere in `registry/`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple, Type, TypeVar

import yaml
from pydantic import BaseModel, ValidationError

from citadel_contracts.events import EventRegistry

from .errors import RegistryError
from .schema import (
    EventDefinition,
    ModelEntry,
    PolicyRule,
    Profile,
    RoleEntry,
    TemplateEntry,
    ToolEntry,
)
from .yaml_source import load_yaml_with_lines

_M = TypeVar("_M", bound=BaseModel)


# ---------------------------------------------------------------------------
# shared machinery
# ---------------------------------------------------------------------------


def _load_document(registry_dir: Path, filename: str) -> Tuple[Any, Dict[int, int]]:
    path = registry_dir / filename
    if not path.exists():
        raise RegistryError(filename, "file does not exist")
    try:
        return load_yaml_with_lines(path)
    except yaml.YAMLError as exc:
        raise RegistryError(filename, f"not valid YAML: {exc}") from exc


def _format_pydantic_errors(exc: ValidationError) -> str:
    parts = []
    for error in exc.errors():
        loc = ".".join(str(p) for p in error["loc"]) or "<entry>"
        parts.append(f"{loc}: {error['msg']}")
    return "; ".join(parts)


def _validate_one(
    model_cls: Type[_M],
    raw: Any,
    *,
    filename: str,
    line_of: Dict[int, int],
    what: str,
    identifier: str,
) -> _M:
    if not isinstance(raw, dict):
        raise RegistryError(
            filename, f"{what} {identifier!r}: expected a mapping, got {type(raw).__name__}"
        )
    try:
        return model_cls.model_validate(raw)
    except ValidationError as exc:
        raise RegistryError(
            filename,
            f"{what} {identifier!r}: {_format_pydantic_errors(exc)}",
            line=line_of.get(id(raw)),
        ) from exc


def _validate_list(
    model_cls: Type[_M],
    raw_list: Any,
    *,
    filename: str,
    line_of: Dict[int, int],
    what: str,
    key_field: str,
) -> Tuple[_M, ...]:
    if not isinstance(raw_list, list):
        raise RegistryError(filename, f"expected a list of {what}s, got {type(raw_list).__name__}")

    entries: List[_M] = []
    seen: Dict[str, int] = {}  # key_field value -> 1-based position, for a clear duplicate message
    for position, raw in enumerate(raw_list, start=1):
        identifier = raw.get(key_field, f"<entry #{position}>") if isinstance(raw, dict) else f"<entry #{position}>"
        entry = _validate_one(
            model_cls, raw, filename=filename, line_of=line_of, what=what, identifier=str(identifier)
        )
        key_value = getattr(entry, key_field)
        if key_value in seen:
            raise RegistryError(
                filename,
                f"duplicate {what} {key_field} {key_value!r} (first seen as entry #{seen[key_value]}, "
                f"again as entry #{position})",
                line=line_of.get(id(raw)),
            )
        seen[key_value] = position
        entries.append(entry)
    return tuple(entries)


# ---------------------------------------------------------------------------
# per-file loaders
# ---------------------------------------------------------------------------


def load_profiles(registry_dir: Path) -> Dict[str, Profile]:
    """`registry/profiles.yaml`. Keyed by profile name -- a dict, not a
    list, is the one file shaped this way, because a profile is selected by
    name (`CITADEL_PROFILE`), never iterated."""
    filename = "profiles.yaml"
    document, line_of = _load_document(registry_dir, filename)
    raw_profiles = (document or {}).get("profiles")
    if not isinstance(raw_profiles, dict) or not raw_profiles:
        raise RegistryError(filename, "top-level `profiles:` must be a non-empty mapping")

    profiles: Dict[str, Profile] = {}
    for name, raw in raw_profiles.items():
        if not isinstance(raw, dict):
            raise RegistryError(filename, f"profile {name!r}: expected a mapping, got {type(raw).__name__}")
        profiles[name] = _validate_one(
            Profile,
            {**raw, "name": name},
            filename=filename,
            line_of=line_of,
            what="profile",
            identifier=name,
        )
    return profiles


def load_models(registry_dir: Path, filename: str) -> Tuple[ModelEntry, ...]:
    """One `models.<profile>.yaml`. `filename` comes from the active
    profile's own `models:` field -- never guessed or globbed, so a
    tombstoned file (`models.dev-8gb.yaml`, `models.cluster.yaml` before
    they were deleted) can never be loaded by accident; nothing in
    `profiles.yaml` points at one."""
    document, line_of = _load_document(registry_dir, filename)
    raw_models = (document or {}).get("models")
    models = _validate_list(
        ModelEntry, raw_models, filename=filename, line_of=line_of, what="model", key_field="id"
    )

    known_ids = {m.id for m in models}
    for model in models:
        unknown = [f for f in model.fallback if f not in known_ids]
        if unknown:
            raise RegistryError(
                filename,
                f"model {model.id!r} has `fallback` entries that do not exist in this file: {unknown}",
            )
    return models


def load_tools(registry_dir: Path) -> Tuple[ToolEntry, ...]:
    filename = "tools.yaml"
    document, line_of = _load_document(registry_dir, filename)
    return _validate_list(
        ToolEntry,
        (document or {}).get("tools"),
        filename=filename,
        line_of=line_of,
        what="tool",
        key_field="name",
    )


def load_policy(registry_dir: Path) -> Tuple[PolicyRule, ...]:
    filename = "policy.yaml"
    document, line_of = _load_document(registry_dir, filename)
    return _validate_list(
        PolicyRule,
        (document or {}).get("rules"),
        filename=filename,
        line_of=line_of,
        what="policy rule",
        key_field="id",
    )


def load_roles(registry_dir: Path) -> Tuple[RoleEntry, ...]:
    filename = "roles.yaml"
    document, line_of = _load_document(registry_dir, filename)
    return _validate_list(
        RoleEntry, (document or {}).get("roles"), filename=filename, line_of=line_of, what="role", key_field="role"
    )


def load_events(registry_dir: Path) -> Tuple[EventDefinition, ...]:
    filename = "events.yaml"
    document, line_of = _load_document(registry_dir, filename)
    return _validate_list(
        EventDefinition,
        (document or {}).get("events"),
        filename=filename,
        line_of=line_of,
        what="event",
        key_field="name",
    )


def load_templates(registry_dir: Path) -> Tuple[TemplateEntry, ...]:
    filename = "templates.yaml"
    document, line_of = _load_document(registry_dir, filename)
    return _validate_list(
        TemplateEntry,
        (document or {}).get("templates"),
        filename=filename,
        line_of=line_of,
        what="template",
        key_field="id",
    )


# ---------------------------------------------------------------------------
# the whole registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Registry:
    """Everything a `CITADEL_PROFILE` needs, already loaded and validated.
    Immutable: built once at startup by `load_registry`, handed to whoever
    asks. Nothing downstream re-parses YAML."""

    profile: Profile
    models: Tuple[ModelEntry, ...]
    tools: Tuple[ToolEntry, ...]
    policy: Tuple[PolicyRule, ...]
    roles: Tuple[RoleEntry, ...]
    events: Tuple[EventDefinition, ...]
    templates: Tuple[TemplateEntry, ...]

    def event_registry(self) -> EventRegistry:
        """A fresh `citadel_contracts.events.EventRegistry`, populated from
        this registry's `events.yaml`. `citadel_contracts` ships the
        open-registration mechanism; this is the one place -- startup --
        that actually populates it, per that package's own design (it is
        instantiable, not a module-level singleton, deliberately)."""
        registry = EventRegistry()
        registry.register_many(e.name for e in self.events)
        return registry

    def tool(self, name: str) -> ToolEntry:
        for entry in self.tools:
            if entry.name == name:
                return entry
        raise KeyError(f"no tool named {name!r} in this registry")

    def model(self, model_id: str) -> ModelEntry:
        for entry in self.models:
            if entry.id == model_id:
                return entry
        raise KeyError(f"no model {model_id!r} in this profile's registry")

    def capabilities_for(self, role: str) -> Tuple[str, ...]:
        for entry in self.roles:
            if entry.role == role:
                return tuple(entry.capabilities)
        raise KeyError(f"no role {role!r} in this registry")


def load_registry(profile_name: str, registry_dir: Path) -> Registry:
    """The single entry point. Loads `profiles.yaml` first to resolve which
    `models.*.yaml` the named profile uses, then loads everything else
    (profile-independent), validating strictly throughout. Raises
    `RegistryError` -- naming the file and, where known, the line -- on the
    first problem found; there is no partial `Registry`."""
    profiles = load_profiles(registry_dir)
    if profile_name not in profiles:
        raise RegistryError(
            "profiles.yaml",
            f"CITADEL_PROFILE={profile_name!r} is not a profile this registry defines "
            f"(known profiles: {sorted(profiles)})",
        )
    profile = profiles[profile_name]

    return Registry(
        profile=profile,
        models=load_models(registry_dir, profile.models),
        tools=load_tools(registry_dir),
        policy=load_policy(registry_dir),
        roles=load_roles(registry_dir),
        events=load_events(registry_dir),
        templates=load_templates(registry_dir),
    )


def load_registry_from_env(
    registry_dir: Path, *, env: Optional[Iterable[Tuple[str, str]]] = None
) -> Registry:
    """Convenience wrapper reading `CITADEL_PROFILE` from the process
    environment. `load_registry` itself takes the profile name as a plain
    argument -- dependency injection, not environment-reading, is what
    tests and callers that already know the profile should use; this
    wrapper exists only for the process entry points (`services/*`) that
    genuinely start from the environment.
    """
    environ = dict(env) if env is not None else os.environ
    profile_name = environ.get("CITADEL_PROFILE")
    if not profile_name:
        raise RegistryError("profiles.yaml", "CITADEL_PROFILE is not set in the environment")
    return load_registry(profile_name, registry_dir)


__all__ = [
    "Registry",
    "load_registry",
    "load_registry_from_env",
    "load_profiles",
    "load_models",
    "load_tools",
    "load_policy",
    "load_roles",
    "load_events",
    "load_templates",
]
