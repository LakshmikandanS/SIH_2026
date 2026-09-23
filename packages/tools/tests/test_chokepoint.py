"""The chokepoint, the receipts it issues, the boundaries that verify them, and every
tool behind it -- against the real registry, the real ingested demo corpus, real
Postgres and the real process sandbox.
"""

from __future__ import annotations

import base64
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

import pytest

from citadel_contracts.domain import User
from citadel_platform.audit.log import AuditLog
from citadel_platform.keyring import init_keys, load_receipt_public_key, load_receipt_signing_key
from citadel_platform.storage import DataDir
from citadel_platform.tracing import Tracer
from citadel_tools import (
    Chokepoint,
    DataBoundary,
    LocalSandboxRunner,
    ToolContext,
    actor_facts_for_task,
)
from citadel_tools.calc import evaluate_expression
from citadel_tools.context import Invocation, ToolFailure, ToolOutput
from citadel_tools.plugins import ToolPlugin
from pg_scratch import apply_all_migrations, pg_scratch_db, requires_pgvector
from stack_fixtures import REPO_ROOT, enabled_registry, gateway_for, ingest_corpus, start_fake

ENGINEER_1 = User("demo-engineer-1", "R. Kulkarni", ("engineer",), "internal", "process-engineering")
ENGINEER_2 = User("demo-engineer-2", "S. Nair", ("engineer",), "confidential", "instrumentation")
APPROVER = User("demo-approver", "A. Menon", ("approver",), "confidential", "quality-assurance")


# -- pure ----------------------------------------------------------------------------------


def test_every_manifest_tool_has_a_plugin_and_a_schema():
    chokepoint = Chokepoint(enabled_registry(), signing_key=None)
    assert set(chokepoint._plugins) == {t.name for t in chokepoint.registry.tools}


@pytest.mark.parametrize(
    ("expression", "value", "steps"),
    [
        ("(9.2 - 8.4) / 0.25", 3.2, ["9.2 - 8.4 = 0.8", "0.8 / 0.25 = 3.2"]),
        ("remaining life = (10.0 - 8.4) / 0.25", 6.4, None),
        ("sqrt(16) + 2^3", 12.0, None),
    ],
)
def test_calc_keeps_its_working(expression: str, value: float, steps: Any):
    _label, result, working = evaluate_expression(expression)
    assert abs(result - value) < 1e-9
    if steps is not None:
        assert working == steps


@pytest.mark.parametrize("expression", ["__import__('os')", "open('x')", "a + 1", "1/0", "[1,2]", "9 ** 9999"])
def test_calc_refuses_anything_but_arithmetic(expression: str):
    with pytest.raises(ToolFailure):
        evaluate_expression(expression)


# -- the stack -------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def stack(tmp_path_factory: pytest.TempPathFactory) -> Iterator[SimpleNamespace]:
    from citadel_knowledge.ocr import available

    if not available():
        pytest.skip("tesseract is not installed on this machine")
    registry = enabled_registry()
    fake = start_fake(registry)
    try:
        with pg_scratch_db() as env:
            apply_all_migrations(env)
            data_dir = DataDir(root=tmp_path_factory.mktemp("data"))
            gateway = gateway_for(registry, fake)
            db = ingest_corpus(env, data_dir, gateway)
            keys = tmp_path_factory.mktemp("keys")
            init_keys(keys)
            key_env = {"CITADEL_KEYS_DIR": str(keys)}
            public = load_receipt_public_key(key_env)
            yield SimpleNamespace(
                env=env, db=db, data_dir=data_dir, registry=registry, gateway=gateway,
                audit=AuditLog(env, registry.event_registry()),
                chokepoint=Chokepoint(registry, signing_key=load_receipt_signing_key(key_env)),
                boundary=DataBoundary(public),
                sandbox=LocalSandboxRunner(public),
                public=public,
            )
    finally:
        fake.stop()


def _context(stack: SimpleNamespace, user: User, classification: str = "INTERNAL") -> ToolContext:
    task_id = str(stack.db.scalar(
        "INSERT INTO tasks (goal, submitted_by, classification) VALUES ('test task', "
        "(SELECT id FROM users WHERE external_identity = %(u)s), %(c)s) RETURNING id::text",
        {"u": user.user_id, "c": classification.lower()},
    ))
    return ToolContext(
        task_id=task_id,
        agent_id="agent-test",
        user=user,
        actor=actor_facts_for_task(user, stack.registry, classification),
        task_classification=classification,
        db=stack.db,
        data_dir=stack.data_dir,
        registry=stack.registry,
        registry_dir=REPO_ROOT / "registry",
        boundary=stack.boundary,
        gateway=stack.gateway,
        audit=stack.audit,
        tracer=Tracer(stack.db),
        sandbox=stack.sandbox,
        goal="test task",
    )


def _audit_events(stack: SimpleNamespace, task_id: str) -> list[str]:
    rows = stack.db.query(
        "SELECT event_name FROM audit_log WHERE payload ->> 'task_id' = %(t)s ORDER BY seq", {"t": task_id}
    )
    return [r["event_name"] for r in rows]


@requires_pgvector
@pytest.mark.integration
def test_search_is_allowed_receipted_verified_and_audited(stack: SimpleNamespace):
    ctx = _context(stack, ENGINEER_1)
    result = stack.chokepoint.invoke(ctx, "docs.search", {"query": "E-101 remaining life CML-3"})
    assert result.ok, result.to_dict()
    assert result.decision["rule_id"] == "allow-read-with-capability"
    assert result.receipt["verified_by"] == "data-boundary"
    assert result.output["passages"][0]["evidence_id"] == "E1"
    assert result.evidence and result.evidence[0] == "E1"
    # confidential documents matched and were withheld -- counted, with reasons, never shown
    assert result.output["withheld_documents"]["count"] >= 1
    assert "receipt" not in result.for_model()  # the model sees outcomes, never the token
    events = _audit_events(stack, ctx.task_id)
    assert events[:2] == ["receipt.issued", "policy.decision"]
    assert "retrieval.denied_doc" in events


@requires_pgvector
@pytest.mark.integration
def test_reading_a_document_above_the_task_ceiling_is_denied_by_the_rule_table(stack: SimpleNamespace):
    ctx = _context(stack, ENGINEER_2, "INTERNAL")  # cleared CONFIDENTIAL, working an INTERNAL task
    survey = str(stack.db.scalar("SELECT id::text FROM documents WHERE title LIKE 'UT%%'"))
    result = stack.chokepoint.invoke(ctx, "docs.read", {"document_id": survey})
    assert result.status == "denied"
    assert result.decision["rule_id"] == "deny-above-clearance"
    assert _audit_events(stack, ctx.task_id)[-1] == "policy.denial"
    confidential = _context(stack, ENGINEER_2, "CONFIDENTIAL")
    allowed = stack.chokepoint.invoke(confidential, "docs.read", {"document_id": survey, "page": 1})
    assert allowed.ok and allowed.output["blocks"]


@requires_pgvector
@pytest.mark.integration
def test_a_department_outside_the_acl_is_denied(stack: SimpleNamespace):
    ctx = _context(stack, ENGINEER_2, "CONFIDENTIAL")
    pump = str(stack.db.scalar("SELECT id::text FROM documents WHERE title LIKE '%%P-310%%'"))
    result = stack.chokepoint.invoke(ctx, "docs.read", {"document_id": pump})
    assert result.status == "denied" and result.decision["rule_id"] == "deny-acl-disjoint"


@requires_pgvector
@pytest.mark.integration
def test_a_replayed_receipt_is_refused_by_the_boundary(stack: SimpleNamespace):
    """The boundary keeps its own nonce set: the second spend of one receipt fails and
    the denial is decided by deny-missing-receipt, from the rule table."""
    ctx = _context(stack, ENGINEER_1)
    captured: dict[str, Any] = {}
    original = stack.chokepoint._plugins["docs.search"]

    def replaying(ctx_: ToolContext, args: dict[str, Any], invocation: Invocation) -> ToolOutput:
        captured["receipt"] = invocation.receipt
        output: ToolOutput = original.run(ctx_, args, invocation)
        ctx_.boundary.verify(invocation, original.resource(ctx_, args))  # spend it again
        return output

    stack.chokepoint._plugins["docs.search"] = ToolPlugin("docs.search", original.resource, replaying)
    try:
        result = stack.chokepoint.invoke(ctx, "docs.search", {"query": "flange leak"})
    finally:
        stack.chokepoint._plugins["docs.search"] = original
    assert result.status == "denied"
    assert result.decision["rule_id"] == "deny-missing-receipt"
    assert "already been used" in result.decision["receipt_error"]
    assert "receipt.rejected" in _audit_events(stack, ctx.task_id)


@requires_pgvector
@pytest.mark.integration
def test_a_tool_whose_boundary_never_verifies_has_its_result_discarded(stack: SimpleNamespace):
    ctx = _context(stack, ENGINEER_1)
    original = stack.chokepoint._plugins["docs.search"]
    stack.chokepoint._plugins["docs.search"] = ToolPlugin(
        "docs.search", original.resource, lambda c, a, i: ToolOutput({"passages": []}, "skipped the boundary")
    )
    try:
        result = stack.chokepoint.invoke(ctx, "docs.search", {"query": "anything"})
    finally:
        stack.chokepoint._plugins["docs.search"] = original
    assert result.status == "error" and "never verified" in (result.error or "")


@requires_pgvector
@pytest.mark.integration
def test_the_approver_is_not_offered_what_it_may_not_do(stack: SimpleNamespace):
    offered = {t["name"]: t for t in stack.chokepoint.available(_context(stack, APPROVER, "CONFIDENTIAL"))}
    assert offered["docs.search"]["available"]
    assert not offered["doc.generate"]["available"] and not offered["code.run"]["available"]
    engineer = {t["name"]: t for t in stack.chokepoint.available(_context(stack, ENGINEER_2, "CONFIDENTIAL"))}
    assert engineer["calc.evaluate"]["available"] and engineer["doc.generate"]["available"]
    assert not engineer["code.run"]["available"]  # sandbox ceiling is INTERNAL
    assert "deny-tool-ceiling" in engineer["code.run"]["why_not"]


@requires_pgvector
@pytest.mark.integration
def test_invalid_arguments_are_rejected_with_a_fixable_message(stack: SimpleNamespace):
    ctx = _context(stack, ENGINEER_1)
    result = stack.chokepoint.invoke(ctx, "docs.search", {"query": "", "surprise": 1})
    assert result.status == "invalid" and "surprise" in (result.error or "")
    coerced = stack.chokepoint.invoke(ctx, "docs.search", {"query": "E-101", "top_k": "3"})
    assert coerced.ok and len(coerced.output["passages"]) <= 3


@requires_pgvector
@pytest.mark.integration
def test_calc_registers_citable_working(stack: SimpleNamespace):
    ctx = _context(stack, ENGINEER_1)
    result = stack.chokepoint.invoke(ctx, "calc.evaluate", {"expression": "(9.2 - 8.4) / 0.25"})
    assert result.ok and result.output["result"] == "3.2" and result.output["evidence_id"] == "C1"
    row = stack.db.query_one(
        "SELECT kind, text, detail FROM task_evidence WHERE task_id = %(t)s::uuid AND evidence_id = 'C1'",
        {"t": ctx.task_id},
    )
    assert row["kind"] == "computation" and row["detail"]["working"] == ["9.2 - 8.4 = 0.8", "0.8 / 0.25 = 3.2"]


@requires_pgvector
@pytest.mark.integration
def test_the_workspace_refuses_escapes(stack: SimpleNamespace):
    ctx = _context(stack, ENGINEER_1)
    wrote = stack.chokepoint.invoke(ctx, "fs.write", {"path": "notes/a.txt", "content": "hello"})
    assert wrote.ok
    read = stack.chokepoint.invoke(ctx, "fs.read", {"path": "notes/a.txt"})
    assert read.output["content"] == "hello"
    escape = stack.chokepoint.invoke(ctx, "fs.read", {"path": "../../keys/receipt.key"})
    assert escape.status == "error" and "outside the workspace" in (escape.error or "")
    sheet = stack.chokepoint.invoke(ctx, "sheet.write", {"path": "t.xlsx", "cells": {"A1": "Tag", "B1": "E-101", "A2": 9.2}})
    assert sheet.ok
    back = stack.chokepoint.invoke(ctx, "sheet.read", {"path": "t.xlsx"})
    assert back.output["rows"][0] == ["Tag", "E-101"] and back.output["rows"][1][0] == 9.2


@requires_pgvector
@pytest.mark.integration
def test_code_runs_only_in_the_sandbox_on_its_own_receipt(stack: SimpleNamespace):
    ctx = _context(stack, ENGINEER_1)
    stack.chokepoint.invoke(ctx, "fs.write", {"path": "readings.csv", "content": "cml,mm\nCML-1,10.1\nCML-3,9.2\n"})
    source = (
        "import csv\n"
        "rows = list(csv.DictReader(open('readings.csv')))\n"
        "low = min(float(r['mm']) for r in rows)\n"
        "open('summary.txt', 'w').write(f'min {low}')\n"
        "print(round((low - 8.4) / 0.25, 2))\n"
    )
    result = stack.chokepoint.invoke(ctx, "code.run", {"source": source})
    assert result.ok, result.to_dict()
    assert result.output["exit_code"] == 0 and result.output["stdout"].strip() == "3.2"
    assert result.output["files_written"] == ["summary.txt"]
    assert result.receipt["verified_by"] == "sandbox (process)"
    assert result.evidence == ["C1"] and len(result.artifacts) == 1
    assert (ctx.workspace() / "summary.txt").read_text() == "min 9.2"

    hostile = stack.chokepoint.invoke(ctx, "code.run", {"source": "import socket\nsocket.create_connection(('1.1.1.1', 443))"})
    assert hostile.ok and hostile.output["exit_code"] != 0 and "blocked in the Citadel sandbox" in hostile.output["stderr"]


def test_the_sandbox_refuses_source_that_differs_from_what_was_authorised(tmp_path: Path):
    """The receipt binds the exact source; one changed byte and nothing runs."""
    from datetime import datetime, timedelta, timezone

    from citadel_contracts.receipts import DecisionReceipt, new_decision_id, new_nonce, resource_digest, sign_receipt
    from citadel_tools import code_resource

    init_keys(tmp_path)
    env = {"CITADEL_KEYS_DIR": str(tmp_path)}
    signing, public = load_receipt_signing_key(env), load_receipt_public_key(env)
    assert signing is not None
    approved = "print('approved')"
    now = datetime.now(timezone.utc)
    token = sign_receipt(
        DecisionReceipt(new_decision_id(), "task", "agent", "code.run",
                        resource_digest(code_resource(approved, "INTERNAL", ["process-engineering"])),
                        {}, "allow-execute-engineer", now, now + timedelta(seconds=30), new_nonce()),
        signing,
    )
    runner = LocalSandboxRunner(public)
    request = {"source": "print('something else')", "receipt": token,
               "resource": {"classification": "INTERNAL", "acl": ["process-engineering"]}}
    refused = runner.run(request)
    assert refused["receipt_verified"] is False and "does not match" in refused["receipt_error"]
    ran = runner.run({**request, "source": approved})
    assert ran["receipt_verified"] and ran["stdout"].strip() == "approved"
    again = runner.run({**request, "source": approved})
    assert again["receipt_verified"] is False  # single use


@requires_pgvector
@pytest.mark.integration
def test_doc_generate_files_a_verified_deliverable_from_task_evidence(stack: SimpleNamespace):
    ctx = _context(stack, ENGINEER_1)
    found = stack.chokepoint.invoke(ctx, "docs.search", {"query": "E-101 CML-3 minimum thickness remaining life"})
    ev = next(p for p in found.output["passages"] if "9.2" in p["text"])["evidence_id"]
    calc = stack.chokepoint.invoke(ctx, "calc.evaluate", {"expression": "(9.2 - 8.4) / 0.25"})
    content = {
        "subject": "Continued service of E-101",
        "background": f"E-101 was inspected under IR-2026-0147 [{ev}].",
        "findings": [f"CML-3 measured a minimum of 9.2 mm against a t-min of 8.4 mm [{ev}]"],
        "analysis": f"At 0.25 mm/year the remaining life is 3.2 years [{ev}][{calc.output['evidence_id']}].",
        "recommendation": "Continue service; re-inspect CML-3 within 12 months.",
    }
    result = stack.chokepoint.invoke(ctx, "doc.generate", {"template_id": "approval-note", "content": content})
    assert result.ok, result.to_dict()
    assert result.output["artifact_status"] == "VERIFIED", result.output
    assert result.artifacts == [result.output["artifact_id"]]
    missing = stack.chokepoint.invoke(ctx, "doc.generate", {"template_id": "no-such-template", "content": {}})
    assert missing.status == "not_found"


def test_the_process_sandbox_blocks_the_obvious_escapes():
    from citadel_tools.sandbox import execute

    for source, marker in [
        ("import socket\nsocket.socket().connect(('1.1.1.1', 80))", "socket"),
        ("import subprocess\nsubprocess.run(['id'])", "subprocess"),
        ("print(open('/etc/passwd').read())", "reading outside"),
        ("open('/tmp/citadel-escape', 'w').write('x')", "writing outside"),
    ]:
        result = execute(source, timeout_s=20, files={}, kind="process")
        assert result["exit_code"] != 0 and marker in result["stderr"], (source, result["stderr"][-300:])
    timed = execute("while True:\n    pass", timeout_s=2, files={}, kind="process")
    assert timed["timed_out"] and timed["exit_code"] == -9
    echoed = execute("print(open('in.txt').read())", timeout_s=20,
                     files={"in.txt": base64.b64encode(b"data").decode()}, kind="process")
    assert echoed["stdout"].strip() == "data"
