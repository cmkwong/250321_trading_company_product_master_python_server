"""Tools for the product-attribute sub-agent.

This module owns the attribute/category-specific readers and SQL builders
(``fetch_category_tree``, ``fetch_attribute_catalog``,
``fetch_attribute_assignments``, ``build_*_attribute*``) plus the
``suggest_category_attributes`` helper.  The generic read / web tools stay in
:mod:`core.plan_tools` and are re-imported here only to assemble
``ATTRIBUTE_TOOLS``.  Nothing here writes data: every builder returns SQL text.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import tool
from pydantic import BaseModel, Field, field_validator

from apps.core.services.AI.Agents.core import master_api
from apps.core.services.AI.Agents.core import sql as sqlguard
from apps.core.services.AI.Agents.core.contracts import (
    MasterApiError,
    _tool_error,
    extract_json_block,
)
from apps.core.services.AI.Agents.core.llm import (
    DEFAULT_MODEL,
    build_llm,
    message_text,
)
from apps.core.services.AI.Agents.core.master_api import _rows
from apps.core.services.AI.Agents.core.plan_tools import (
    execute_select_query,
    get_master_table_schema,
    list_master_tables,
    read_master_schema_file,
    read_table_definition,
)


class _Reuse(BaseModel):
    name: str
    reason: str = ""


class _New(BaseModel):
    name: str = Field(description="snake_case, e.g. suitable_pet")
    label: str = Field(description="short Title Case label, max 3 words")
    description: str = ""
    multiple_selection: bool = False
    values: List[str] = Field(default_factory=list, description="2-12 dropdown values")
    reason: str = ""

    @field_validator("name")
    @classmethod
    def _snake(cls, v: str) -> str:
        return re.sub(r"[^a-z0-9]+", "_", str(v).strip().lower()).strip("_")


# Proposal for attribute reuse and creation within a category.
class _Proposal(BaseModel):
    reuse: List[_Reuse] = Field(
        default_factory=list, description="Attributes proposed for reuse"
    )
    create: List[_New] = Field(
        default_factory=list, description="Attributes proposed for creation"
    )
    missing_data: List[str] = Field(
        default_factory=list, description="Data missing for attribute proposal"
    )


_PROPOSE_PROMPT = """You are a product-data architect for a trading company.
Decide which attributes buyers need to filter and compare products in ONE category.

Rules:
1. PREFER reusing an existing attribute. Create a new one only if no existing
   attribute covers the meaning (check names AND labels, including synonyms).
2. Choose attributes that distinguish products inside THIS category
   (material, size, capacity, power type...). Skip generic ones it does not need.
3. name: snake_case ASCII. label: short Title Case, max 3 words.
4. Dropdown values: 2-12 concise, mutually exclusive, real-world values.
   If values are open-ended, leave values empty.
5. multiple_selection = true only when a product can have several values at once.
6. Do not repeat attributes already assigned to the category.
7. At most MAX_NEW new attributes. Put doubts in missing_data.
   Return ONLY a JSON object with keys: reuse, create, missing_data.
8. Use category.description to understand what products this category covers.
   If it is empty or unclear, add a question to missing_data instead of guessing.
"""


def _propose(
    category: dict,
    catalog: list,
    assigned: list,
    max_new: int,
    category_description: str = "",
) -> _Proposal:
    llm = build_llm(DEFAULT_MODEL, temperature=0.0)
    messages = [
        SystemMessage(content=_PROPOSE_PROMPT.replace("MAX_NEW", str(max_new))),
        HumanMessage(
            content=json.dumps(
                {
                    "category": {
                        "name": category.get("name"),
                        "description": category_description
                        or category.get("description"),
                    },
                    "existing_attributes": [
                        {"name": a.get("name"), "label": a.get("label")}
                        for a in catalog
                    ],
                    "already_assigned": assigned,
                },
                ensure_ascii=False,
            )
        ),
    ]
    try:
        out = llm.with_structured_output(_Proposal, method="json_schema").invoke(
            messages
        )
        if isinstance(out, _Proposal):
            return out
        if isinstance(out, dict):
            return _Proposal.model_validate(out)
    except Exception:  # noqa: BLE001 - model/package without structured output
        pass
    raw = message_text(llm.invoke(messages))
    return _Proposal.model_validate(extract_json_block(raw) or {})


# Input schema for the suggest_category_attributes tool.
class SuggestAttributesInput(BaseModel):
    category: str = Field(
        description="Exact category NAME as shown by fetch_category_tree, e.g. 'Wok'. "
        "Do not translate, pluralise or guess."
    )
    category_description: str = Field(
        default="",
        description=(
            "1-2 sentences explaining what products belong in this category and "
            "how buyers use them, e.g. 'Round-bottom cooking pans for stir-frying, "
            "used in restaurant and home kitchens'. Use the user's wording; do not invent facts."
        ),
    )
    max_new: int = Field(
        default=5,
        ge=1,
        le=8,
        description="Maximum number of NEW attributes to propose (1-8). Default 5.",
    )


# Tool for suggesting category attributes based on existing catalog and assigned attributes.
@tool(args_schema=SuggestAttributesInput)
def suggest_category_attributes(
    category: str, category_description: str = "", max_new: int = 5
) -> str:
    """Decide which attributes (label + dropdown values) a product category should have.

    Returns a JSON envelope with `steps` (each step is either kind "assign" an
    existing attribute, or "create" a new one with label and values).
    It builds NO SQL and writes nothing: call the build_* tools for each step.
    Do NOT use when the user already gave exact attribute names and values.
    """
    try:
        max_new = max(1, min(int(max_new or 5), 8))
        cats = _rows("master_categories")
        key = str(category or "").strip().lower()
        cat = next(
            (
                c
                for c in cats
                if key in (str(c.get("name", "")).lower(), str(c.get("id", "")).lower())
            ),
            None,
        )
        if cat is None:
            return json.dumps(
                {
                    "ok": False,
                    "error": f"category {category!r} not found",
                    "hint": "Use fetch_category_tree to find the exact name.",
                },
                ensure_ascii=False,
            )

        catalog = _rows("master_product_attributes")
        by_name = {str(a.get("name")): a for a in catalog}
        assign = _rows("master_product_category_attribute_assign")
        assigned_ids = {
            r.get("attribute_id")
            for r in assign
            if r.get("category_id") == cat.get("id")
        }
        assigned = [str(a.get("name")) for a in catalog if a.get("id") in assigned_ids]

        # Determine the category description to provide context for attribute proposal.
        description = (category_description or "").strip() or str(
            cat.get("description") or ""
        ).strip()

        proposal = _propose(cat, catalog, assigned, max_new, description)

        steps: List[Dict[str, Any]] = []
        risks: List[str] = []
        for r in proposal.reuse:
            if r.name not in by_name:
                risks.append(
                    f"model proposed unknown existing attribute {r.name!r}; ignored"
                )
            elif r.name not in assigned:
                steps.append(
                    {"kind": "assign", "attribute": r.name, "reason": r.reason}
                )
        for n in proposal.create[:max_new]:
            if not n.name:
                continue
            if n.name in by_name:  # model missed that it already exists
                if n.name not in assigned:
                    steps.append(
                        {
                            "kind": "assign",
                            "attribute": n.name,
                            "reason": "already exists; reused",
                        }
                    )
                continue
            values = list(
                dict.fromkeys(v.strip() for v in n.values if v and v.strip())
            )[:12]
            steps.append(
                {
                    "kind": "create",
                    "attribute": n.name,
                    "label": n.label or n.name,
                    "description": n.description,
                    "multiple_selection": n.multiple_selection,
                    "values": values,
                    "reason": n.reason,
                }
            )
        return json.dumps(
            {
                "ok": True,
                "category": {"id": cat.get("id"), "name": cat.get("name")},
                "steps": steps,
                "missing_data": proposal.missing_data,
                "risks": risks,
            },
            ensure_ascii=False,
        )
    except MasterApiError as exc:
        return _tool_error(exc)
    except Exception as exc:  # noqa: BLE001 - always return an envelope
        return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)


@tool
def fetch_category_tree() -> str:
    """Fetch every master_categories row and render it as an indented tree with ids."""
    try:
        rows = master_api._rows("master_categories")
    except MasterApiError as exc:
        return _tool_error(exc)

    by_id: Dict[str, Dict[str, Any]] = {str(r.get("id")): r for r in rows}
    depth_cache: Dict[str, int] = {}

    def depth(node: Dict[str, Any]) -> int:
        nid = str(node.get("id"))
        if nid in depth_cache:
            return depth_cache[nid]
        d = 0
        seen = {nid}
        cur = node
        while cur.get("parent_id"):
            pid = str(cur.get("parent_id"))
            if pid not in by_id or pid in seen:
                break
            seen.add(pid)
            cur = by_id[pid]
            d += 1
        depth_cache[nid] = d
        return d

    lines = []
    for row in sorted(rows, key=lambda r: str(r.get("name", "")).lower()):
        indent = "  " * depth(row)
        lines.append(f"{indent}{row.get('name')}")
    return json.dumps(
        {"ok": True, "count": len(rows), "tree": lines}, ensure_ascii=False
    )


@tool
def fetch_attribute_catalog() -> str:
    """Fetch every master_product_attributes row (name, label, multiple_selection, id)."""
    try:
        rows = master_api._rows("master_product_attributes")
    except MasterApiError as exc:
        return _tool_error(exc)

    lines = []
    for row in sorted(rows, key=lambda r: str(r.get("name", "")).lower()):
        lines.append(
            f"{row.get('name')} | label={row.get('label') or ''}"
            f" | multiple={bool(row.get('multiple_selection'))}"
        )
    return json.dumps(
        {"ok": True, "count": len(rows), "attributes": lines}, ensure_ascii=False
    )


@tool
def fetch_attribute_assignments(category: str, summarize: bool = False) -> str:
    """List attributes already assigned to a category (by name or id). Pass summarize=True for a compact list of names."""
    try:
        categories = master_api._rows("master_categories")
        cat, candidates = master_api._resolve(categories, "id", "name", category)
        if cat is None:
            return json.dumps(
                {
                    "ok": False,
                    "error": f"category not found: {category!r}",
                    "candidates": candidates,
                },
                ensure_ascii=False,
            )

        assigns = master_api._rows("master_product_category_attribute_assign")
        attributes = master_api._rows("master_product_attributes")
        attr_by_id = {str(a.get("id")): a for a in attributes}

        cat_assigns = [
            a for a in assigns if str(a.get("category_id")) == str(cat.get("id"))
        ]

        resolved = []
        for a in cat_assigns:
            attr = attr_by_id.get(str(a.get("attribute_id")), {})
            resolved.append(
                {
                    "attribute_name": attr.get("name", "?"),
                    "attribute_id": a.get("attribute_id"),
                }
            )
    except MasterApiError as exc:
        return _tool_error(exc)

    if summarize:
        names = sorted({r["attribute_name"] for r in resolved})
        return json.dumps(
            {
                "ok": True,
                "category": cat.get("name"),
                "category_id": cat.get("id"),
                "assigned_attributes": names,
                "count": len(names),
            },
            ensure_ascii=False,
        )

    resolved.sort(key=lambda r: r["attribute_name"])
    return json.dumps(
        {
            "ok": True,
            "category": cat.get("name"),
            "category_id": cat.get("id"),
            "assignments": resolved,
            "count": len(resolved),
        },
        ensure_ascii=False,
    )


@tool
def build_category_attribute_assign_sql(category: str, attributes: List[str]) -> str:
    """Build an idempotent MySQL INSERT..SELECT assigning attribute names to a category (name or id). Resolves ids, skips already-assigned pairs, and returns SQL plus verification statements. Read-only: nothing is executed."""
    try:
        categories = master_api._rows("master_categories")
        all_attrs = master_api._rows("master_product_attributes")

        cat, cat_candidates = master_api._resolve(categories, "id", "name", category)
        if cat is None:
            return json.dumps(
                {
                    "ok": False,
                    "error": f"category not found: {category!r}",
                    "candidates": cat_candidates,
                },
                ensure_ascii=False,
            )

        matched = []
        unknown = []
        for raw in attributes or []:
            target = str(raw).strip()
            if not target:
                continue
            match, candidates = master_api._resolve(all_attrs, "id", "name", target)
            if match is not None:
                matched.append(match)
            else:
                unknown.append({"attribute": target, "candidates": candidates})

        if not matched:
            return json.dumps(
                {
                    "ok": False,
                    "error": "no attributes resolved",
                    "category": cat.get("name"),
                    "unknown": unknown,
                },
                ensure_ascii=False,
            )

        assigns = master_api._rows("master_product_category_attribute_assign")
        existing_ids = {
            str(a.get("attribute_id"))
            for a in assigns
            if str(a.get("category_id")) == str(cat.get("id"))
        }

        already = [a["name"] for a in matched if str(a.get("id")) in existing_ids]
        to_add = [a for a in matched if str(a.get("id")) not in existing_ids]

        cat_id = sqlguard._sql_literal(cat.get("id"))
        names_lit = ", ".join(sqlguard._sql_literal(a["name"]) for a in to_add)
        ids_lit = ", ".join(sqlguard._sql_literal(a["id"]) for a in to_add)

        if to_add:
            sql = (
                "INSERT INTO master_product_category_attribute_assign "
                "(id, category_id, attribute_id)\n"
                "SELECT UUID(), c.id, a.id\n"
                "FROM master_categories AS c\n"
                "JOIN master_product_attributes AS a ON a.name IN (" + names_lit + ")\n"
                "WHERE c.id = " + cat_id + "\n"
                "  AND NOT EXISTS (\n"
                "    SELECT 1 FROM master_product_category_attribute_assign AS x\n"
                "    WHERE x.category_id = c.id AND x.attribute_id = a.id\n"
                "  );"
            )
            verification_before = (
                "SELECT a.name AS attribute_name, a.id AS attribute_id\n"
                "FROM master_categories c\n"
                "JOIN master_product_attributes a ON a.name IN (" + names_lit + ")\n"
                "WHERE c.id = " + cat_id + ";"
            )
            verification_after = (
                "SELECT COUNT(*) AS assigned FROM master_product_category_attribute_assign\n"
                "WHERE category_id = "
                + cat_id
                + " AND attribute_id IN ("
                + ids_lit
                + ");"
            )
        else:
            sql = "-- nothing to insert: all requested attributes are already assigned"
            verification_before = "-- nothing to verify (no new rows)"
            verification_after = "-- nothing to verify (no new rows)"

    except MasterApiError as exc:
        return _tool_error(exc)

    return json.dumps(
        {
            "ok": True,
            "category": cat.get("name"),
            "category_id": cat.get("id"),
            "to_add": [a["name"] for a in to_add],
            "already_assigned": already,
            "unknown_attributes": unknown,
            "sql": sql,
            "verification_before": verification_before,
            "verification_after": verification_after,
            "notes": "UUID() generates the 36-char id; NOT EXISTS keeps the insert idempotent.",
        },
        ensure_ascii=False,
    )


# Build SQL for creating a new product attribute. Returns a dict with SQL and verification queries.
def _create_attribute_sql(
    name,  # attribute name
    label="",
    description="",
    multiple_selection=False,
    dropdown_reference="",
    catalog_default_display=False,
) -> dict:
    """Pure builder. Returns a dict, never a JSON string."""
    name = (name or "").strip()
    label = (label or "").strip() or name
    if not sqlguard.ATTRIBUTE_NAME_RE.match(name):
        return {
            "ok": False,
            "error": f"invalid attribute name {name!r} (must be snake_case)",
        }

    existing = master_api._rows("master_product_attributes")
    if any(str(a.get("name", "")) == name for a in existing):
        return {"ok": True, "already_exists": True, "name": name, "sql": ""}

    lit = sqlguard._sql_literal
    cols = ["id", "name", "label"]
    vals = ["UUID()", lit(name), lit(label)]
    if description:
        cols.append("description")
        vals.append(lit(description))
    if dropdown_reference:
        cols.append("dropdown_reference")
        vals.append(lit(dropdown_reference))
    cols += ["multiple_selection", "catalog_default_display"]
    vals += [
        "1" if multiple_selection else "0",
        "1" if catalog_default_display else "0",
    ]

    sql = (
        f"INSERT INTO master_product_attributes ({', '.join(cols)})\n"
        f"SELECT {', '.join(vals)}\n"
        f"WHERE NOT EXISTS (SELECT 1 FROM master_product_attributes WHERE name = {lit(name)});"
    )
    return {
        "ok": True,
        "name": name,
        "sql": sql,
        "verification_before": f"SELECT id, name FROM master_product_attributes WHERE name = {lit(name)};",
        "verification_after": f"SELECT COUNT(*) AS created FROM master_product_attributes WHERE name = {lit(name)};",
    }


@tool
def build_create_attribute_sql(
    name: str,
    label: str = "",
    description: str = "",
    multiple_selection: bool = False,
    dropdown_reference: str = "",
    catalog_default_display: bool = False,
) -> str:
    """Build an idempotent INSERT for ONE attribute whose name/label are already decided.
    To decide the attributes for a category, use `suggest_category_attributes` first."""
    try:
        return json.dumps(
            _create_attribute_sql(
                name,
                label,
                description,
                multiple_selection,
                dropdown_reference,
                catalog_default_display,
            ),
            ensure_ascii=False,
        )
    except MasterApiError as exc:
        return _tool_error(exc)


@tool
def build_attribute_dropdown_sql(attribute: str, values: List[str]) -> str:
    """Build idempotent MySQL INSERTs adding dropdown values to an attribute. Read-only: nothing is executed."""
    try:
        all_attrs = master_api._rows("master_product_attributes")
        attr, candidates = master_api._resolve(all_attrs, "id", "name", attribute)
        if attr is None:
            return json.dumps(
                {
                    "ok": False,
                    "error": f"attribute not found: {attribute!r}",
                    "candidates": candidates,
                },
                ensure_ascii=False,
            )

        attr_id = str(attr.get("id"))
        attr_name = attr.get("name")
        statements = []
        for raw in values or []:
            value = str(raw).strip()
            if not value:
                continue
            val_lit = sqlguard._sql_literal(value)
            stmt = (
                "INSERT INTO master_product_attribute_dropdown (id, attribute_id, value)\n"
                f"SELECT UUID(), {sqlguard._sql_literal(attr_id)}, {val_lit}\n"
                "WHERE NOT EXISTS (SELECT 1 FROM master_product_attribute_dropdown "
                f"WHERE attribute_id = {sqlguard._sql_literal(attr_id)} AND value = {val_lit});"
            )
            statements.append({"value": value, "sql": stmt})

        verification_before = (
            "SELECT value FROM master_product_attribute_dropdown "
            f"WHERE attribute_id = {sqlguard._sql_literal(attr_id)};"
        )
        return json.dumps(
            {
                "ok": True,
                "attribute": attr_name,
                "attribute_id": attr_id,
                "statements": statements,
                "verification_before": verification_before,
            },
            ensure_ascii=False,
        )
    except MasterApiError as exc:
        return _tool_error(exc)


ATTRIBUTE_TOOLS = [
    suggest_category_attributes,
    list_master_tables,
    get_master_table_schema,
    fetch_category_tree,
    fetch_attribute_catalog,
    fetch_attribute_assignments,
    read_master_schema_file,
    read_table_definition,
    build_category_attribute_assign_sql,
    build_create_attribute_sql,
    build_attribute_dropdown_sql,
    execute_select_query,
]

ALLOWED_ATTRIBUTE_TABLES = (
    "master_product_attributes",
    "master_product_attribute_dropdown",
    "master_product_category_attribute_assign",
)
