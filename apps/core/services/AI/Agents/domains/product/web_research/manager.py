"""Product web-research sub-agent manager.

Summarises web pages into target fields using a local model, and exposes the
``research_product_web`` ``@tool`` the product manager delegates to.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import tool

from apps.core.services.AI.Agents.core.agent import ToolLoopAgent
from apps.core.services.AI.Agents.core.contracts import (
    as_str_list,
    dump_agent_result,
    extract_json_block,
)
from apps.core.services.AI.Agents.core.llm import message_text

from apps.core.services.AI.Agents.domains.product.web_research.prompts import (
    WEB_SYSTEM_PROMPT,
)
from apps.core.services.AI.Agents.domains.product.web_research import tools as web_tools


class WebResearchAgent(ToolLoopAgent):
    """Summarise web pages into target fields using a local model."""

    TOOLS = ()
    SYSTEM_PROMPT = WEB_SYSTEM_PROMPT

    def research(
        self,
        urls: Optional[List[str]] = None,
        target_fields: Optional[List[str]] = None,
        question: Optional[str] = None,
    ) -> Dict[str, Any]:
        urls = [u for u in (urls or []) if str(u).strip()][: web_tools.MAX_URLS]
        target_fields = [f for f in (target_fields or []) if str(f).strip()]
        if not urls:
            return {"agent": "web_research", "ok": False, "error": "no URLs provided"}

        sources: List[Dict[str, Any]] = []
        pages: List[str] = []
        risks: List[str] = []
        for url in urls:
            try:
                text = web_tools.fetch_text(str(url))
                sources.append({"url": url, "chars": len(text), "error": None})
                if len(text.strip()) < web_tools.SHORT_PAGE_CHARS:
                    risks.append(
                        f"page returned little text ({len(text)} chars), may be JS-rendered: {url}"
                    )
                else:
                    pages.append(text)
            except Exception as exc:  # noqa: BLE001 - record and continue
                sources.append({"url": url, "chars": 0, "error": str(exc)})
                risks.append(f"fetch failed for {url}: {exc}")

        if not pages:
            return {
                "agent": "web_research",
                "ok": False,
                "error": "could not fetch any page",
                "sources": sources,
                "risks": risks,
            }

        combined = "\n\n".join(pages)[: web_tools.MAX_CHARS]
        if question:
            user = f"Page content:\n{combined}\n\nQuestion: {question}\n"
        else:
            user = f"Page content:\n{combined}\n\n"
        if target_fields:
            fields_json = ", ".join(repr(f) for f in target_fields)
            user += (
                f"Extract these fields from the page: {fields_json}. "
                "Return ONLY a JSON object with keys: summary (string), "
                "fields (object mapping each field to its value or null), "
                "quotes (list of short verbatim excerpts), "
                "missing_fields (list of fields not found in the page)."
            )
        else:
            user += (
                "Summarize this page in 5 bullet points and return ONLY a JSON "
                "object with keys: summary (string), fields (empty object), "
                "quotes (list), missing_fields (list)."
            )

        try:
            text = message_text(
                self.llm.invoke(
                    [
                        SystemMessage(content=WEB_SYSTEM_PROMPT),
                        HumanMessage(content=user),
                    ]
                )
            )
        except Exception as exc:  # noqa: BLE001
            return {
                "agent": "web_research",
                "ok": False,
                "error": str(exc),
                "sources": sources,
                "risks": risks,
            }

        data = extract_json_block(text) or {}
        fields = data.get("fields") if isinstance(data.get("fields"), dict) else {}

        missing = list(as_str_list(data.get("missing_fields")))
        for f in target_fields:
            if (f not in fields or fields[f] in (None, "")) and f not in missing:
                missing.append(f)

        return {
            "agent": "web_research",
            "ok": True,
            "summary": str(data.get("summary", "")),
            "fields": fields,
            "quotes": as_str_list(data.get("quotes")),
            "missing_fields": missing,
            "sources": sources,
            "risks": risks,
        }


@tool
def research_product_web(
    urls: List[str],
    target_fields: Optional[List[str]] = None,
    question: Optional[str] = None,
) -> str:
    """Fetch one or more web pages and summarise them into the requested target fields. Returns a JSON envelope (never fabricates fields not present on the page)."""
    try:
        result = WebResearchAgent().research(
            urls=urls, target_fields=target_fields, question=question
        )
    except Exception as exc:  # noqa: BLE001
        result = {"agent": "web_research", "ok": False, "error": str(exc)}
    return dump_agent_result(result)
