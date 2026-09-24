"""Filling the fixed templates -- never building layout.

The generator only ever replaces placeholders in the committed template files
(registry/templates/, built by ops/templates/make_templates.py): a block placeholder
(`{{key}}` alone in a paragraph or cell) becomes the section's paragraphs, list items or
table rows; an inline field is substituted where it stands; a table row holding
`{{key.N}}` cells is repeated once per entry. Anything a template asks for that the
content does not supply is rendered as an em dash -- and verification tier 1 still sees
the gap, because it reads the content, not this output.

Table rows are arranged by the template's own column headings: a column headed
"Location" takes the item's `location` cell, a column headed "Ref" or "Source ref"
takes the citation markers. So a real organisational template with different columns
needs no code change, only matching keys in the content.

Citations become bracketed numbers in order of first use ([1], [2], ...), and the
sources table lists each one with the exact document version, page and region.
"""

from __future__ import annotations

import html
import io
import re
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence, Union

from docx import Document
from docx.document import Document as DocumentObject
from docx.shared import Pt, RGBColor
from docx.table import Table
from docx.text.paragraph import Paragraph
from lxml import etree

from citadel_deliverables.content import Content, Item, Section

_FIELD = re.compile(r"\{\{([a-z_]+)\}\}")
_ROW_FIELD = re.compile(r"\{\{([a-z_]+)\.(\d+)\}\}")
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_REF_COLUMNS = {"ref", "refs", "sourceref", "sourcerefs", "citation", "citations", "source", "sources"}
EM_DASH = "—"

RowEntry = Union[Sequence[str], Item]


@dataclass(frozen=True)
class SourceRef:
    number: int
    evidence_id: str
    kind: str  # document | computation | unresolved
    title: str
    version: Optional[int]
    page: Optional[int]
    bbox: Optional[Sequence[float]]

    def region(self) -> str:
        if not self.bbox:
            return "whole page" if self.kind == "document" else "computed value" if self.kind == "computation" else EM_DASH
        return "(" + ", ".join(f"{v:.2f}" for v in self.bbox) + ")"

    def row(self) -> list[str]:
        return [
            f"[{self.number}]",
            self.title,
            str(self.version) if self.version is not None else EM_DASH,
            str(self.page) if self.page is not None else EM_DASH,
            self.region(),
        ]

    def to_dict(self) -> dict[str, Any]:
        return {
            "number": self.number, "evidence_id": self.evidence_id, "kind": self.kind, "title": self.title,
            "version": self.version, "page": self.page, "bbox": list(self.bbox) if self.bbox else None,
        }


def citation_numbers(content: Content) -> dict[str, int]:
    return {cite: index for index, cite in enumerate(content.all_citations(), start=1)}


def markers(item: Item, numbers: Mapping[str, int]) -> str:
    refs = sorted({numbers[c] for c in item.citations if c in numbers})
    return "".join(f"[{n}]" for n in refs)


def _suffix(item: Item, numbers: Mapping[str, int]) -> str:
    text = markers(item, numbers)
    return f" {text}" if text else ""


def _cited(text: str, item: Item, numbers: Mapping[str, int]) -> str:
    """The text with its reference numbers, placed before a closing full stop the way a
    report is typeset: "... 3.2 years [1][2]." rather than "... 3.2 years. [1][2]"."""
    suffix = _suffix(item, numbers)
    if suffix and text.rstrip().endswith((".", ";")):
        stripped = text.rstrip()
        return f"{stripped[:-1]}{suffix}{stripped[-1]}"
    return f"{text}{suffix}"


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def arrange_cells(headers: Sequence[str], item: Item, numbers: Mapping[str, int]) -> list[str]:
    """One table row in the template's column order. A column takes the cell whose key
    names it; a reference column takes the citation markers; any other column takes the
    next unclaimed value in the order the content gave them. Nothing is dropped: values
    left over after every column is filled join the last content column."""
    claimed: dict[int, str] = {}
    used: set[str] = set()
    for index, header in enumerate(headers):
        key = _norm(header)
        if key in _REF_COLUMNS:
            claimed[index] = markers(item, numbers)
            continue
        for cell_key, value in item.cells.items():
            if cell_key in used:
                continue
            candidate = _norm(cell_key)
            if candidate and (candidate == key or key.startswith(candidate) or candidate.startswith(key)):
                claimed[index] = value
                used.add(cell_key)
                break
    leftovers = [v for k, v in item.cells.items() if k not in used]
    if item.text.strip() and item.text not in item.cells.values():
        leftovers.insert(0, item.text)
    row: list[str] = []
    content_columns: list[int] = []
    for index, header in enumerate(headers):
        if index in claimed:
            row.append(claimed[index])
        elif leftovers:
            row.append(leftovers.pop(0))
        else:
            row.append("")
        if _norm(header) not in _REF_COLUMNS:
            content_columns.append(index)
    if leftovers and content_columns:
        last = content_columns[-1]
        row[last] = "; ".join(v for v in [row[last], *leftovers] if v)
    return row


# -- docx ---------------------------------------------------------------------------------


def _set_text(paragraph: Paragraph, text: str, *, italic: bool = False, muted: bool = False) -> None:
    for run in list(paragraph.runs)[1:]:
        run._r.getparent().remove(run._r)
    run = paragraph.runs[0] if paragraph.runs else paragraph.add_run()
    run.text = text
    run.italic = italic or None
    if muted:
        run.font.color.rgb = RGBColor(0x66, 0x70, 0x7A)


def _insert_after(paragraph: Paragraph, text: str) -> Paragraph:
    new_p = deepcopy(paragraph._p)
    paragraph._p.addnext(new_p)
    added = Paragraph(new_p, paragraph._parent)
    _set_text(added, text)
    return added


def _section_lines(section: Section, numbers: Mapping[str, int]) -> list[str]:
    if section.type == "list":
        return [f"•  {_cited(item.text, item, numbers)}" for item in section.items]
    if section.type == "rich_text":
        lines: list[str] = []
        for item in section.items:
            parts = [p.strip() for p in re.split(r"\n+", item.text) if p.strip()] or [item.text]
            parts[-1] = _cited(parts[-1], item, numbers)
            lines.extend(parts)
        return lines
    if section.type == "table":
        return [_cited("  |  ".join(v for v in [*item.cells.values(), item.text] if v), item, numbers) for item in section.items]
    return [_cited(item.text, item, numbers) for item in section.items]


_BULLET = re.compile(r"^\s*[-*\u2022]\s+")
_HEADING_COLOUR = RGBColor(0x1D, 0x2B, 0x3A)


def _paragraph_after(anchor: Paragraph, model: Any, text: str, *, heading: bool) -> Paragraph:
    """A new paragraph after `anchor`, cloned from the placeholder's own formatting (so a
    template's body style carries), set as a sub-heading or as body text."""
    new_p = deepcopy(model)
    anchor._p.addnext(new_p)
    added = Paragraph(new_p, anchor._parent)
    _style_line(added, text, heading=heading)
    return added


def _style_line(paragraph: Paragraph, text: str, *, heading: bool) -> None:
    _set_text(paragraph, text)
    run = paragraph.runs[0]
    run.bold = True if heading else None
    if heading:
        run.font.size = Pt(11.5)
        run.font.color.rgb = _HEADING_COLOUR
        paragraph.paragraph_format.space_before = Pt(8)
        paragraph.paragraph_format.keep_with_next = True


def _render_subsections(paragraph: Paragraph, section: Section, numbers: Mapping[str, int]) -> None:
    """A report body: each section's heading, then its paragraphs, each paragraph with
    the reference numbers of what it cites."""
    model = deepcopy(paragraph._p)
    lines: list[tuple[str, bool]] = []
    for item in section.items:
        lines.append((item.heading or EM_DASH, True))
        for part in item.parts or [item]:
            text = part.text
            if _BULLET.match(text):
                text = "\u2022  " + _BULLET.sub("", text, count=1)
            lines.append((_cited(text, part, numbers), False))
    first, *rest = lines
    _style_line(paragraph, first[0], heading=first[1])
    anchor = paragraph
    for text, heading in rest:
        anchor = _paragraph_after(anchor, model, text, heading=heading)


def _is_heading(paragraph: Paragraph) -> bool:
    """A template's section heading: a short line whose every run is bold (how the
    constructed templates set them, and how the preview recognises them)."""
    runs = [r for r in paragraph.runs if r.text.strip()]
    return bool(runs) and all(r.bold for r in runs) and len(paragraph.text.strip()) < 90


def _drop(paragraph: Paragraph) -> None:
    element = paragraph._p
    parent = element.getparent()
    if parent is not None:
        parent.remove(element)


def _render_section(paragraph: Paragraph, section: Section, numbers: Mapping[str, int]) -> None:
    if section.empty:
        if section.omit_when_empty:
            previous = paragraph._p.getprevious()
            if previous is not None and etree.QName(previous).localname == "p":
                heading = Paragraph(previous, paragraph._parent)
                if _is_heading(heading):
                    _drop(heading)
            _drop(paragraph)
            return
        _set_text(paragraph, "Not applicable.", italic=True, muted=True)
        return
    if section.type == "sections":
        _render_subsections(paragraph, section, numbers)
        return
    lines = _section_lines(section, numbers)
    _set_text(paragraph, lines[0])
    anchor = paragraph
    for line in lines[1:]:
        anchor = _insert_after(anchor, line)


def _substitute(text: str, fields: Mapping[str, str]) -> str:
    return _FIELD.sub(lambda m: fields.get(m.group(1), EM_DASH), text)


def _replace_fields(paragraphs: Sequence[Paragraph], fields: Mapping[str, str]) -> None:
    for paragraph in paragraphs:
        if "{{" not in paragraph.text:
            continue
        for run in paragraph.runs:
            if "{{" in run.text:
                run.text = _substitute(run.text, fields)
        if _FIELD.search(paragraph.text):  # a placeholder split across runs by an editor
            _set_text(paragraph, _substitute(paragraph.text, fields))


def _all_paragraphs(document: DocumentObject) -> list[Paragraph]:
    paragraphs = list(document.paragraphs)
    for table in document.tables:
        for row in table.rows:
            for cell in row.cells:
                paragraphs.extend(cell.paragraphs)
    for section in document.sections:
        for part in (section.header, section.footer, section.first_page_header, section.first_page_footer):
            paragraphs.extend(part.paragraphs)
            for table in part.tables:
                for row in table.rows:
                    for cell in row.cells:
                        paragraphs.extend(cell.paragraphs)
    return paragraphs


def _row_text(tr: Any) -> str:
    return "".join(t.text or "" for t in tr.iter(f"{_W}t"))


def _cell_texts(tr: Any) -> list[str]:
    return ["".join(t.text or "" for t in tc.iter(f"{_W}t")) for tc in tr.iter(f"{_W}tc")]


def _fill_row_tables(
    document: DocumentObject, rows: Mapping[str, Sequence[RowEntry]], numbers: Mapping[str, int]
) -> None:
    tables: list[Table] = list(document.tables)
    for table in tables:
        trs = list(table._tbl.iter(f"{_W}tr"))
        for position, template_tr in enumerate(trs):
            match = _ROW_FIELD.search(_row_text(template_tr))
            if not match:
                continue
            key = match.group(1)
            headers = _cell_texts(trs[position - 1]) if position > 0 else []
            entries = rows.get(key) or []
            materialised: list[Sequence[str]] = [
                arrange_cells(headers, entry, numbers) if isinstance(entry, Item) else entry for entry in entries
            ] or [[EM_DASH]]
            for entry in materialised:
                new_tr = deepcopy(template_tr)
                for node in new_tr.iter(f"{_W}t"):
                    node.text = _ROW_FIELD.sub(
                        lambda m: entry[int(m.group(2))] if int(m.group(2)) < len(entry) else "",
                        node.text or "",
                    )
                template_tr.addprevious(new_tr)
            parent = template_tr.getparent()
            if parent is not None:
                parent.remove(template_tr)


def render_docx(
    template_path: Path,
    content: Content,
    *,
    fields: Mapping[str, str],
    sources: Sequence[SourceRef],
    revisions: Sequence[Sequence[str]],
) -> bytes:
    document = Document(str(template_path))
    numbers = {s.evidence_id: s.number for s in sources}
    for paragraph in list(document.paragraphs):
        match = _FIELD.fullmatch(paragraph.text.strip())
        if match and match.group(1) in content.sections and content.sections[match.group(1)].type != "table":
            _render_section(paragraph, content.sections[match.group(1)], numbers)
    rows: dict[str, Sequence[RowEntry]] = {
        "sources": [s.row() for s in sources],
        "revision_history": [list(r) for r in revisions],
    }
    for key, section in content.sections.items():
        if section.type == "table":
            rows[key] = list(section.items)
    _fill_row_tables(document, rows, numbers)
    # A table section whose template gives it a block placeholder rather than a row table.
    for paragraph in list(document.paragraphs):
        match = _FIELD.fullmatch(paragraph.text.strip())
        if match and match.group(1) in content.sections:
            _render_section(paragraph, content.sections[match.group(1)], numbers)
    _replace_fields(_all_paragraphs(document), fields)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def docx_text(data: bytes) -> str:
    """Everything a reader would see, for verification and the preview."""
    document = Document(io.BytesIO(data))
    return "\n".join(p.text for p in _all_paragraphs(document))


def docx_to_html(data: bytes) -> str:
    """A faithful-enough HTML rendering for the in-browser preview: header, headings,
    paragraphs and tables in document order, footer. Every string is escaped."""
    document = Document(io.BytesIO(data))
    parts: list[str] = []
    for child in document.element.body.iterchildren():
        tag = etree.QName(child).localname
        if tag == "p":
            paragraph = Paragraph(child, document)
            text = paragraph.text.strip()
            if not text:
                continue
            runs = [r for r in paragraph.runs if r.text.strip()]
            bold = bool(runs) and all(r.bold for r in runs)
            if bold and len(text) < 90:
                parts.append(f"<h3>{html.escape(text)}</h3>")
            else:
                parts.append(f"<p>{html.escape(text)}</p>")
        elif tag == "tbl":
            body = []
            for tr in child.iter(f"{_W}tr"):
                body.append("<tr>" + "".join(f"<td>{html.escape(c)}</td>" for c in _cell_texts(tr)) + "</tr>")
            parts.append("<table>" + "".join(body) + "</table>")
    header = " ".join(p.text for p in document.sections[0].header.paragraphs).strip()
    footer = " ".join(p.text for p in document.sections[0].footer.paragraphs).strip()
    return (
        f"<div class='doc-header'>{html.escape(header)}</div>"
        + "".join(parts)
        + f"<div class='doc-footer'>{html.escape(footer)}</div>"
    )


# -- xlsx ---------------------------------------------------------------------------------


def _header_footer_parts(sheet: Any) -> list[Any]:
    parts = []
    for hf in (sheet.oddHeader, sheet.oddFooter, sheet.evenHeader, sheet.evenFooter, sheet.firstHeader, sheet.firstFooter):
        parts.extend([hf.left, hf.center, hf.right])
    return parts


def _numeric(value: str) -> Any:
    return float(value) if re.fullmatch(r"-?\d+(\.\d+)?", value.strip()) else value


def render_xlsx(
    template_path: Path,
    content: Content,
    *,
    fields: Mapping[str, str],
    sources: Sequence[SourceRef],
) -> bytes:
    from openpyxl import load_workbook

    workbook = load_workbook(str(template_path))
    sheet = workbook.active
    assert sheet is not None
    numbers = {s.evidence_id: s.number for s in sources}
    anchors: dict[str, tuple[int, int]] = {}
    for row in sheet.iter_rows():
        for cell in row:
            if not (isinstance(cell.value, str) and "{{" in cell.value):
                continue
            block = _FIELD.fullmatch(cell.value.strip())
            key = block.group(1) if block else ""
            if key == "sources" or (key in content.sections and content.sections[key].type in ("table", "list")):
                anchors[key] = (cell.row, cell.column)
                cell.value = None
            elif key in content.sections:
                section = content.sections[key]
                text = "; ".join(f"{i.text}{_suffix(i, numbers)}" for i in section.items)
                cell.value = _numeric(text) if text else EM_DASH
            else:
                cell.value = _substitute(cell.value, fields)
    for key, (row_index, column) in anchors.items():
        headers = [
            str(sheet.cell(row=row_index - 1, column=c).value or "")
            for c in range(column, column + 8)
        ]
        while headers and not headers[-1]:
            headers.pop()
        if key == "sources":
            values_list: list[list[str]] = [s.row() for s in sources] or [[EM_DASH]]
        else:
            section = content.sections[key]
            if section.type == "table":
                values_list = [arrange_cells(headers or ["Value", "Ref"], item, numbers) for item in section.items]
            else:
                values_list = [[f"{item.text}{_suffix(item, numbers)}"] for item in section.items]
            values_list = values_list or [[EM_DASH]]
        for offset, values in enumerate(values_list):
            for col_offset, value in enumerate(values):
                sheet.cell(row=row_index + offset, column=column + col_offset, value=_numeric(value))
    for part in _header_footer_parts(sheet):
        if part.text and "{{" in part.text:
            part.text = _substitute(part.text, fields)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def xlsx_text(data: bytes) -> str:
    from openpyxl import load_workbook

    sheet = load_workbook(io.BytesIO(data)).active
    assert sheet is not None
    cells = [str(c.value) for row in sheet.iter_rows() for c in row if c.value is not None]
    printed = [p.text for p in _header_footer_parts(sheet) if p.text]
    return "\n".join(cells + printed)


def xlsx_to_html(data: bytes) -> str:
    from openpyxl import load_workbook

    sheet = load_workbook(io.BytesIO(data)).active
    assert sheet is not None
    rows = []
    for row in sheet.iter_rows():
        values = ["" if c.value is None else str(c.value) for c in row]
        if not any(values):
            continue
        while values and not values[-1]:
            values.pop()
        rows.append("<tr>" + "".join(f"<td>{html.escape(v)}</td>" for v in values) + "</tr>")
    header = " ".join(p.text for p in (sheet.oddHeader.left, sheet.oddHeader.center, sheet.oddHeader.right) if p.text)
    footer = " ".join(p.text for p in (sheet.oddFooter.left, sheet.oddFooter.center, sheet.oddFooter.right) if p.text)
    return (
        f"<div class='doc-header'>{html.escape(header)}</div>"
        + "<table class='sheet'>" + "".join(rows) + "</table>"
        + f"<div class='doc-footer'>{html.escape(footer)}</div>"
    )


def rendered_text(fmt: str, data: bytes) -> str:
    return docx_text(data) if fmt == "docx" else xlsx_text(data)


def to_html(fmt: str, data: bytes) -> str:
    return docx_to_html(data) if fmt == "docx" else xlsx_to_html(data)


__all__ = [
    "SourceRef",
    "citation_numbers",
    "markers",
    "arrange_cells",
    "render_docx",
    "render_xlsx",
    "docx_text",
    "xlsx_text",
    "docx_to_html",
    "xlsx_to_html",
    "rendered_text",
    "to_html",
    "EM_DASH",
]
