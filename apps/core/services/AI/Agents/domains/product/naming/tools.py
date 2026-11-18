"""Helper logic for the product-naming sub-agent (image + name SQL helpers)."""

from __future__ import annotations

import base64
import io
import mimetypes
import os
from typing import Any, Dict, List

import requests

from apps.core import config
from apps.core.services.AI.Agents.core.contracts import MasterApiError
from apps.core.services.AI.Agents.core.master_api import _rows
from apps.core.services.AI.Agents.core.sql import _sql_literal

MAX_IMAGE_PX = int(config.get("AI_IMAGE_MAX_PX", "1024"))
IMAGE_MAX_BYTES = int(config.get("AI_IMAGE_MAX_BYTES", str(5 * 1024 * 1024)))
IMAGE_TIMEOUT = float(config.get("MASTER_API_TIMEOUT", "20"))


def _downscale(raw: bytes, mime: str) -> bytes:
    try:
        from PIL import Image
    except Exception:  # noqa: BLE001 - Pillow optional at this path
        return raw
    try:
        img = Image.open(io.BytesIO(raw))
        img.thumbnail((MAX_IMAGE_PX, MAX_IMAGE_PX))
        buf = io.BytesIO()
        is_png = "png" in mime.lower()
        fmt = "PNG" if is_png else "JPEG"
        img.convert("RGBA" if is_png else "RGB").save(buf, format=fmt, quality=85)
        return buf.getvalue()
    except Exception:  # noqa: BLE001 - fall back to the raw bytes
        return raw


def _to_data_url(ref: str) -> str:
    """Normalise a photo reference into a base64 ``data:`` URL."""
    ref = (ref or "").strip()
    if not ref:
        return ""
    if ref.startswith("data:"):
        return ref

    if ref.startswith(("http://", "https://")):
        session = requests.Session()
        session.trust_env = False
        resp = session.get(
            ref, timeout=IMAGE_TIMEOUT, headers={"User-Agent": "Mozilla/5.0"}
        )
        resp.raise_for_status()
        raw = resp.content
        mime = (resp.headers.get("Content-Type", "") or "image/jpeg").split(";")[0]
        if len(raw) > IMAGE_MAX_BYTES:
            raw = _downscale(raw, mime)
        return "data:" + mime + ";base64," + base64.b64encode(raw).decode("ascii")

    if not os.path.isabs(ref):
        raise ValueError(f"image path must be absolute: {ref!r}")
    if not os.path.isfile(ref):
        raise ValueError(f"image file not found: {ref!r}")
    mime = mimetypes.guess_type(ref)[0] or "image/jpeg"
    with open(ref, "rb") as fh:
        raw = fh.read()
    if len(raw) > IMAGE_MAX_BYTES:
        raw = _downscale(raw, mime)
    return "data:" + mime + ";base64," + base64.b64encode(raw).decode("ascii")


def _build_product_name_sql(
    product_id: str, name_type: str, name: str, display_order: int = 1
) -> Dict[str, str]:
    """Build an idempotent INSERT for one ``product_names`` row."""
    try:
        rows = _rows("master_product_name_types")
        matched = next(
            (
                r
                for r in rows
                if str(r.get("name", "")).strip().lower() == name_type.lower()
            ),
            None,
        )
    except MasterApiError:
        matched = None

    pid = _sql_literal(product_id)
    name_lit = _sql_literal(name)
    type_lit = _sql_literal(name_type)

    if matched is None:
        return {
            "purpose": "",
            "statement": "",
            "verification": "",
            "error": f"unknown name_type {name_type!r}",
        }

    statement = (
        "INSERT INTO product_names (id, product_id, name, name_type_id, display_order)\n"
        "SELECT UUID(), "
        + pid
        + ", "
        + name_lit
        + ", t.id, "
        + str(int(display_order))
        + "\n"
        "FROM master_product_name_types AS t\n"
        "WHERE t.name = " + type_lit + "\n"
        "  AND NOT EXISTS (\n"
        "    SELECT 1 FROM product_names AS p\n"
        "    WHERE p.product_id = " + pid + "\n"
        "      AND p.name_type_id = t.id\n"
        "      AND p.name = " + name_lit + "\n"
        "  );"
    )
    verification = (
        "SELECT p.name, t.name AS name_type FROM product_names p\n"
        "JOIN master_product_name_types t ON t.id = p.name_type_id\n"
        "WHERE p.product_id = " + pid + " AND p.name = " + name_lit + ";"
    )
    return {
        "purpose": f"assign {name_type} name to product {product_id}",
        "statement": statement,
        "verification": verification,
    }
