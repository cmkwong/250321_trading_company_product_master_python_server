"""Schema-file parsing: read table definitions from the Node server's schema modules.

``NODE_SERVER_ROOT`` / ``SCHEMA_DIR`` point at the sibling Node repository's
``MVC/models/table_schema`` folder.  ``load_schema_catalog`` parses every
``*.js`` there into a ``{table: {field: {...}}}`` catalog (brace-aware, follows
``references`` foreign keys), cached in-process; ``extract_table_fields`` reads
one table's ``fields`` block for the draft builders.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from apps.core import config

NODE_SERVER_ROOT = Path(
    config.get(
        "MASTER_SERVER_ROOT",
        str(
            Path(config.BASE_DIR).parent
            / "241127_trading_company_product_master_server"
        ),
    )
)

SCHEMA_DIR = NODE_SERVER_ROOT / "MVC" / "models" / "table_schema"

MAX_FILE_CHARS = int(config.get("AI_MAX_FILE_CHARS", "20000"))


def extract_table_fields(table_name: str) -> Optional[Dict[str, Any]]:
    """Extract the ``fields`` block for a table from the Node schema files on disk."""
    target = (table_name or "").strip()
    if not target:
        return None
    for base in ("master", "products"):
        path = SCHEMA_DIR / f"{base}.js"
        if not path.is_file():
            continue
        content = path.read_text(encoding="utf-8", errors="replace")
        fields = _extract_fields_from_js(content, target)
        if fields:
            return fields
    return None


def _extract_fields_from_js(content: str, table_name: str) -> Optional[Dict[str, Any]]:
    marker = f"name: '{table_name}',"
    idx = content.find(marker)
    if idx == -1:
        return None
    start = content.find("fields: {", idx)
    if start == -1:
        return None

    fields: Dict[str, Any] = {}
    current: Optional[Dict[str, Any]] = None
    for line in content[start:].splitlines()[1:]:
        if line.startswith("    }") or line.startswith("    },"):
            break  # end of the fields object
        if line.startswith("      ") and line.rstrip().endswith(": {"):
            raw = line.strip()[:-1].strip()  # "id: {"
            name = raw.rstrip(":").strip()
            current = {}
            fields[name] = current
            continue
        if current is not None and line.startswith("        "):
            kv = line.strip().rstrip(",").split(":", 1)
            if len(kv) == 2:
                current[kv[0].strip()] = kv[1].strip().strip("'")
    return fields or None


_SCHEMA_CATALOG_CACHE: Optional[Dict[str, Dict[str, Any]]] = None


def _brace_body(text: str, open_idx: int):
    """Return the contents of the ``{...}`` opened at ``open_idx`` and the index
    just past its closing brace (or ``("", -1)`` when unmatched)."""
    depth = 0
    i = open_idx
    while i < len(text):
        char = text[i]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[open_idx + 1 : i], i + 1
        i += 1
    return "", -1


def _parse_field_body(body: str) -> Dict[str, Any]:
    """Extract type / nullability / PK / FK from one ``field: { ... }`` body."""
    field: Dict[str, Any] = {}
    match = re.search(r"\btype\s*:\s*'([^']*)'", body)
    field["type"] = match.group(1) if match else ""
    match = re.search(r"\bprimaryKey\s*:\s*(true|false)", body)
    field["primaryKey"] = bool(match and match.group(1) == "true")
    match = re.search(r"\bnotNull\s*:\s*(true|false)", body)
    field["notNull"] = bool(match and match.group(1) == "true")
    ref_m = re.search(r"\breferences\s*:\s*\{", body)
    if ref_m:
        ref_body, _ = _brace_body(body, ref_m.end() - 1)
        if ref_body:
            table_m = re.search(r"\btable\s*:\s*'([^']*)'", ref_body)
            field_m = re.search(r"\bfield\s*:\s*'([^']*)'", ref_body)
            if table_m:
                field["references"] = {
                    "table": table_m.group(1),
                    "field": field_m.group(1) if field_m else "id",
                }
    return field


def _parse_fields(block: str) -> Dict[str, Any]:
    """Parse a ``fields: { ... }`` body into ``{name: {type, ...}}`` (brace-aware)."""
    fields: Dict[str, Any] = {}
    name_re = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*:\s*\{")
    pos = 0
    while True:
        match = name_re.search(block, pos)
        if not match:
            break
        name = match.group(1)
        body, end = _brace_body(block, match.end() - 1)
        pos = match.end() if end == -1 else end
        if name in ("references", "constraints", "default") or end == -1:
            continue
        fields[name] = _parse_field_body(body)
    return fields


def load_schema_catalog(reload: bool = False) -> Dict[str, Dict[str, Any]]:
    """Parse every ``table_schema/*.js`` into ``{table: {field: {...}}}``.

    Brace-aware (independent of indentation), follows ``references: {table,
    field}`` foreign keys and never leaks ``references``/``constraints`` as
    columns.  Cached in-process; pass ``reload=True`` after editing a schema
    file.
    """
    global _SCHEMA_CATALOG_CACHE
    if _SCHEMA_CATALOG_CACHE is not None and not reload:
        return _SCHEMA_CATALOG_CACHE

    catalog: Dict[str, Dict[str, Any]] = {}
    if SCHEMA_DIR.is_dir():
        name_re = re.compile(r"name:\s*'([a-z0-9_]+)'")
        for path in sorted(SCHEMA_DIR.glob("*.js")):
            try:
                content = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for match in name_re.finditer(content):
                table = match.group(1)
                fields_idx = content.find("fields: {", match.end())
                if fields_idx == -1:
                    continue
                block, _ = _brace_body(content, content.index("{", fields_idx))
                if block:
                    catalog[table] = _parse_fields(block)
    _SCHEMA_CATALOG_CACHE = catalog
    return catalog


def schema_table_names() -> List[str]:
    """Return every table name found in the schema files, sorted."""
    return sorted(load_schema_catalog().keys())


def render_schema_catalog(tables: Iterable[str], *, include_descriptions: bool = False) -> str:
    """Render compact ``- table(col TYPE [PK] [-> fk.table.fk])`` lines."""
    catalog = load_schema_catalog()
    lines: List[str] = []
    for name in tables:
        fields = catalog.get(name)
        if not fields:
            lines.append(f"- {name}: (not found in schema)")
            continue
        parts = []
        for field_name, field in fields.items():
            token = field_name
            if field.get("type"):
                token += " " + str(field["type"])
            if field.get("primaryKey"):
                token += " PK"
            if field.get("notNull"):
                token += " NOT NULL"
            ref = field.get("references")
            if ref:
                token += f" -> {ref['table']}.{ref['field']}"
            parts.append(token)
        lines.append(f"- {name}(" + ", ".join(parts) + ")")
    return "\n".join(lines)

