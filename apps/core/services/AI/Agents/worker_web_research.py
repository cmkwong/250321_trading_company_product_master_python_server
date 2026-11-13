"""Worker that reads a web page and summarises it against target fields."""

from __future__ import annotations

import ipaddress
import socket
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import tool

from apps.core import config
from . import worker_base as base

MAX_CHARS = int(config.get("AI_WEB_FETCH_MAX_CHARS", "8000"))
MAX_URLS = int(config.get("AI_WEB_MAX_URLS", "3"))
TIMEOUT = float(config.get("MASTER_API_TIMEOUT", "20"))
SHORT_PAGE_CHARS = 200

WEB_SYSTEM_PROMPT = "Answer using only the provided web page content."


def _validate_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"unsupported URL scheme: {url!r}")
    host = parsed.hostname
    if not host:
        raise ValueError(f"URL has no host: {url!r}")
    allow_private = str(config.get("AI_WEB_ALLOW_PRIVATE_HOSTS", "0")).lower() in (
        "1",
        "true",
        "yes",
    )
    if not allow_private:
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            ip = ipaddress.ip_address(socket.gethostbyname(host))
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            raise ValueError(f"blocked private/loopback host: {host}")
    return url


def fetch_text(url: str, max_chars: Optional[int] = None) -> str:
    """Fetch a page and return clean, whitespace-collapsed text."""
    max_chars = max_chars or MAX_CHARS
    _validate_url(url)
    session = requests.Session()
    session.trust_env = False
    resp = session.get(url, timeout=TIMEOUT, headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header"]):
        tag.decompose()
    text = " ".join(soup.get_text(separator=" ").split())
    return text[:max_chars]


class WebResearchWorker:
    """Summarise web pages into target fields using a local model."""

    def __init__(self, model: Optional[str] = None, llm: Any = None):
        self.model = model or base.DEFAULT_MODEL
        self.llm = llm or base.build_llm(self.model)

    def research(
        self,
        urls: Optional[List[str]] = None,
        target_fields: Optional[List[str]] = None,
        question: Optional[str] = None,
    ) -> Dict[str, Any]:
        urls = [u for u in (urls or []) if str(u).strip()][:MAX_URLS]
        target_fields = [f for f in (target_fields or []) if str(f).strip()]
        if not urls:
            return {"worker": "web_research", "ok": False, "error": "no URLs provided"}

        sources: List[Dict[str, Any]] = []
        pages: List[str] = []
        risks: List[str] = []
        for url in urls:
            try:
                text = fetch_text(str(url))
                sources.append({"url": url, "chars": len(text), "error": None})
                if len(text.strip()) < SHORT_PAGE_CHARS:
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
                "worker": "web_research",
                "ok": False,
                "error": "could not fetch any page",
                "sources": sources,
                "risks": risks,
            }

        combined = "\n\n".join(pages)[:MAX_CHARS]
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
            text = base.message_text(
                self.llm.invoke(
                    [
                        SystemMessage(content=WEB_SYSTEM_PROMPT),
                        HumanMessage(content=user),
                    ]
                )
            )
        except Exception as exc:  # noqa: BLE001
            return {
                "worker": "web_research",
                "ok": False,
                "error": str(exc),
                "sources": sources,
                "risks": risks,
            }

        data = base.extract_json_block(text) or {}
        fields = data.get("fields") if isinstance(data.get("fields"), dict) else {}

        missing = list(base.as_str_list(data.get("missing_fields")))
        for f in target_fields:
            if (f not in fields or fields[f] in (None, "")) and f not in missing:
                missing.append(f)

        return {
            "worker": "web_research",
            "ok": True,
            "summary": str(data.get("summary", "")),
            "fields": fields,
            "quotes": base.as_str_list(data.get("quotes")),
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
        result = WebResearchWorker().research(
            urls=urls, target_fields=target_fields, question=question
        )
    except Exception as exc:  # noqa: BLE001
        result = {"worker": "web_research", "ok": False, "error": str(exc)}
    return base.dumps(result)
