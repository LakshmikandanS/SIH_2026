"""Loading and strict validation of `registry/*.yaml`.

See `loader.py` for the entry point (`load_registry`), `schema.py` for the
typed shape of every registry file, `errors.py` for what a validation
failure looks like, and `yaml_source.py` for how a failure gets a line
number attached to it.
"""

from __future__ import annotations

from .errors import RegistryError
from .loader import (
    Registry,
    load_events,
    load_models,
    load_policy,
    load_profiles,
    load_registry,
    load_registry_from_env,
    load_roles,
    load_templates,
    load_tools,
)
from .schema import (
    EventDefinition,
    Grounding,
    ModelEntry,
    PolicyRule,
    Profile,
    RoleEntry,
    TemplateEntry,
    TemplateSection,
    ToolEntry,
)

__all__ = [
    "RegistryError",
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
    "Profile",
    "ModelEntry",
    "ToolEntry",
    "PolicyRule",
    "RoleEntry",
    "EventDefinition",
    "TemplateEntry",
    "TemplateSection",
    "Grounding",
]
