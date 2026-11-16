"""Worker that renames a Chinese product name into SEO-friendly English.

Uses a local vision model (default ``gemma3:4b``) to read the supplied product
photos together with the Chinese name, then produces a marketplace-ready
English name, keywords, slug and a plain translation.  When a ``product_id`` is
supplied it also emits an idempotent ``INSERT`` into ``product_names`` (draft
SQL, never executed).
"""

from __future__ import annotations

import base64
import io
import mimetypes
import os
from typing import Any, Dict, List, Optional

import requests
from langchain_core.messages import HumanMessage
from langchain_core.tools import tool

from apps.core import config
from apps.core.services.AI.Agents.core.contracts import (
    as_str_list,
    dump_worker_result,
    extract_json_block,
)
from apps.core.services.AI.Agents.core.graph import build_tool_graph
from apps.core.services.AI.Agents.core.llm import VISION_MODEL, build_llm, message_text
from apps.core.services.AI.Agents.core.contracts import MasterApiError
from apps.core.services.AI.Agents.core.master_api import _rows
from apps.core.services.AI.Agents.core.sql import _sql_literal

MAX_IMAGE_PX = int(config.get("AI_IMAGE_MAX_PX", "1024"))
IMAGE_MAX_BYTES = int(config.get("AI_IMAGE_MAX_BYTES", str(5 * 1024 * 1024)))
IMAGE_TIMEOUT = float(config.get("MASTER_API_TIMEOUT", "20"))

NAME_SYSTEM_PROMPT = """You are an e-commerce SEO copywriter for a trading company.
Given a product name in Chinese and, optionally, product photos, produce an
optimised English product name plus supporting metadata.

Rules:
1. Translate the Chinese name faithfully, then write a marketplace-optimised
   English name (front-load the most searched keyword, stay under 70 chars).
2. Capitalise the first letter of each word; use only ASCII letters, digits and
   spaces (no Chinese characters, emoji, hyphens, pipes or other symbols).
3. The meaning and use-case of the optimised name MUST match the source name.
4. If hot keywords are supplied, they MUST appear in the optimised name where
   they read naturally.
5. Never invent materials, colours, sizes, certifications or performance claims
   that are not visible in the photos or the source name.

Return ONLY a JSON object (no markdown, no prose) with exactly:
- source_name_cn: the original Chinese name.
- plain_translation: a simple direct English translation.
- seo_name: the optimised English name (<=70 chars).
- short_name: a short display name (<=40 chars).
- slug: a lowercase hyphenated URL slug of seo_name.
- keywords: list of 5-10 distinct SEO keywords.
- detected_attributes: object of visible attributes you are confident about
  (e.g. material, color, size); use null when unsure.
- description: a 1-2 sentence marketing description.
- confidence: number 0..1 for how sure you are of the reading.
- missing_data: list of things the photos/name cannot answer.
- risks: list of assumptions or warnings.
"""


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


class ProductNameWorker:
    """Rename a Chinese product name into SEO English using a vision model.

    Runs through a single ``model`` node graph (``core.graph.build_tool_graph``
    with no tools); the system prompt is prepended on each call but never stored
    in state.
    """

    def __init__(self, model: Optional[str] = None, llm: Any = None):
        self.model = model or VISION_MODEL
        self.llm = llm or build_llm(self.model, vision=True)
        self._agent = None

    def _get_agent(self):
        """Compile once: a single ``model`` node (this worker uses no tools)."""
        if self._agent is None:
            self._agent = build_tool_graph(self.llm, [], NAME_SYSTEM_PROMPT)
        return self._agent

    def rename(
        self,
        chinese_name: str,
        images: Optional[List[str]] = None,
        seo_keywords: Optional[List[str]] = None,
        seo_file: Optional[str] = None,
        name_type: str = "seo",
        tone: str = "professional",
        product_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Run the rename worker and return a structured envelope."""
        images = [i for i in (images or []) if str(i).strip()]
        seo_keywords = list(seo_keywords or [])

        if seo_file:
            try:
                from apps.core.services.file_service import FileController

                extra = FileController().readSEOWords_Alibaba(seo_file)
                if extra:
                    seo_keywords = seo_keywords + [extra]
            except Exception:  # noqa: BLE001 - SEO file is best-effort
                pass

        data_urls: List[str] = []
        image_errors: List[str] = []
        for ref in images:
            try:
                data_urls.append(_to_data_url(str(ref)))
            except Exception as exc:  # noqa: BLE001 - record and continue
                image_errors.append(f"{ref}: {exc}")

        prompt_lines = [
            f"Product name in Chinese: {chinese_name}",
            f"Tone: {tone}",
        ]
        if seo_keywords:
            prompt_lines.append(
                "Hot keywords to include (naturally): " + ", ".join(seo_keywords)
            )
        prompt_lines.append(
            "Return ONLY the JSON object described in the system prompt."
        )

        content: List[Dict[str, Any]] = [
            {"type": "text", "text": "\n".join(prompt_lines)}
        ]
        for url in data_urls:
            content.append({"type": "image_url", "image_url": {"url": url}})

        try:
            state = self._get_agent().invoke(
                {"messages": [HumanMessage(content=content)]}
            )
            text = message_text(state["messages"][-1])
        except Exception as exc:  # noqa: BLE001 - surface as an error envelope
            return {
                "worker": "product_name",
                "ok": False,
                "error": str(exc),
                "hint": "Check the Ollama vision model is loaded.",
            }

        data = extract_json_block(text) or {}
        sql: List[Dict[str, str]] = []
        missing = list(as_str_list(data.get("missing_data")))
        risks = list(as_str_list(data.get("risks")))
        risks += [f"image skipped: {e}" for e in image_errors]

        if product_id:
            name_to_store = data.get("seo_name") or data.get("plain_translation") or ""
            if name_to_store:
                step = _build_product_name_sql(
                    str(product_id), name_type, str(name_to_store)
                )
                if step.get("error"):
                    missing.append(step["error"])
                else:
                    sql.append(step)
            else:
                missing.append("no English name produced; product_names INSERT skipped")

        return {
            "worker": "product_name",
            "ok": True,
            "summary": data.get("seo_name") or data.get("plain_translation") or "",
            "data": data,
            "sql": sql,
            "missing_data": missing,
            "risks": risks,
        }


@tool
def rename_product_with_seo(
    chinese_name: str,
    images: Optional[List[str]] = None,
    seo_keywords: Optional[List[str]] = None,
    name_type: str = "seo",
    product_id: Optional[str] = None,
) -> str:
    """Rename a Chinese product name into an SEO English name (optionally reading product photos). Returns a JSON envelope with names, keywords and draft product_names SQL (never executed)."""
    try:
        result = ProductNameWorker().rename(
            chinese_name=chinese_name,
            images=images,
            seo_keywords=seo_keywords,
            name_type=name_type,
            product_id=product_id,
        )
    except Exception as exc:  # noqa: BLE001
        result = {"worker": "product_name", "ok": False, "error": str(exc)}
    return dump_worker_result(result)
