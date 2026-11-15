"""Web fetch helpers (SSRF-guarded) for the AI tools."""

from __future__ import annotations

import ipaddress
import socket
from typing import Optional
from urllib.parse import urlparse

import requests

from apps.core import config

from .master_api import REQUEST_TIMEOUT

WEB_FETCH_MAX_CHARS = int(config.get("AI_WEB_FETCH_MAX_CHARS", "8000"))


def validate_url(url: str) -> str:
    """Reject non-http(s) and private/loopback hosts; return the URL unchanged."""
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
    from bs4 import BeautifulSoup

    max_chars = max_chars or WEB_FETCH_MAX_CHARS
    validate_url(url)
    session = requests.Session()
    session.trust_env = False
    resp = session.get(
        url, timeout=REQUEST_TIMEOUT, headers={"User-Agent": "Mozilla/5.0"}
    )
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header"]):
        tag.decompose()
    text = " ".join(soup.get_text(separator=" ").split())
    return text[:max_chars]


def fetch_html(url: str, max_chars: Optional[int] = None) -> str:
    """Fetch a page and return the raw HTML markup (truncated)."""
    max_chars = max_chars or WEB_FETCH_MAX_CHARS
    validate_url(url)
    session = requests.Session()
    session.trust_env = False
    resp = session.get(
        url, timeout=REQUEST_TIMEOUT, headers={"User-Agent": "Mozilla/5.0"}
    )
    resp.raise_for_status()
    return resp.text[:max_chars]
