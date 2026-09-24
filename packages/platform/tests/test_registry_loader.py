"""citadel_platform.registry -- loads and strictly validates registry/*.yaml.

Two kinds of test live here. The end-to-end ones load the REAL files under
the repo's own `registry/` directory: the loader is only trustworthy if it
actually accepts the registry this repo ships (and these tests are what
caught `classification_ceiling: restricted` in `models.demo-local.yaml` and
`tools.yaml` -- not a value the lattice defines, fixed in those files'
own header comments; this is the automated version of the reading that
found it). The rest are synthetic: a minimal registry, in a pytest
`tmp_path`, with exactly one thing wrong, proving each strict-validation
promise actually holds rather than merely reading like it should.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from citadel_contracts.events import EventRegistry
from citadel_platform.registry import RegistryError, load_registry, load_registry_from_env
from citadel_platform.registry.loader import (
    load_events,
    load_models,
    load_policy,
    load_profiles,
    load_roles,
    load_templates,
    load_tools,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
REGISTRY_DIR = REPO_ROOT / "registry"


def _write(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# the real registry -- end to end
# ---------------------------------------------------------------------------


def test_loads_the_real_demo_local_registry():
    registry = load_registry("demo-local", REGISTRY_DIR)

    assert registry.profile.name == "demo-local"
    assert registry.profile.classification_ceiling == "CONFIDENTIAL"
    assert registry.profile.single_box is True

    model_ids = {m.id for m in registry.models}
    assert model_ids == {"reason-general", "reason-code", "reason-large", "vision-doc", "embed-text"}
    # The bug this test suite exists to catch: every model here must be a
    # real lattice value now, not "restricted". Uppercase: the YAML says
    # "confidential", but `_known_classification` normalises it once, at
    # the boundary that reads the registry (schema.py) -- this asserts the
    # normalised value, not the as-written YAML casing.
    assert {m.classification_ceiling for m in registry.models} == {"CONFIDENTIAL"}

    tool_names = {t.name for t in registry.tools}
    assert "docs.search" in tool_names and "code.run" in tool_names
    assert len(registry.tools) == 15

    assert len(registry.policy) == 8
    assert registry.policy[0].id == "deny-unknown-classification"  # order preserved

    role_names = {r.role for r in registry.roles}
    assert role_names == {"engineer", "approver", "admin"}
    assert set(registry.capabilities_for("approver")) == {"retrieval"}

    assert len(registry.events) == 46
    assert len(registry.templates) == 4


def test_loads_the_real_hpc_eval_registry():
    registry = load_registry("hpc-eval", REGISTRY_DIR)

    assert registry.profile.name == "hpc-eval"
    assert registry.profile.classification_ceiling == "PUBLIC"
    assert registry.profile.single_box is False  # absent in the YAML -- defaults False

    model_ids = {m.id for m in registry.models}
    assert model_ids == {"reason-general", "reason-code", "vision-doc", "embed-text", "judge"}
    assert {m.classification_ceiling for m in registry.models} == {"PUBLIC"}

    # Profile-independent files are identical regardless of which profile loaded them.
    assert len(registry.tools) == 15
    assert len(registry.policy) == 8
    assert len(registry.roles) == 3
    assert len(registry.events) == 46


def test_unknown_profile_name_fails_loudly():
    with pytest.raises(RegistryError, match="nonexistent"):
        load_registry("nonexistent", REGISTRY_DIR)


def test_event_registry_populates_from_the_real_events_yaml():
    registry = load_registry("demo-local", REGISTRY_DIR)
    events = registry.event_registry()

    assert isinstance(events, EventRegistry)
    assert len(events) == 46
    assert "policy.decision" in events
    assert events.is_registered("receipt.rejected")
    assert not events.is_registered("made.up.event")


def test_registry_tool_and_model_lookup_helpers():
    registry = load_registry("demo-local", REGISTRY_DIR)

    assert registry.tool("docs.search").side_effect == "read"
    assert registry.model("reason-general").runtime == "ollama"
    assert "retrieval" in registry.capabilities_for("engineer")

    with pytest.raises(KeyError):
        registry.tool("no.such.tool")
    with pytest.raises(KeyError):
        registry.model("no-such-model")
    with pytest.raises(KeyError):
        registry.capabilities_for("no-such-role")


def test_load_registry_from_env_reads_citadel_profile():
    registry = load_registry_from_env(REGISTRY_DIR, env=[("CITADEL_PROFILE", "hpc-eval")])
    assert registry.profile.name == "hpc-eval"


def test_load_registry_from_env_fails_loudly_when_unset():
    with pytest.raises(RegistryError, match="CITADEL_PROFILE"):
        load_registry_from_env(REGISTRY_DIR, env=[])


# ---------------------------------------------------------------------------
# strict validation -- synthetic, minimal, one defect each
# ---------------------------------------------------------------------------


_VALID_MODEL_LINES = [
    "models:",
    "  - id: reason-general",
    "    enabled: false",
    '    tag: "qwen3:4b"',
    "    runtime: ollama",
    "    modalities: [text]",
    "    capabilities: [reasoning]",
    "    context_window: 32768",
    "    vram_gb: 2.6",
    "    quality_tier: standard",
    "    classification_ceiling: confidential",
    "    resident: true",
    "    fallback: []",
]


def test_an_unknown_field_fails_with_the_file_and_the_entrys_line(tmp_path):
    lines = [*_VALID_MODEL_LINES, "    bogus_field: 1"]
    _write(tmp_path / "models.yaml", "\n".join(lines) + "\n")
    entry_line = lines.index("  - id: reason-general") + 1  # 1-based

    with pytest.raises(RegistryError) as excinfo:
        load_models(tmp_path, "models.yaml")

    err = excinfo.value
    assert err.file == "models.yaml"
    assert err.line == entry_line
    assert "bogus_field" in str(err)


def test_an_unknown_classification_fails(tmp_path):
    lines = [
        line.replace("classification_ceiling: confidential", "classification_ceiling: top_secret")
        for line in _VALID_MODEL_LINES
    ]
    _write(tmp_path / "models.yaml", "\n".join(lines) + "\n")

    with pytest.raises(RegistryError, match="unknown classification"):
        load_models(tmp_path, "models.yaml")


def test_duplicate_model_id_fails(tmp_path):
    # One `models:` key with two entries -- not two `models:` blocks, which
    # YAML would silently collapse into one key (the last wins) and this
    # test would never actually exercise the duplicate-id check at all.
    body = _VALID_MODEL_LINES[1:]
    content = "\n".join([_VALID_MODEL_LINES[0], *body, *body]) + "\n"
    _write(tmp_path / "models.yaml", content)

    with pytest.raises(RegistryError, match="duplicate model id 'reason-general'"):
        load_models(tmp_path, "models.yaml")


def test_fallback_to_a_nonexistent_model_fails(tmp_path):
    lines = [line.replace("fallback: []", "fallback: [does-not-exist]") for line in _VALID_MODEL_LINES]
    _write(tmp_path / "models.yaml", "\n".join(lines) + "\n")

    with pytest.raises(RegistryError, match="does-not-exist"):
        load_models(tmp_path, "models.yaml")


_VALID_TOOL = """
tools:
  - name: docs.search
    package: citadel_tools.docs
    side_effect: read
    required_capabilities: [retrieval]
    classification_ceiling: confidential
    requires_receipt: true
    schema:
      type: object
      required: [query]
      additionalProperties: false
      properties:
        query: { type: string, minLength: 1 }
"""


def test_duplicate_tool_name_fails(tmp_path):
    duplicated = _VALID_TOOL + _VALID_TOOL.replace("tools:\n", "")
    _write(tmp_path / "tools.yaml", duplicated)

    with pytest.raises(RegistryError, match="duplicate tool name 'docs.search'"):
        load_tools(tmp_path)


def test_tool_with_an_invalid_json_schema_fails(tmp_path):
    broken = _VALID_TOOL.replace("type: object", "type: not-a-real-json-schema-type")
    _write(tmp_path / "tools.yaml", broken)

    with pytest.raises(RegistryError, match="invalid JSON Schema"):
        load_tools(tmp_path)


def test_tool_package_outside_citadel_tools_fails(tmp_path):
    broken = _VALID_TOOL.replace("package: citadel_tools.docs", "package: some_other_package")
    _write(tmp_path / "tools.yaml", broken)

    with pytest.raises(RegistryError, match="not citadel_tools"):
        load_tools(tmp_path)


def test_policy_rule_with_unknown_operator_fails(tmp_path):
    _write(
        tmp_path / "policy.yaml",
        """
rules:
  - id: deny-something
    effect: deny
    reason: because
    when:
      resource.classification: { frobnicates: true }
""",
    )
    with pytest.raises(RegistryError, match="unknown operator 'frobnicates'"):
        load_policy(tmp_path)


def test_policy_deny_rule_without_reason_fails(tmp_path):
    _write(
        tmp_path / "policy.yaml",
        """
rules:
  - id: deny-something
    effect: deny
    when:
      resource.classification: { not_in_lattice: true }
""",
    )
    with pytest.raises(RegistryError, match="no `reason`"):
        load_policy(tmp_path)


def test_policy_rule_with_empty_when_fails(tmp_path):
    _write(
        tmp_path / "policy.yaml",
        """
rules:
  - id: allow-nothing-in-particular
    effect: allow
    when: {}
""",
    )
    with pytest.raises(RegistryError, match="must test something"):
        load_policy(tmp_path)


def test_policy_preserves_file_order_not_id_order(tmp_path):
    _write(
        tmp_path / "policy.yaml",
        """
rules:
  - id: z-rule
    effect: allow
    when:
      tool.side_effect: read
  - id: a-rule
    effect: allow
    when:
      tool.side_effect: write
""",
    )
    rules = load_policy(tmp_path)
    assert [r.id for r in rules] == ["z-rule", "a-rule"]


_VALID_ROLE = """
roles:
  - role: engineer
    capabilities: [retrieval, workspace]
"""


def test_duplicate_role_fails(tmp_path):
    duplicated = _VALID_ROLE + _VALID_ROLE.replace("roles:\n", "")
    _write(tmp_path / "roles.yaml", duplicated)

    with pytest.raises(RegistryError, match="duplicate role role 'engineer'"):
        load_roles(tmp_path)


def test_an_unknown_role_name_fails(tmp_path):
    broken = _VALID_ROLE.replace("role: engineer", "role: overlord")
    _write(tmp_path / "roles.yaml", broken)

    with pytest.raises(RegistryError):
        load_roles(tmp_path)


def test_role_capabilities_must_be_explicit_not_defaulted(tmp_path):
    _write(tmp_path / "roles.yaml", "roles:\n  - role: engineer\n")

    with pytest.raises(RegistryError, match="capabilities"):
        load_roles(tmp_path)


def test_duplicate_event_name_fails(tmp_path):
    _write(
        tmp_path / "events.yaml",
        """
events:
  - { name: task.submitted, severity: info, audit: true, trace: true }
  - { name: task.submitted, severity: warning, audit: false, trace: false }
""",
    )
    with pytest.raises(RegistryError, match="duplicate event name 'task.submitted'"):
        load_events(tmp_path)


_VALID_TEMPLATE = """
templates:
  - id: approval-note
    format: docx
    file: templates/approval-note.docx
    classification_markings: true
    approval_block: true
    revision_history: true
    sections:
      - { key: subject, required: true, type: text }
    grounding:
      quantitative_claims: must_cite
"""


def test_duplicate_template_id_fails(tmp_path):
    duplicated = _VALID_TEMPLATE + _VALID_TEMPLATE.replace("templates:\n", "")
    _write(tmp_path / "templates.yaml", duplicated)

    with pytest.raises(RegistryError, match="duplicate template id 'approval-note'"):
        load_templates(tmp_path)


def test_template_section_requires_a_known_type(tmp_path):
    broken = _VALID_TEMPLATE.replace("type: text", "type: paragraph")
    _write(tmp_path / "templates.yaml", broken)

    with pytest.raises(RegistryError):
        load_templates(tmp_path)


def test_profiles_yaml_requires_a_non_empty_mapping(tmp_path):
    _write(tmp_path / "profiles.yaml", "profiles: {}\n")
    with pytest.raises(RegistryError, match="non-empty mapping"):
        load_profiles(tmp_path)


def test_a_missing_registry_file_fails_loudly(tmp_path):
    with pytest.raises(RegistryError, match="does not exist"):
        load_tools(tmp_path)


def test_malformed_yaml_fails_loudly(tmp_path):
    _write(tmp_path / "tools.yaml", "tools: [this is: not: valid: yaml")
    with pytest.raises(RegistryError, match="not valid YAML"):
        load_tools(tmp_path)
