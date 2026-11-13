"""Worker that turns a web-research summary + cost into a price and SQL.

Prices are computed deterministically with the same formula as
``PriceController.price_range`` (cost * (1 + profit%) / exchange rate), never
invented by the model.  The draft ``INSERT`` statements target
``product_sale_prices_by_qty`` (or ``product_costs``) and are never executed.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from langchain_core.tools import tool

from apps.core import config
from . import worker_base as base
from apps.core.services.AI.Tools.utils import (
    MasterApiError,
    _rows,
    _sql_literal,
    extract_table_fields,
)
from codes.datas import Exchange

DEFAULT_EXCHANGE_RATE = float(
    config.get("AI_PRICE_EXCHANGE_RATE", str(Exchange.ToUSD.get("RMB", 6.87)))
)
PRICE_TABLES = ("product_sale_prices_by_qty", "product_costs")


def _qty_insert(product_id: str, currency_id: str, qty: int, price: float) -> Dict[str, str]:
    pid = _sql_literal(product_id)
    cid = _sql_literal(currency_id)
    statement = (
        "INSERT INTO product_sale_prices_by_qty (id, product_id, min_order_qty, currency_id, sale_price)\n"
        "SELECT UUID(), " + pid + ", " + str(qty) + ", " + cid + ", " + str(price) + "\n"
        "WHERE NOT EXISTS (\n"
        "  SELECT 1 FROM product_sale_prices_by_qty\n"
        "  WHERE product_id = " + pid + " AND min_order_qty = " + str(qty) + "\n"
        ");"
    )
    verification = (
        "SELECT min_order_qty, sale_price FROM product_sale_prices_by_qty\n"
        "WHERE product_id = " + pid + " AND min_order_qty = " + str(qty) + ";"
    )
    return {"purpose": f"set sale price for min order qty {qty}",
            "statement": statement, "verification": verification}


class ProductPriceWorker:
    """Define a product price and draft SQL from cost + research inputs."""

    def __init__(self, model: Optional[str] = None, llm: Any = None):
        self.model = model or base.DEFAULT_MODEL
        self.llm = llm or base.build_llm(self.model)

    def define_price(
        self,
        cost: Any,
        qty_tiers: str = "10,100,500",
        profit_max: float = 60.0,
        profit_min: float = 30.0,
        currency: str = "USD",
        exchange_rate: Optional[float] = None,
        selling_by_mode: str = "by_qty",
        currency_id: Optional[str] = None,
        product_id: Optional[str] = None,
        variant_ids: Optional[List[str]] = None,
        research_summary: Optional[str] = None,
    ) -> Dict[str, Any]:
        try:
            cost = float(cost)
        except (TypeError, ValueError):
            return {"worker": "product_price", "ok": False, "error": f"invalid cost: {cost!r}"}

        qtys = [int(q.strip()) for q in str(qty_tiers).split(",") if q.strip()]
        if not qtys:
            return {"worker": "product_price", "ok": False, "error": "no quantity tiers"}

        profit_max = float(profit_max)
        profit_min = float(profit_min)
        step = (profit_max - profit_min) / (len(qtys) - 1) if len(qtys) > 1 else 0.0
        rate = float(exchange_rate) if exchange_rate else DEFAULT_EXCHANGE_RATE

        tiers: List[Dict[str, Any]] = []
        for i, qty in enumerate(qtys):
            pct = profit_max - i * step
            price = cost * (1 + pct / 100.0) / rate
            tiers.append({"min_qty": qty, "sale_price": round(price, 3),
                          "profit_pct": round(pct, 2)})

        missing: List[str] = []
        if not currency_id:
            try:
                rows = _rows("master_currencies")
                match = next(
                    (r for r in rows if str(r.get("code", "")).upper() == currency.upper()), None
                )
                currency_id = match.get("id") if match else None
            except MasterApiError:
                currency_id = None
            if not currency_id:
                missing.append(f"could not resolve currency_id for {currency!r}")

        sql: List[Dict[str, str]] = []
        if selling_by_mode == "by_qty":
            if product_id and currency_id:
                for tier in tiers:
                    sql.append(_qty_insert(str(product_id), str(currency_id),
                                           int(tier["min_qty"]), tier["sale_price"]))
            else:
                if not product_id:
                    missing.append("product_id required to build product_sale_prices_by_qty SQL")
                if not currency_id:
                    missing.append("currency_id required to build SQL")
        elif selling_by_mode == "by_variants":
            missing.append(
                "by_variants pricing needs size/color/capacity variant ids (not yet supported)"
            )
        else:
            missing.append(f"unknown selling_by_mode {selling_by_mode!r}")

        safe_sql, risks = base.assert_draft_sql(sql, PRICE_TABLES)

        schema: Dict[str, Any] = {}
        try:
            schema = extract_table_fields("product_sale_prices_by_qty") or {}
        except Exception:  # noqa: BLE001 - schema snapshot is informational only
            pass

        return {
            "worker": "product_price",
            "ok": True,
            "summary": f"{len(tiers)} tier(s) in {currency} at rate {rate}",
            "data": {
                "currency": currency,
                "exchange_rate": rate,
                "cost": cost,
                "tiers": tiers,
                "schema": schema,
                "research_summary": research_summary,
            },
            "sql": safe_sql,
            "missing_data": missing,
            "risks": risks,
        }


@tool
def define_product_price(
    cost: float,
    qty_tiers: str = "10,100,500",
    profit_max: float = 60.0,
    profit_min: float = 30.0,
    currency: str = "USD",
    exchange_rate: Optional[float] = None,
    selling_by_mode: str = "by_qty",
    currency_id: Optional[str] = None,
    product_id: Optional[str] = None,
    research_summary: Optional[str] = None,
) -> str:
    """Define a product price (RMB cost -> target currency) and return draft SQL (never executed) plus the computed tier prices."""
    try:
        result = ProductPriceWorker().define_price(
            cost=cost, qty_tiers=qty_tiers, profit_max=profit_max, profit_min=profit_min,
            currency=currency, exchange_rate=exchange_rate,
            selling_by_mode=selling_by_mode, currency_id=currency_id,
            product_id=product_id, research_summary=research_summary,
        )
    except Exception as exc:  # noqa: BLE001
        result = {"worker": "product_price", "ok": False, "error": str(exc)}
    return base.dumps(result)


