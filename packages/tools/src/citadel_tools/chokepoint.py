"""The chokepoint: the one function every tool invocation passes through.

    resolve the tool from the registry
      -> validate the arguments against the tool's declared JSON schema
      -> name the concrete resource, from trusted facts
      -> evaluate policy (registry/policy.yaml; first match wins; default deny)
      -> record the decision (allow AND deny are both audit events)
      -> issue a signed receipt, if the tool requires one
      -> dispatch; the executing boundary verifies the receipt before acting

Two checks stay two checks (packages/tools/AGENTS.md): the policy decision here, and
the receipt verification at the boundary that acts. The chokepoint holds the receipt
*private* key and never verifies its own receipts; if the boundary refuses one, the
decision is re-evaluated with `receipt.valid = false`, so the recorded denial comes
from the same rule table as every other decision (`deny-missing-receipt`) rather than
from a special case written here.
"""

from __future__ import annotations

import importlib
import json
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from jsonschema import Draft202012Validator

from citadel_contracts.domain import Resource
from citadel_contracts.receipts import (
    DecisionReceipt,
    new_decision_id,
    new_nonce,
    resource_digest,
    sign_receipt,
)
from citadel_platform.registry import Registry
from citadel_platform.registry.schema import ToolEntry
from citadel_platform.tracing import Tracer

from citadel_tools.context import (
    Invocation,
    ReceiptRejected,
    ResourceNotFound,
    ToolContext,
    ToolFailure,
    ToolResult,
)
from citadel_tools.plugins import ToolPlugin
from citadel_tools.policy import Decision, ReceiptFacts, evaluate

#: Receipts live seconds, single machine, no renewal (citadel_contracts.receipts).
RECEIPT_TTL_S = 30.0


class ChokepointError(RuntimeError):
    """The manifest and the code disagree -- raised at startup, never mid-task."""


def _discover(tool: ToolEntry) -> ToolPlugin:
    try:
        module = importlib.import_module(tool.package)
    except ImportError as exc:
        raise ChokepointError(f"tool {tool.name}: package {tool.package} cannot be imported ({exc})") from exc
    plugins = getattr(module, "PLUGINS", None)
    if not isinstance(plugins, Mapping) or tool.name not in plugins:
        raise ChokepointError(f"tool {tool.name}: {tool.package} does not export a plugin for it in PLUGINS")
    plugin = plugins[tool.name]
    if not isinstance(plugin, ToolPlugin):
        raise ChokepointError(f"tool {tool.name}: PLUGINS entry is not a ToolPlugin")
    return plugin


def _coerce(schema: Mapping[str, Any], arguments: Mapping[str, Any]) -> dict[str, Any]:
    """Apply declared defaults, and forgive the two slips small models make most --
    a number sent as a string, an object or array sent as a JSON string -- before
    strict validation. Anything still wrong after this is rejected, not guessed at."""
    properties = schema.get("properties") or {}
    out = dict(arguments)
    for key, spec in properties.items():
        if key not in out:
            if isinstance(spec, Mapping) and "default" in spec:
                out[key] = spec["default"]
            continue
        value = out[key]
        wanted = spec.get("type") if isinstance(spec, Mapping) else None
        if isinstance(value, str):
            text = value.strip()
            if wanted == "integer" and text.lstrip("-").isdigit():
                out[key] = int(text)
            elif wanted == "number":
                try:
                    out[key] = float(text)
                except ValueError:
                    pass
            elif wanted in ("object", "array") and text[:1] in "{[":
                try:
                    out[key] = json.loads(text)
                except ValueError:
                    pass
        elif wanted == "string" and isinstance(value, (int, float)) and not isinstance(value, bool):
            out[key] = str(value)
    return out


def _resource_dict(resource: Resource) -> dict[str, Any]:
    return {
        "id": resource.resource_id,
        "type": resource.type,
        "classification": resource.classification,
        "acl": list(resource.acl),
        "digest": resource_digest(resource),
    }


class Chokepoint:
    def __init__(
        self,
        registry: Registry,
        *,
        signing_key: Optional[Ed25519PrivateKey],
        receipt_ttl_s: float = RECEIPT_TTL_S,
    ) -> None:
        self.registry = registry
        self._signing_key = signing_key
        self._ttl = receipt_ttl_s
        self._tools: dict[str, ToolEntry] = {t.name: t for t in registry.tools}
        self._plugins: dict[str, ToolPlugin] = {t.name: _discover(t) for t in registry.tools}
        self._validators = {t.name: Draft202012Validator(t.schema_) for t in registry.tools}

    # -- what a planner may offer ------------------------------------------------------

    def available(self, ctx: ToolContext) -> list[dict[str, Any]]:
        """The tools this actor could use in this task, decided by the same rule table
        as every call: each tool is evaluated against a task-scoped resource at the
        task's classification, receipt aside. A tool the policy would refuse outright
        is not offered; resource-specific refusals still happen at the call."""
        offered = []
        probe = Resource.build(f"task:{ctx.task_id}", "task", ctx.task_classification, (ctx.department,))
        for tool in self.registry.tools:
            decision = evaluate(self.registry.policy, actor=ctx.actor, resource=probe, tool=tool, receipt=ReceiptFacts(valid=True))
            offered.append({
                "name": tool.name,
                "available": decision.allowed,
                "why_not": None if decision.allowed else f"{decision.rule_id or 'default deny'}: {decision.reason}",
                "side_effect": tool.side_effect,
                "schema": tool.schema_,
                "description": tool.description,
                "notes": tool.notes,
                "requires_receipt": tool.requires_receipt,
                "classification_ceiling": tool.classification_ceiling,
            })
        return offered

    # -- the one entry point -------------------------------------------------------------

    def invoke(self, ctx: ToolContext, name: str, arguments: Mapping[str, Any]) -> ToolResult:
        started = time.perf_counter()
        tracer = ctx.tracer or Tracer(None)
        with tracer.span(f"tool.{name}", "tool", task_id=ctx.task_id, attributes={"tool": name}) as span:
            result = self._invoke(ctx, name, arguments if isinstance(arguments, Mapping) else {})
            result.duration_ms = int((time.perf_counter() - started) * 1000)
            span.set("status", result.status)
            span.set("rule_id", (result.decision or {}).get("rule_id"))
            span.set("evidence", result.evidence)
            span.set("artifacts", result.artifacts)
            if result.status == "error":
                span.status = "error"
                span.set("error", (result.error or "")[:300])
        return result

    def _invoke(self, ctx: ToolContext, name: str, arguments: Mapping[str, Any]) -> ToolResult:
        tool = self._tools.get(name)
        if tool is None:
            known = ", ".join(sorted(self._tools))
            self._audit(ctx, "policy.denial", {"tool": name, "reason": "unknown_tool"})
            return ToolResult(name, "invalid", f"no tool named {name}", error=f"unknown tool {name!r}; the tools are: {known}")

        args = _coerce(tool.schema_, arguments)
        problems = sorted(self._validators[name].iter_errors(args), key=lambda e: list(e.path))
        if problems:
            message = "; ".join(
                f"{'.'.join(str(p) for p in e.path) or 'arguments'}: {e.message}" for e in problems[:4]
            )
            return ToolResult(name, "invalid", f"{name}: invalid arguments", error=message)

        plugin = self._plugins[name]
        try:
            resource = plugin.resource(ctx, args)
        except ResourceNotFound as exc:
            return ToolResult(name, "not_found", f"{name}: {exc}", error=str(exc))
        except ToolFailure as exc:
            return ToolResult(name, "error", f"{name}: {exc}", error=str(exc))

        decision = evaluate(self.registry.policy, actor=ctx.actor, resource=resource, tool=tool, receipt=ReceiptFacts(valid=True))
        decision_id = new_decision_id()
        described = self._describe(decision, decision_id, resource)
        if not decision.allowed:
            self._audit(ctx, "policy.denial", {"tool": name, **described})
            return ToolResult(name, "denied", f"{name}: denied by {decision.rule_id or 'default deny'} ({decision.reason})",
                              decision=described, error=f"policy denied this call: {decision.reason}")

        invocation = Invocation(tool=tool, resource=resource, decision=decision, decision_id=decision_id)
        receipt_info: Optional[dict[str, Any]] = None
        if tool.requires_receipt:
            if self._signing_key is None:
                return self._receipt_refused(ctx, tool, resource, decision_id, "no receipt signing key is configured")
            invocation.receipt, receipt_info = self._issue(ctx, tool, resource, decision, decision_id)
        self._audit(ctx, "policy.decision", {"tool": name, **described})

        try:
            output = plugin.run(ctx, args, invocation)
        except ReceiptRejected as exc:
            return self._receipt_refused(ctx, tool, resource, decision_id, str(exc))
        except ResourceNotFound as exc:
            return ToolResult(name, "not_found", f"{name}: {exc}", decision=described, receipt=receipt_info, error=str(exc))
        except ToolFailure as exc:
            return ToolResult(name, "error", f"{name}: {exc}", decision=described, receipt=receipt_info, error=str(exc))
        except Exception as exc:  # a tool bug must not take the agent loop down with it
            return ToolResult(name, "error", f"{name}: failed", decision=described, receipt=receipt_info,
                              error=f"{type(exc).__name__}: {str(exc)[:400]}")

        if tool.requires_receipt and not invocation.verified:
            return ToolResult(name, "error", f"{name}: result discarded", decision=described, receipt=receipt_info,
                              error="the executing boundary never verified this call's receipt; the result is discarded")
        if receipt_info is not None:
            receipt_info["verified_by"] = invocation.verified_by
        return ToolResult(
            name, "ok", output.summary, output=output.data, decision=described, receipt=receipt_info,
            evidence=output.evidence, artifacts=output.artifacts, detail=output.detail,
        )

    # -- helpers -------------------------------------------------------------------------

    @staticmethod
    def _describe(decision: Decision, decision_id: str, resource: Resource) -> dict[str, Any]:
        return {
            "effect": decision.effect,
            "rule_id": decision.rule_id,
            "reason": decision.reason,
            "decision_id": decision_id,
            "resource": _resource_dict(resource),
        }

    def _issue(
        self, ctx: ToolContext, tool: ToolEntry, resource: Resource, decision: Decision, decision_id: str
    ) -> tuple[str, dict[str, Any]]:
        assert self._signing_key is not None
        now = datetime.now(timezone.utc)
        receipt = DecisionReceipt(
            decision_id=decision_id,
            task_id=ctx.task_id,
            agent_id=ctx.agent_id,
            operation=tool.name,
            resource_digest=resource_digest(resource),
            scope={"classification_max": ctx.actor.classification_max, "department": ctx.department},
            rule=decision.rule_id or "",
            issued_at=now,
            expires_at=now + timedelta(seconds=self._ttl),
            nonce=new_nonce(),
        )
        token = sign_receipt(receipt, self._signing_key)
        info = {
            "decision_id": decision_id,
            "operation": tool.name,
            "resource_digest": receipt.resource_digest,
            "expires_at": receipt.expires_at.isoformat(),
        }
        self._audit(ctx, "receipt.issued", {"tool": tool.name, **info})
        return token, info

    def _receipt_refused(self, ctx: ToolContext, tool: ToolEntry, resource: Resource, decision_id: str, why: str) -> ToolResult:
        decision = evaluate(self.registry.policy, actor=ctx.actor, resource=resource, tool=tool, receipt=ReceiptFacts(valid=False))
        described = self._describe(decision, decision_id, resource)
        described["receipt_error"] = why
        self._audit(ctx, "receipt.rejected", {"tool": tool.name, "decision_id": decision_id, "error": why[:300]})
        self._audit(ctx, "policy.denial", {"tool": tool.name, **described})
        return ToolResult(tool.name, "denied", f"{tool.name}: receipt refused at the boundary ({why})",
                          decision=described, error=f"the executing boundary refused the receipt: {why}")

    @staticmethod
    def _audit(ctx: ToolContext, event: str, payload: Mapping[str, Any]) -> None:
        if ctx.audit is not None:
            ctx.audit.record(event, actor_id=ctx.user.user_id, payload={"task_id": ctx.task_id, "agent_id": ctx.agent_id, **payload})


__all__ = ["Chokepoint", "ChokepointError", "RECEIPT_TTL_S"]
