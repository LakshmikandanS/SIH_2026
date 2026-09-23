"""The content a deliverable is filled with, normalised against the template's declaration.

A model supplies content as loose JSON -- a finding may be a bare string or an object
with citations, a list may arrive as a single string. This module reduces it to one
canonical shape per declared section type (`text`, `rich_text`, `list`, `table`,
`date`, `value`) and records every mismatch as a schema issue rather than guessing.
Those issues are verification tier 2; nothing here decides whether they are fatal.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Mapping, Optional, Sequence

from citadel_platform.registry.schema import TemplateEntry, TemplateSection

_CITATION = re.compile(r"\[(?:(?:E|C)\d+)(?:\s*,\s*(?:E|C)\d+)*\]")
_CITE_ID = re.compile(r"\b([EC]\d+)\b")


@dataclass
class Item:
    text: str
    citations: list[str] = field(default_factory=list)
    cells: dict[str, str] = field(default_factory=dict)


@dataclass
class Section:
    key: str
    type: str
    items: list[Item] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not any(i.text.strip() or any(v.strip() for v in i.cells.values()) for i in self.items)

    @property
    def citations(self) -> list[str]:
        return [c for i in self.items for c in i.citations]


@dataclass
class Content:
    sections: dict[str, Section]
    issues: list[str]

    def all_citations(self) -> list[str]:
        ordered: list[str] = []
        for section in self.sections.values():
            for cite in section.citations:
                if cite not in ordered:
                    ordered.append(cite)
        return ordered


def _citations_from(value: Any) -> list[str]:
    if isinstance(value, str):
        return [c.upper() for c in _CITE_ID.findall(value.upper())]
    if isinstance(value, (list, tuple)):
        out: list[str] = []
        for entry in value:
            out.extend(_citations_from(entry))
        return out
    return []


def _split_inline_citations(text: str) -> tuple[str, list[str]]:
    """`"CML-3 reads 9.2 mm [E2]"` -> text without the marker, plus `["E2"]`."""
    cites: list[str] = []
    for marker in _CITATION.findall(text):
        cites.extend(_CITE_ID.findall(marker))
    cleaned = _CITATION.sub("", text)
    cleaned = re.sub(r"[ \t]+([.,;:])", r"\1", cleaned)  # "leak [E1]." -> "leak."
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    return cleaned.strip(), cites


def _item(value: Any, issues: list[str], where: str) -> Optional[Item]:
    if isinstance(value, str):
        text, cites = _split_inline_citations(value)
        return Item(text=text, citations=cites)
    if isinstance(value, Mapping):
        raw_text = value["text"] if "text" in value else value["value"] if "value" in value else ""
        text, cites = _split_inline_citations(str(raw_text))
        for key in ("citations", "citation", "sources", "refs"):
            if key in value:
                cites.extend(_citations_from(value[key]))
        cells = {str(k): str(v) for k, v in value.items() if k not in ("text", "citations", "citation", "sources", "refs")}
        return Item(text=text, citations=list(dict.fromkeys(cites)), cells=cells)
    if isinstance(value, (int, float)):
        return Item(text=str(value))
    issues.append(f"{where}: expected text or an object with 'text', got {type(value).__name__}")
    return None


def _section(spec: TemplateSection, raw: Any, issues: list[str]) -> Section:
    section = Section(key=spec.key, type=spec.type)
    where = f"section '{spec.key}'"
    if spec.type in ("list", "table"):
        values: Sequence[Any]
        if isinstance(raw, (list, tuple)):
            values = raw
        elif raw is None:
            values = []
        else:
            issues.append(f"{where}: expected a list, got {type(raw).__name__}; treated as one item")
            values = [raw]
        for index, value in enumerate(values):
            item = _item(value, issues, f"{where} item {index + 1}")
            if item is not None:
                section.items.append(item)
        if spec.min_items is not None and len(section.items) < spec.min_items:
            issues.append(f"{where}: needs at least {spec.min_items} item(s), has {len(section.items)}")
        return section
    if isinstance(raw, (list, tuple)):
        issues.append(f"{where}: expected a single value, got a list; joined")
        raw = {"text": "\n".join(str(v if not isinstance(v, Mapping) else v.get("text", "")) for v in raw),
               "citations": _citations_from([v.get("citations") for v in raw if isinstance(v, Mapping)])}
    item = _item(raw, issues, where) if raw is not None else None
    if item is not None:
        section.items.append(item)
    if spec.type == "date" and item is not None and item.text:
        try:
            date.fromisoformat(item.text[:10])
        except ValueError:
            issues.append(f"{where}: {item.text!r} is not an ISO date (YYYY-MM-DD)")
    if spec.type == "value" and item is not None and item.text:
        if not re.search(r"\d", item.text):
            issues.append(f"{where}: {item.text!r} carries no numeric value")
    return section


def normalise(template: TemplateEntry, raw: Mapping[str, Any]) -> Content:
    issues: list[str] = []
    declared = {s.key for s in template.sections}
    for key in raw:
        if key not in declared:
            issues.append(f"'{key}' is not a section of template {template.id}; ignored")
    sections = {spec.key: _section(spec, raw[spec.key] if spec.key in raw else None, issues) for spec in template.sections}
    return Content(sections=sections, issues=issues)


__all__ = ["Item", "Section", "Content", "normalise"]
