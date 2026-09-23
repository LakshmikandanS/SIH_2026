"""Getting a JSON value out of a model reply, honestly.

Structured output is requested with a JSON schema, which Ollama and vLLM enforce with a
grammar -- but a small model can still return nothing, or wrap valid JSON in prose or
a code fence, or (for a reasoning model on an older runtime that ignores `think`)
prefix its answer with a `<think>` block. This module recovers the JSON when it is
there. When it is not, it says so; the gateway then records the attempt as degraded
and tries the next model. It never invents a value (ADR-0001 §Q3, the prototype's
silently-substituted empty responses).
"""

from __future__ import annotations

import json
import re
from typing import Any, Mapping, Optional

import jsonschema

_THINK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


class StructuredOutputError(ValueError):
    """The reply held no JSON value matching the requested schema."""


def strip_reasoning(text: str) -> str:
    return _THINK.sub("", text).strip()


def _balanced_object(text: str) -> Optional[str]:
    start = text.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return text[start : index + 1]
        start = text.find("{", start + 1)
    return None


def parse_json(text: str) -> Any:
    cleaned = strip_reasoning(text)
    if not cleaned:
        raise StructuredOutputError("the model returned an empty response")
    candidates = [cleaned]
    fenced = _FENCE.search(cleaned)
    if fenced:
        candidates.append(fenced.group(1).strip())
    balanced = _balanced_object(cleaned)
    if balanced:
        candidates.append(balanced)
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except ValueError:
            continue
    raise StructuredOutputError(f"no JSON object in the model's reply: {cleaned[:160]!r}")


def parse_structured(text: str, schema: Mapping[str, Any]) -> Any:
    value = parse_json(text)
    try:
        jsonschema.validate(value, dict(schema))
    except jsonschema.ValidationError as exc:
        location = "/".join(str(p) for p in exc.absolute_path) or "<root>"
        raise StructuredOutputError(f"reply does not match the schema at {location}: {exc.message}") from exc
    return value


__all__ = ["StructuredOutputError", "strip_reasoning", "parse_json", "parse_structured"]
