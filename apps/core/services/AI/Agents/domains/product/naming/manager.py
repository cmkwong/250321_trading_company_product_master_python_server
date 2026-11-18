"""Product-naming sub-agent manager.

Owns the ``ProductNameAgent`` (a vision model that renames a Chinese product
name into SEO English) and the ``rename_product_with_seo`` ``@tool``.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from langchain_core.messages import HumanMessage
from langchain_core.tools import tool

from apps.core.services.AI.Agents.core.agent import ToolLoopAgent
from apps.core.services.AI.Agents.core.contracts import (
    as_str_list,
    dump_agent_result,
    extract_json_block,
)
from apps.core.services.AI.Agents.core.llm import VISION_MODEL, build_llm, message_text

from apps.core.services.AI.Agents.domains.product.naming.prompts import NAME_SYSTEM_PROMPT
from apps.core.services.AI.Agents.domains.product.naming.tools import (
    _build_product_name_sql,
    _to_data_url,
)


class ProductNameAgent(ToolLoopAgent):
    """Rename a Chinese product name into SEO English using a vision model.

    Runs a single ``model`` node graph (``build_tool_graph`` with no tools); the
    system prompt is prepended on each call but never stored in state.
    """

    DEFAULT_MODEL_NAME = VISION_MODEL
    TOOLS = ()
    SYSTEM_PROMPT = NAME_SYSTEM_PROMPT

    def _build_llm(self):
        return build_llm(self.model, vision=True)

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
        """Run the rename sub-agent and return a structured envelope."""
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
                "agent": "product_name",
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
            "agent": "product_name",
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
        result = ProductNameAgent().rename(
            chinese_name=chinese_name,
            images=images,
            seo_keywords=seo_keywords,
            name_type=name_type,
            product_id=product_id,
        )
    except Exception as exc:  # noqa: BLE001
        result = {"agent": "product_name", "ok": False, "error": str(exc)}
    return dump_agent_result(result)
