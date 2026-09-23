"""Typed shapes for every `registry/*.yaml` file.

Every model here is strict (`extra="forbid"`): an unknown field is a
validation error, not a warning, exactly as `registry/AGENTS.md` demands --
"a registry that silently ignores a typo will cost someone a day, and the
error will surface as a routing decision nobody can explain."

Two different kinds of vocabulary meet in this file and are treated
differently on purpose:

- **Open** vocabularies -- event names, tool capability names -- are never
  `Literal[...]`. Closing them is the named failure mode in root `AGENTS.md`
  ("the closed vocabulary... sixteen event types, and anything new was
  rejected"). They are validated as non-empty strings and nothing more.
- **Closed, small, operationally-meaningful** vocabularies -- a tool's
  `side_effect`, a model's `runtime`, a template section's `type` -- *are*
  `Literal[...]`, validated against exactly the values this repo currently
  defines. This is not the same mistake: these values are not "one more
  tool the user configured", they are branches other code (the policy
  evaluator, the gateway's provider selection, the deliverables verifier)
  is written against today. A new one is a deliberate, visible one-line
  diff here, not silent acceptance of a typo that then matches nothing.

`classification_ceiling` fields (on a profile, a model, a tool) are the one
piece of shared validation: `citadel_contracts.classification.Classification`
is the single lattice definition repo-wide, so this module imports it rather
than re-listing PUBLIC/INTERNAL/CONFIDENTIAL a fourth time. Two of these
fields (`models.demo-local.yaml`, `tools.yaml`) previously said `restricted`
-- not a level the lattice defines -- which is exactly the bug this
validation exists to catch; see those files' own header comments for the
fix.
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional, Union

import jsonschema
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator
from typing_extensions import Annotated

from citadel_contracts.classification import Classification


class _Strict(BaseModel):
    """Base for every registry model: unknown fields are errors, not silent
    passengers."""

    model_config = ConfigDict(extra="forbid")


def _known_classification(value: str) -> str:
    """Fail closed on a value the lattice does not define. Uppercased once,
    here, at the boundary that reads the registry -- `classification.py`'s
    own docstring names this exact function as that boundary."""
    upper = value.upper()
    Classification.rank(upper)  # raises ValueError; pydantic wraps it
    return upper


ClassificationLevel = Annotated[str, AfterValidator(_known_classification)]


# ---------------------------------------------------------------------------
# profiles.yaml
# ---------------------------------------------------------------------------


class OllamaInference(_Strict):
    """`demo-local`'s shape: one fixed, locally-known endpoint."""

    provider: Literal["ollama"]
    endpoint: str
    endpoint_source: Literal["static"]


class SlurmInference(_Strict):
    """`hpc-eval`'s shape: SLURM allocates a node per job, so the endpoint is
    not known until the sbatch script writes it -- `endpoint` is null in the
    registry and `endpoint_file` names where to read it from at runtime."""

    provider: Literal["vllm"]
    endpoint: Optional[str] = None
    endpoint_source: Literal["slurm_allocation"]
    endpoint_file: str


InferenceConfig = Annotated[
    Union[OllamaInference, SlurmInference], Field(discriminator="provider")
]


class RuntimeManagedResidency(_Strict):
    """`demo-local`: Ollama owns the admission queue; Citadel drives it."""

    strategy: Literal["runtime_managed"]
    max_loaded_models: int
    keep_alive_resident: str
    keep_alive_transient: str


class AllResidentResidency(_Strict):
    """`hpc-eval`: nothing swaps, so residency is just the tensor-parallel
    shape of the allocation."""

    strategy: Literal["all_resident"]
    tensor_parallel_size: int


ResidencyConfig = Annotated[
    Union[RuntimeManagedResidency, AllResidentResidency], Field(discriminator="strategy")
]


class Profile(_Strict):
    """One entry under `profiles:` in `registry/profiles.yaml`. `name` is
    injected from the YAML mapping key, not read from the entry's own body
    -- the file does not repeat the name inside each profile."""

    name: str
    role: str
    description: str
    models: str
    inference: InferenceConfig
    residency: ResidencyConfig
    gpu_admission: int
    cpu_admission: int
    classification_ceiling: ClassificationLevel
    sovereign: bool
    single_box: bool = False
    notes: str = ""


# ---------------------------------------------------------------------------
# models.<profile>.yaml
# ---------------------------------------------------------------------------


class ModelEntry(_Strict):
    """One entry under `models:` in a `models.*.yaml` file. `enabled` is
    almost always `False` -- registry/AGENTS.md: "models are not pulled
    automatically" -- and this schema does not itself enforce that; the
    human-approval gate is a workflow property, not a type, and is instead
    the subject of a structural/process check, not this loader."""

    id: str
    enabled: bool
    tag: str
    runtime: Literal["ollama", "vllm"]
    modalities: List[Literal["text", "image"]]
    capabilities: List[str] = Field(min_length=1)  # open vocabulary, deliberately not Literal
    context_window: int
    vram_gb: float
    quality_tier: Literal["standard", "high", "reference"]
    classification_ceiling: ClassificationLevel
    resident: bool
    fallback: List[str]
    swap_cost_s: Optional[float] = None
    notes: str = ""


# ---------------------------------------------------------------------------
# tools.yaml
# ---------------------------------------------------------------------------


class ToolEntry(_Strict):
    """One entry under `tools:` in `registry/tools.yaml`. `schema` is a JSON
    Schema object describing the tool's call arguments; validated here as a
    real JSON Schema (not just "is a dict") so a malformed tool schema fails
    at registry-load time, not at the tool's first invocation."""

    name: str
    package: str
    side_effect: Literal["read", "write", "execute"]
    required_capabilities: List[str]  # open vocabulary
    classification_ceiling: ClassificationLevel
    requires_receipt: bool
    schema_: Dict[str, Any] = Field(alias="schema")
    description: str = ""  # one line a planner reads; phrasing for a model, not policy
    notes: str = ""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    @model_validator(mode="after")
    def _package_is_a_tools_module(self) -> "ToolEntry":
        if not (self.package == "citadel_tools" or self.package.startswith("citadel_tools.")):
            raise ValueError(
                f"tool {self.name!r} declares package {self.package!r}, which is not "
                f"citadel_tools or a submodule of it -- every tool lives in citadel_tools "
                f"(root AGENTS.md's module map)"
            )
        return self

    @model_validator(mode="after")
    def _schema_is_well_formed_json_schema(self) -> "ToolEntry":
        try:
            jsonschema.Draft7Validator.check_schema(self.schema_)
        except jsonschema.exceptions.SchemaError as exc:
            raise ValueError(
                f"tool {self.name!r} has an invalid JSON Schema for its call arguments: {exc.message}"
            ) from exc
        return self


# ---------------------------------------------------------------------------
# policy.yaml
# ---------------------------------------------------------------------------


#: The closed set of comparison operators the policy DSL currently defines.
#: A `when` clause value is either a literal (direct equality) or a single-key
#: mapping whose key is one of these -- validated here so a typo'd operator
#: (a deny rule that would then silently never match anything) fails at
#: registry-load time. The operators' *runtime* semantics belong to the
#: policy evaluator (`docs/PLAN-M0.md` task 8 / citadel_tools), not this
#: loader -- this only proves the rule is well-formed enough to be
#: evaluable.
_KNOWN_POLICY_OPERATORS = frozenset(
    {"not_in_lattice", "exceeds", "disjoint_from", "contains_all", "in"}
)


def _validate_when_clause(when: Dict[str, Any]) -> Dict[str, Any]:
    if not when:
        raise ValueError("a rule's `when` clause is empty -- a rule must test something")
    for path, value in when.items():
        if not isinstance(path, str) or "." not in path:
            raise ValueError(
                f"`when` key {path!r} is not a <subject>.<field> path "
                f"(e.g. 'resource.classification', 'tool.side_effect')"
            )
        if isinstance(value, dict):
            if len(value) != 1:
                raise ValueError(
                    f"`when.{path}` has {len(value)} operator keys; exactly one is expected "
                    f"(e.g. {{exceeds: actor.classification_max}})"
                )
            (operator,) = value.keys()
            if operator not in _KNOWN_POLICY_OPERATORS:
                raise ValueError(
                    f"`when.{path}` uses unknown operator {operator!r}; known operators are "
                    f"{sorted(_KNOWN_POLICY_OPERATORS)}"
                )
    return when


WhenClause = Annotated[Dict[str, Any], AfterValidator(_validate_when_clause)]


class PolicyRule(_Strict):
    """One entry under `rules:` in `registry/policy.yaml`. Order matters --
    first match wins -- so this model does not sort or deduplicate; the
    loader preserves file order exactly."""

    id: str
    effect: Literal["allow", "deny"]
    when: WhenClause
    reason: Optional[str] = None
    notes: Optional[str] = None

    @model_validator(mode="after")
    def _deny_rules_carry_a_reason(self) -> "PolicyRule":
        # Every current deny rule names a machine-readable `reason`, and it
        # is what the audit event for the resulting denial records --
        # packages/platform/AGENTS.md: "a denial that is not recorded did
        # not happen, as far as an auditor is concerned." A deny with no
        # reason would record nothing to explain it by.
        if self.effect == "deny" and not self.reason:
            raise ValueError(f"deny rule {self.id!r} has no `reason`")
        return self


# ---------------------------------------------------------------------------
# roles.yaml
# ---------------------------------------------------------------------------


class RoleEntry(_Strict):
    """One entry under `roles:` in `registry/roles.yaml`. `role` is CLOSED --
    it must match exactly the values `registry/policy.yaml`'s `actor.role`
    rules and migration 0005's `role` column `CHECK` constraint define
    (`engineer`, `approver`, `admin`); a fourth role is a deliberate,
    visible one-line diff in those places, not a silently-accepted typo
    that then grants nothing. `capabilities` is OPEN, the same vocabulary
    `ToolEntry.required_capabilities` uses, for the same reason: a new
    capability is a new tool's need, not a fixed catalogue. Required, not
    defaulted, so a role entry that grants nothing says `capabilities: []`
    explicitly rather than omitting the field and leaving a reader to guess
    whether that was deliberate."""

    role: Literal["engineer", "approver", "admin"]
    capabilities: List[str]  # open vocabulary
    notes: str = ""


# ---------------------------------------------------------------------------
# events.yaml
# ---------------------------------------------------------------------------


class EventDefinition(_Strict):
    """One entry under `events:` in `registry/events.yaml`. `name` is an
    OPEN vocabulary -- this is deliberately not `Literal[...]`; the closed
    16-type enum is the named failure mode this repo exists to avoid. What
    *is* validated is that `name` is well-formed and that the file assigns
    each one exactly once (`citadel_platform.registry.loader` checks
    uniqueness across the whole file, since one entry alone cannot)."""

    name: str = Field(min_length=1)
    severity: Literal["info", "warning", "error", "critical"]
    audit: bool
    trace: bool


# ---------------------------------------------------------------------------
# templates.yaml
# ---------------------------------------------------------------------------


class TemplateSection(_Strict):
    key: str
    required: bool
    type: Literal["text", "rich_text", "list", "table", "date", "value"]
    min_items: Optional[int] = None
    cited: bool = False


class Grounding(_Strict):
    quantitative_claims: Literal["must_cite"]
    derived_value_tolerance: Optional[float] = None


class TemplateEntry(_Strict):
    id: str
    format: Literal["docx", "xlsx"]
    file: str
    classification_markings: bool
    approval_block: bool
    revision_history: bool
    sections: List[TemplateSection] = Field(min_length=1)
    grounding: Grounding


__all__ = [
    "ClassificationLevel",
    "OllamaInference",
    "SlurmInference",
    "InferenceConfig",
    "RuntimeManagedResidency",
    "AllResidentResidency",
    "ResidencyConfig",
    "Profile",
    "ModelEntry",
    "ToolEntry",
    "PolicyRule",
    "RoleEntry",
    "EventDefinition",
    "TemplateSection",
    "Grounding",
    "TemplateEntry",
]
