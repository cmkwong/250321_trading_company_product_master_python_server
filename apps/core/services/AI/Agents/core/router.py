"""General supervisor: slash-command parsing and domain routing.

This is the real home of the cross-domain routing logic that used to live inside
``manager_general``:

- ``/plan`` / ``/act`` mode parsing (``parse_mode``) and the interactive REPL
  command parser (``SlashCommand`` / ``parse_command``);
- the general supervisor (``GeneralManagerAgent``) that owns a running
  conversation, routes each request to the ``product`` or ``sales`` domain
  manager, executes approved plans (``/act``) and compacts the history
  (``/compact``).

Domain managers live in ``domains.product`` and ``domains.sales``; they are
imported lazily inside :meth:`GeneralManagerAgent._manager` so this module has
no import cycle with them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from apps.core import config

from .engine import ProductPlan, assert_plan_sql
from .llm import DEFAULT_MODEL, NUM_CTX, build_llm
from .memory import ConversationMemory
from .contracts import canonical_digest, extract_json_block
from .act_tools import execute_plan

# Leading /plan or /act command token (only honoured at the start of the text,
# so a URL or path containing "/act" is not mistaken for a command).
_MODE_RE = re.compile(r"^\s*/(plan|act)\b[\s:,\-]*", re.IGNORECASE)


def parse_mode(text: Any, default: str = "plan") -> tuple:
    """Split an optional leading ``/plan`` or ``/act`` command from the request text.

    Returns ``(mode, task)``. ``mode`` is ``"plan"`` or ``"act"``; ``task`` is the
    remainder of the text with the command token stripped.  Text without a token
    defaults to ``plan`` (read-only), so nothing executes by accident.
    """
    text = str(text or "")
    match = _MODE_RE.match(text)
    if match:
        return match.group(1).lower(), _MODE_RE.sub("", text, count=1).strip()
    default = str(default).lower()
    if default not in ("plan", "act"):
        default = "plan"
    return default, text.strip()


@dataclass
class SlashCommand:
    """A parsed interactive command line (see :func:`parse_command`)."""

    name: str = "empty"  # empty|quit|reset|compact|help|dry|live|message
    mode: str = "plan"  # plan|act
    domain: Optional[str] = None  # product|sales override, or None to route
    text: str = ""  # the request text (mode/domain tokens stripped)


_QUIT_TOKENS = {"/q", "q", "/quit", "quit", "/exit", "exit"}
_RESET_TOKENS = {"/new", "/reset", "/clear"}
_DRY_TOKENS = {"/dry", "/dryrun", "/dry-run"}
_HELP_TOKENS = {"/help", "?"}
# Leading domain override: /product, /sales, @product, @sales.
_DOMAIN_RE = re.compile(
    r"^\s*(?:/(product|sales)|@(product|sales))\b[\s:,\-]*", re.IGNORECASE
)


def parse_command(raw: str) -> SlashCommand:
    """Parse an interactive REPL line into a :class:`SlashCommand`.

    Handles ``/q``, ``/new``, ``/compact``, ``/dry``, ``/live``, ``/help``,
    a leading ``/plan`` or ``/act`` mode, and a ``/product`` | ``/sales`` |
    ``@product`` | ``@sales`` domain override.  Anything else is a plain message
    (plan mode by default).
    """
    text = (raw or "").strip()
    low = text.lower()
    if not text:
        return SlashCommand(name="empty")
    if low in _QUIT_TOKENS:
        return SlashCommand(name="quit")
    if low in _RESET_TOKENS:
        return SlashCommand(name="reset")
    if low in _DRY_TOKENS:
        return SlashCommand(name="dry")
    if low == "/live":
        return SlashCommand(name="live")
    if low == "/compact":
        return SlashCommand(name="compact")
    if low in _HELP_TOKENS:
        return SlashCommand(name="help")

    domain: Optional[str] = None
    domain_match = _DOMAIN_RE.match(text)
    if domain_match:
        domain = (domain_match.group(1) or domain_match.group(2)).lower()
        text = _DOMAIN_RE.sub("", text, count=1).strip()

    mode, task = parse_mode(text, default="plan")
    return SlashCommand(name="message", mode=mode, domain=domain, text=task)


_ROUTER_SYSTEM = """Classify the user's request into exactly one domain.
- product: product master data (categories, attributes, dropdown values, name \
types, products, prices, costs, images, 1688 links, keywords).
- sales: quotations, sales documents, packing, shipping, customers, invoicing, \
incoterms, freight.
If the request is a follow-up to the recent conversation, keep the same domain \
unless the topic clearly changes.
Reply with JSON only: {"domain": "product"|"sales", "reason": "<short>"}"""

_SALES_KEYWORDS = (
    "quotation",
    "invoice",
    "packing",
    "shipping",
    "shipment",
    "customer",
    "order",
    "sales",
    "incoterm",
    "freight",
    "cbm",
    "carton",
)
_PRODUCT_KEYWORDS = (
    "category",
    "attribute",
    "product",
    "sku",
    "price",
    "cost",
    "image",
    "1688",
    "name",
    "material",
    "color",
    "size",
    "capacity",
    "dropdown",
    "keyword",
    "certificate",
)


class GeneralManagerAgent(ConversationMemory):
    """Supervisor agent that owns the conversation and routes to a domain manager.

    ``pinned="product"`` skips the router entirely (the ``-mp`` CLI); otherwise
    each request is classified and dispatched to ``product`` or ``sales``.
    """

    DOMAINS = ("product", "sales")

    def __init__(
        self,
        model: Optional[str] = None,
        temperature: float = 0.0,
        recursion_limit: int = 25,
        pinned: Optional[str] = None,
        router: Optional[str] = None,
        llm: Any = None,
    ) -> None:
        super().__init__()
        self.model = model or DEFAULT_MODEL
        self.temperature = temperature
        self.recursion_limit = recursion_limit
        self.pinned = pinned
        self.router = (
            router or str(config.get("AI_GENERAL_ROUTER", "llm")).strip().lower()
        )
        self.llm = llm or build_llm(
            self.model, temperature=self.temperature, num_ctx=NUM_CTX
        )
        self._managers: Dict[str, Any] = {}
        self.last_domain: str = pinned or "product"
        self.last_plan: Optional[ProductPlan] = None

    def reset(self) -> None:
        """Forget the conversation and the last plan (agents stay loaded)."""
        super().reset()
        self.last_plan = None
        self.last_domain = self.pinned or "product"

    # -- domain managers ----------------------------------------------------
    def _manager(self, domain: str):
        domain = domain or self.last_domain or "product"
        if domain not in self._managers:
            self._managers[domain] = self._build_manager(domain)
        return self._managers[domain]

    def _build_manager(self, domain: str):
        if domain == "product":
            from apps.core.services.AI.Agents.domains.product.manager import (
                ProductMasterAgent,
            )

            return ProductMasterAgent(
                model=self.model,
                temperature=self.temperature,
                recursion_limit=self.recursion_limit,
            )
        if domain == "sales":
            from apps.core.services.AI.Agents.domains.sales.manager import (
                SalesManagerAgent,
            )

            return SalesManagerAgent(
                model=self.model,
                temperature=self.temperature,
                recursion_limit=self.recursion_limit,
            )
        raise ValueError(f"Unknown domain: {domain!r}")

    # -- routing ------------------------------------------------------------
    def classify(self, task: str) -> str:
        """Pick the domain for a request (explicit overrides skip this)."""
        if self.pinned:
            return self.pinned
        if self.router == "keyword":
            return self._keyword_classify(task) or self.last_domain

        recent = self.transcript(limit=3000)
        try:
            response = self.llm.invoke(
                [
                    ("system", _ROUTER_SYSTEM),
                    ("human", f"Recent conversation:\n{recent}\n\nNew request: {task}"),
                ]
            )
            data = extract_json_block(str(getattr(response, "content", "") or ""))
            domain = str((data or {}).get("domain", "")).strip().lower()
            if domain in self.DOMAINS:
                self.last_domain = domain
                return domain
        except Exception:  # noqa: BLE001 - fall back to keywords
            pass
        return self._keyword_classify(task) or self.last_domain

    @staticmethod
    def _keyword_classify(task: str) -> Optional[str]:
        text = str(task).lower()
        sales = any(k in text for k in _SALES_KEYWORDS)
        product = any(k in text for k in _PRODUCT_KEYWORDS)
        if sales and not product:
            return "sales"
        if product and not sales:
            return "product"
        if sales:
            return "sales"
        if product:
            return "product"
        return None

    # -- turns --------------------------------------------------------------
    def _maybe_auto_compact(self) -> Optional[Dict[str, Any]]:
        if str(config.get("AI_AUTO_COMPACT", "1")).strip().lower() not in (
            "1",
            "true",
            "yes",
        ):
            return None
        limit = int(config.get("AI_HISTORY_MAX_CHARS", "48000"))
        if len(self.transcript()) > limit:
            return self.compact(force=True)
        return None

    def stream(self, task: str, domain: Optional[str] = None):
        """Run one turn through the routed domain manager, yielding events.

        Emits the same ``status``/``token``/``plan``/``error`` events as the
        domain managers, prefixed by a ``status`` naming the chosen domain.  The
        finished turn is recorded into the shared conversation memory.
        """
        if not str(task).strip():
            yield {"type": "status", "text": "empty request"}
            return
        self._maybe_auto_compact()
        domain = domain or self.classify(task)
        self.last_domain = domain
        sub = self._manager(domain)
        yield {"type": "status", "text": f"domain: {domain}"}
        plan: Optional[ProductPlan] = None
        for event in sub.stream(task, history=self.history()):
            if event["type"] == "plan":
                plan = event["plan"]
            yield event
        if plan is not None:
            self.record(task, plan)

    def plan(self, task: str, domain: Optional[str] = None) -> ProductPlan:
        """Run one routed turn and return the guarded plan (no token streaming)."""
        plan: Optional[ProductPlan] = None
        for event in self.stream(task, domain=domain):
            if event["type"] == "plan":
                plan = event["plan"]
        return plan or {}

    # -- act ----------------------------------------------------------------
    def act(
        self,
        plan: ProductPlan,
        *,
        dry_run: bool = True,
        confirm: bool = False,
        plan_hash: Optional[str] = None,
        plan_id: Optional[str] = None,
    ):
        """Guard and execute an approved plan; return ``(plan, executed)``.

        When ``plan_hash`` is supplied it must match the digest the plan was
        reviewed with (computed at ``/plan`` time), so ``/act`` can only run the
        statements a human already approved -- never a regenerated plan.
        """
        if plan_hash is not None and canonical_digest(plan) != plan_hash:
            raise ValueError("plan changed since review; re-run /plan")
        plan = assert_plan_sql(plan, mode="act")
        executed = execute_plan(plan, plan_id=plan_id, dry_run=dry_run, confirm=confirm)
        return plan, executed

