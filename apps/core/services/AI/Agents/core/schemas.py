"""Pydantic argument schemas for the AI tools.

These replace the ad-hoc ``_normalise_tables`` / ``_normalise_execute`` string
coercion: the model often passes ``"a,b"`` where a list is expected, or ``"false"``
where a bool is expected.  Declaring the coercion on the tool's ``args_schema``
fixes it once, at the boundary, before the function body runs.
"""

from __future__ import annotations

from typing import Annotated, Any, Dict, List, Optional

from pydantic import BaseModel, BeforeValidator, Field

# Sentinel table names the model uses as "no hint".
_PLACEHOLDER_TABLES = ("all", "*", "none")


def _csv_to_list(value: Any) -> Optional[List[str]]:
    """Coerce a comma-separated string or an iterable into a clean list of names.

    Drops blanks and placeholder names (``all``, ``*``, ``none``); returns ``None``
    when there is nothing left (the same "no hint" signal the workers expect).
    """
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        parts = [str(p).strip() for p in value]
    elif isinstance(value, str):
        parts = [p.strip() for p in value.split(",")]
    else:
        return None
    parts = [p for p in parts if p and p.lower() not in _PLACEHOLDER_TABLES]
    return parts or None


def _bool_like(value: Any) -> bool:
    """Coerce ``"false"`` / ``"0"`` / ``"no"`` / ``""`` (and friends) to a bool."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() not in ("false", "0", "no", "off", "none", "")
    return bool(value)


TablesArg = Annotated[Optional[List[str]], BeforeValidator(_csv_to_list)]
BoolArg = Annotated[bool, BeforeValidator(_bool_like)]


class BuildSqlInput(BaseModel):
    """Arguments for the SQL-builder worker tools (product + sales)."""

    question: str = Field(min_length=1)
    tables: TablesArg = None
    execute: BoolArg = True


class BuildInsertInput(BaseModel):
    """Arguments for ``build_insert_sql``."""

    table: str = Field(min_length=1)
    values: Dict[str, Any] = Field(min_length=1)


class BuildUpdateInput(BaseModel):
    """Arguments for ``build_update_sql`` (``where`` is required downstream)."""

    table: str = Field(min_length=1)
    set_values: Dict[str, Any] = Field(min_length=1)
    where: str = ""

