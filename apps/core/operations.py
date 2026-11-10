"""Command operations registered in the shared registry.

Each operation is a pure callable that the HTTP layer (and, later, the CLI)
can invoke by command name.  Only the stateless ``sync`` commands are
registered here; the ``job`` / ``desktop`` commands are added in a later step.
"""

from apps.core import commands as registry
from apps.core.services.chat_service import ChatController
from apps.core.services.price_service import PriceController


@registry.command(
    name="price",
    params={
        "qtyString": ["10, 100, 500, 1000", str],
        "cost": ["", float],
        "profit_max": ["", float, "In %"],
        "profit_min": ["", float, "In %"],
    },
    kind=registry.KIND_SYNC,
)
def price(qtyString="10, 100, 500, 1000", cost=0.0, profit_max=0.0, profit_min=0.0):
    return PriceController().price_range(
        qtyString=qtyString, cost=cost, profit_max=profit_max, profit_min=profit_min
    )


@registry.command(
    name="translate",
    aliases=("t", "tl"),
    params={
        "content": ["", str],
        "fromT": ["Chinese", str],
        "toT": ["English", str],
        "tone": ["Simple and Professional", str],
    },
    kind=registry.KIND_SYNC,
)
def translate(content="", fromT="Chinese", toT="English", tone="Simple and Professional"):
    return ChatController().translate_content(content, fromT, toT, tone)


@registry.command(
    name="keywords",
    aliases=("kw",),
    params={"content": ["", str]},
    kind=registry.KIND_SYNC,
)
def keywords(content=""):
    return ChatController().generate_product_keywords(content)


@registry.command(
    name="description",
    aliases=("des",),
    params={"content": ["", str], "tone": ["business and professional", str]},
    kind=registry.KIND_SYNC,
)
def description(content="", tone="business and professional"):
    return ChatController().generate_product_description(content, tone)


@registry.command(
    name="email",
    aliases=("we",),
    params={"emailNote": [[], list], "toT": ["English", str]},
    kind=registry.KIND_SYNC,
)
def email(emailNote=None, toT="English"):
    emailNote = emailNote or []
    return ChatController().write_email(emailNote, toT)
