"""ReqIF (OMG Requirements Interchange Format 1.2) import, export and diff.

ReqIF is how requirements move between primes and suppliers and between tools
such as DOORS, Polarion and Jama. Export writes a compact, well-formed ReqIF
document; import is deliberately tolerant of other tools' output:

* attribute definitions are matched by LONG-NAME, accepting the standard
  ``ReqIF.ForeignID`` / ``ReqIF.Name`` / ``ReqIF.Text`` names and common
  variants (``ID``, ``Title``, ``Object Text``...),
* string, XHTML and enumeration values are all read as text,
* hierarchy comes from SPECIFICATION / SPEC-HIERARCHY nesting.

Provenant-specific fields travel as ``Provenant.Method`` and
``Provenant.Condition`` attributes, which other tools simply carry along.
"""

from __future__ import annotations

import datetime as _dt
import re
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .project import Project

NS = "http://www.omg.org/spec/ReqIF/20110401/reqif.xsd"

FIELDS = ("title", "text", "parent", "method", "condition")

_ATTRIBUTE_NAMES = {
    "id": ("ReqIF.ForeignID", "ID", "Identifier", "Object Identifier", "Absolute Number", "Req ID"),
    "title": ("ReqIF.Name", "ReqIF.ChapterName", "Title", "Name", "Object Heading", "Heading"),
    "text": ("ReqIF.Text", "Text", "Object Text", "Description"),
    "method": ("Provenant.Method", "Verification Method", "Method"),
    "condition": ("Provenant.Condition", "Acceptance Criterion", "Condition"),
}


class ReqIFError(Exception):
    pass


@dataclass(frozen=True)
class ReqRecord:
    id: str
    title: str = ""
    text: str = ""
    parent: str | None = None
    method: str | None = None
    condition: str | None = None

    def get(self, name: str) -> Any:
        return getattr(self, name)


def records_from_project(project: Project) -> list[ReqRecord]:
    return [
        ReqRecord(
            id=r.id,
            title=r.title,
            text=r.text,
            parent=r.parent,
            method=r.method,
            condition=r.condition.source if r.condition else None,
        )
        for r in project.tree_order()
    ]


def records_from_yaml(path: Path) -> list[ReqRecord]:
    with path.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    return [
        ReqRecord(
            id=str(e["id"]),
            title=str(e.get("title") or ""),
            text=str(e.get("text") or "").strip(),
            parent=None if e.get("parent") is None else str(e["parent"]),
            method=e.get("method"),
            condition=e.get("condition"),
        )
        for e in data.get("requirements", []) or []
    ]


# --------------------------------------------------------------------------- export


def _q(tag: str) -> str:
    return f"{{{NS}}}{tag}"


def _sub(parent: ET.Element, tag: str, **attrs: str) -> ET.Element:
    return ET.SubElement(parent, _q(tag), {k.replace("_", "-").upper(): v for k, v in attrs.items()})


def _safe_id(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", text)


def export_reqif(records: list[ReqRecord], title: str, created: _dt.datetime | None = None) -> bytes:
    ids = [r.id for r in records]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        raise ReqIFError(f"cannot export: duplicate requirement ids {', '.join(duplicates)}")
    ET.register_namespace("", NS)
    now = (created or _dt.datetime.now(_dt.timezone.utc)).replace(microsecond=0).isoformat()
    root = ET.Element(_q("REQ-IF"))
    header = _sub(_sub(root, "THE-HEADER"), "REQ-IF-HEADER", identifier=f"_{uuid.uuid5(uuid.NAMESPACE_URL, title)}")
    for tag, value in (
        ("CREATION-TIME", now),
        ("REQ-IF-TOOL-ID", "provenant"),
        ("REQ-IF-VERSION", "1.0"),
        ("SOURCE-TOOL-ID", "provenant"),
        ("TITLE", title),
    ):
        _sub(header, tag).text = value

    content = _sub(_sub(root, "CORE-CONTENT"), "REQ-IF-CONTENT")
    datatypes = _sub(content, "DATATYPES")
    _sub(
        datatypes, "DATATYPE-DEFINITION-STRING",
        identifier="DT-String", last_change=now, long_name="String", max_length="65535",
    )  # fmt: skip

    spec_types = _sub(content, "SPEC-TYPES")
    object_type = _sub(
        spec_types, "SPEC-OBJECT-TYPE", identifier="SOT-Requirement", last_change=now, long_name="Requirement"
    )
    attributes = _sub(object_type, "SPEC-ATTRIBUTES")
    definitions = {
        "id": "ReqIF.ForeignID",
        "title": "ReqIF.Name",
        "text": "ReqIF.Text",
        "method": "Provenant.Method",
        "condition": "Provenant.Condition",
    }
    for key, long_name in definitions.items():
        definition = _sub(
            attributes, "ATTRIBUTE-DEFINITION-STRING", identifier=f"AD-{key}", last_change=now, long_name=long_name
        )
        _sub(_sub(definition, "TYPE"), "DATATYPE-DEFINITION-STRING-REF").text = "DT-String"
    _sub(spec_types, "SPECIFICATION-TYPE", identifier="ST-Specification", last_change=now, long_name="Specification")

    objects = _sub(content, "SPEC-OBJECTS")
    for record in records:
        obj = _sub(objects, "SPEC-OBJECT", identifier=f"SO-{_safe_id(record.id)}", last_change=now)
        values = _sub(obj, "VALUES")
        for key in definitions:
            value = record.get(key)
            if value in (None, ""):
                continue
            av = _sub(values, "ATTRIBUTE-VALUE-STRING", the_value=str(value))
            _sub(_sub(av, "DEFINITION"), "ATTRIBUTE-DEFINITION-STRING-REF").text = f"AD-{key}"
        _sub(_sub(obj, "TYPE"), "SPEC-OBJECT-TYPE-REF").text = "SOT-Requirement"

    _sub(content, "SPEC-RELATIONS")
    specifications = _sub(content, "SPECIFICATIONS")
    spec = _sub(specifications, "SPECIFICATION", identifier="SPEC-1", last_change=now, long_name=title)
    _sub(_sub(spec, "TYPE"), "SPECIFICATION-TYPE-REF").text = "ST-Specification"
    known = {r.id for r in records}
    by_parent: dict[str | None, list[ReqRecord]] = {}
    for record in records:
        parent = record.parent if record.parent in known else None
        by_parent.setdefault(parent, []).append(record)

    def add_children(element: ET.Element, parent: str | None) -> None:
        kids = by_parent.get(parent, [])
        if not kids:
            return
        container = _sub(element, "CHILDREN")
        for record in kids:
            node = _sub(container, "SPEC-HIERARCHY", identifier=f"SH-{_safe_id(record.id)}", last_change=now)
            # Schema order within SPEC-HIERARCHY is CHILDREN before OBJECT.
            add_children(node, record.id)
            _sub(_sub(node, "OBJECT"), "SPEC-OBJECT-REF").text = f"SO-{_safe_id(record.id)}"

    add_children(spec, None)
    _sub(content, "SPEC-RELATION-GROUPS")
    ET.indent(root, space="  ")
    data: bytes = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    return data


# --------------------------------------------------------------------------- import


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _children(element: ET.Element, name: str) -> list[ET.Element]:
    return [c for c in element if _local(c.tag) == name]


def _child(element: ET.Element, name: str) -> ET.Element | None:
    found = _children(element, name)
    return found[0] if found else None


def _descendants(element: ET.Element, name: str) -> list[ET.Element]:
    return [e for e in element.iter() if _local(e.tag) == name]


def _ref_text(element: ET.Element | None) -> str | None:
    if element is None:
        return None
    for node in element.iter():
        if _local(node.tag).endswith("-REF") and node.text:
            return node.text.strip()
    return None


def _xhtml_text(element: ET.Element) -> str:
    parts: list[str] = []
    block = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4"}

    def walk(node: ET.Element) -> None:
        if node.text:
            parts.append(node.text)
        for child in node:
            walk(child)
            if _local(child.tag) in block:
                parts.append("\n")
            if child.tail:
                parts.append(child.tail)

    walk(element)
    text = "".join(parts)
    return "\n".join(" ".join(line.split()) for line in text.splitlines() if line.strip())


def import_reqif(path: Path) -> list[ReqRecord]:
    try:
        tree = ET.parse(path)
    except ET.ParseError as exc:
        raise ReqIFError(f"{path.name}: not well-formed XML ({exc})") from None
    root = tree.getroot()
    if _local(root.tag) != "REQ-IF":
        raise ReqIFError(f"{path.name}: root element is <{_local(root.tag)}>, expected <REQ-IF>")

    # Attribute definition identifier -> our field name, by LONG-NAME.
    lookup = {name.casefold(): key for key, names in _ATTRIBUTE_NAMES.items() for name in names}
    definitions: dict[str, str] = {}
    enum_values: dict[str, str] = {}
    for element in root.iter():
        tag = _local(element.tag)
        if tag.startswith("ATTRIBUTE-DEFINITION-"):
            key = lookup.get((element.get("LONG-NAME") or "").casefold())
            if key and element.get("IDENTIFIER"):
                definitions[element.get("IDENTIFIER", "")] = key
        elif tag == "ENUM-VALUE" and element.get("IDENTIFIER"):
            enum_values[element.get("IDENTIFIER", "")] = element.get("LONG-NAME") or ""

    objects: dict[str, dict[str, str]] = {}
    for obj in _descendants(root, "SPEC-OBJECT"):
        identifier = obj.get("IDENTIFIER")
        if not identifier:
            continue
        fields: dict[str, str] = {}
        values = _child(obj, "VALUES")
        for value in list(values) if values is not None else []:
            key = definitions.get(_ref_text(_child(value, "DEFINITION")) or "")
            if key is None:
                continue
            kind = _local(value.tag)
            if kind == "ATTRIBUTE-VALUE-XHTML":
                holder = _child(value, "THE-VALUE")
                text = _xhtml_text(holder) if holder is not None else ""
            elif kind == "ATTRIBUTE-VALUE-ENUMERATION":
                refs = [n.text.strip() for n in value.iter() if _local(n.tag) == "ENUM-VALUE-REF" and n.text]
                text = ", ".join(enum_values.get(r, r) for r in refs)
            else:
                text = value.get("THE-VALUE", "")
            fields[key] = text.strip()
        objects[identifier] = fields

    parents: dict[str, str | None] = {}
    order: list[str] = []

    def walk(node: ET.Element, parent: str | None) -> None:
        container = _child(node, "CHILDREN")
        if container is None:
            return
        for hierarchy in _children(container, "SPEC-HIERARCHY"):
            ref = _ref_text(_child(hierarchy, "OBJECT"))
            if ref and ref in objects and ref not in parents:
                parents[ref] = parent
                order.append(ref)
                walk(hierarchy, ref)
            else:
                walk(hierarchy, parent)

    for spec in _descendants(root, "SPECIFICATION"):
        walk(spec, None)
    order.extend(i for i in objects if i not in parents)  # objects outside any specification

    def readable_id(identifier: str) -> str:
        return objects[identifier].get("id") or identifier

    records: list[ReqRecord] = []
    seen: set[str] = set()
    for identifier in order:
        fields = objects[identifier]
        rid = readable_id(identifier)
        if rid in seen:
            raise ReqIFError(f"{path.name}: requirement id '{rid}' appears more than once")
        seen.add(rid)
        parent = parents.get(identifier)
        method = (fields.get("method") or "").strip().lower() or None
        records.append(
            ReqRecord(
                id=rid,
                title=fields.get("title", ""),
                text=fields.get("text", ""),
                parent=readable_id(parent) if parent else None,
                method=method,
                condition=fields.get("condition") or None,
            )
        )
    return records


def load_records(path: Path) -> list[ReqRecord]:
    """Load requirement records from a .reqif file, a requirements.yaml, or a project directory."""
    if path.is_dir():
        return records_from_project(Project.load(path))
    if path.suffix.lower() in (".reqif", ".xml"):
        return import_reqif(path)
    if path.suffix.lower() in (".yaml", ".yml"):
        return records_from_yaml(path)
    raise ReqIFError(f"don't know how to read {path} (expected .reqif, .yaml or a project directory)")


# --------------------------------------------------------------------------- diff & merge


@dataclass
class FieldChange:
    field: str
    before: str | None
    after: str | None


@dataclass
class RecordDiff:
    added: list[ReqRecord] = field(default_factory=list)
    removed: list[ReqRecord] = field(default_factory=list)
    modified: dict[str, list[FieldChange]] = field(default_factory=dict)

    @property
    def empty(self) -> bool:
        return not (self.added or self.removed or self.modified)


def _norm(value: Any) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split())
    return text or None


def diff_records(before: list[ReqRecord], after: list[ReqRecord]) -> RecordDiff:
    old = {r.id: r for r in before}
    new = {r.id: r for r in after}
    result = RecordDiff(
        added=[r for r in after if r.id not in old],
        removed=[r for r in before if r.id not in new],
    )
    for rid, record in new.items():
        if rid not in old:
            continue
        changes = [
            FieldChange(name, old[rid].get(name), record.get(name))
            for name in FIELDS
            if _norm(old[rid].get(name)) != _norm(record.get(name))
        ]
        if changes:
            result.modified[rid] = changes
    return result


def merge_into_yaml(yaml_path: Path, incoming: list[ReqRecord], prune: bool = False) -> RecordDiff:
    """Update requirements.yaml from incoming records, keeping local-only fields.

    Incoming records win for fields they carry. A field the incoming side leaves
    empty (e.g. a supplier tool that does not know ``Provenant.Condition``) keeps
    its local value. Requirements missing from the incoming set are kept unless
    ``prune`` is set.
    """
    current = records_from_yaml(yaml_path) if yaml_path.is_file() else []
    by_id = {r.id: r for r in current}
    merged: list[ReqRecord] = []
    incoming_ids = {r.id for r in incoming}
    for record in current:
        if record.id not in incoming_ids and not prune:
            merged.append(record)
    for record in incoming:
        local = by_id.get(record.id)
        if local is not None:
            record = ReqRecord(
                id=record.id,
                title=record.title or local.title,
                text=record.text or local.text,
                parent=record.parent if record.parent is not None else local.parent,
                method=record.method or local.method,
                condition=record.condition or local.condition,
            )
        merged.append(record)
    order = {r.id: i for i, r in enumerate(current)}
    merged.sort(key=lambda r: order.get(r.id, len(order)))
    changes = diff_records(current, merged)

    entries = []
    for r in merged:
        entry: dict[str, Any] = {"id": r.id}
        if r.parent:
            entry["parent"] = r.parent
        entry["title"] = r.title or r.id
        if r.text:
            entry["text"] = r.text
        if r.method:
            entry["method"] = r.method
        if r.condition:
            entry["condition"] = r.condition
        entries.append(entry)
    text = yaml.safe_dump({"requirements": entries}, sort_keys=False, allow_unicode=True, width=100)
    yaml_path.write_text(text, encoding="utf-8")
    return changes
