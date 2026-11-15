import os, json

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django

django.setup()

from django.test import Client

c = Client()

# 1) missing request -> 400
r = c.post(
    "/api/agents/manager_product",
    data="{}",
    content_type="application/json",
    HTTP_HOST="127.0.0.1:8000",
)
print("missing-request status:", r.status_code)

# 2) real run
r = c.post(
    "/api/agents/manager_product",
    data=json.dumps(
        {
            "request": "Assign the madeof_material attribute to the 'Pet Playpens' category."
        }
    ),
    content_type="application/json",
    HTTP_HOST="127.0.0.1:8000",
)
print("agent status:", r.status_code)
d = json.loads(r.content)
plan = d.get("plan", {})
print("summary:", plan.get("summary"))
print("sql count:", len(plan.get("sql", [])))
print("risks count:", len(plan.get("risks", [])))
print("has text:", bool(d.get("text")))

# --------------------------------------------------------------------------- #
# Regression checks: SQL-builder "no zero-attribute categories" bug.
# --------------------------------------------------------------------------- #
from apps.core.services.AI.Agents.worker_product_sql_builder import (
    ProductSqlBuilderWorker,
    _normalise_tables,
    _normalise_execute,
)

# 3) tables / execute argument coercion (manager emits a comma string -> must not
#    raise a Pydantic ValidationError).
assert _normalise_tables(
    "master_categories, master_product_category_attribute_assign"
) == ["master_categories", "master_product_category_attribute_assign"]
assert _normalise_tables(["a", " b "]) == ["a", "b"]
assert _normalise_tables(None) is None
assert _normalise_execute("false") is False
assert _normalise_execute("1") is True
assert _normalise_execute(True) is True
print("tables coercion: OK")

w = ProductSqlBuilderWorker()

# 4) A fabricated "answer: none" with NO tool calls must never be published as a
#    confident result: the worker must auto-execute the drafted SQL (or fail closed).
fake = {
    "messages": [
        type(
            "M",
            (),
            {
                "type": "ai",
                "content": (
                    '{"answer": "none", "summary": "No product categories have zero '
                    'assigned attributes.", "sql": [{"statement": "SELECT 1"}]}'
                ),
            },
        )()
    ]
}
parsed = w._parse_result(fake, execute=True)
print(
    "fabrication guard (parse) -> ok:", parsed.get("ok"),
    "executed:", parsed.get("executed"),
    "execute_requested:", parsed.get("execute_requested"),
)
assert parsed.get("executed") is False, parsed
assert parsed.get("execute_requested") is True, parsed
guarded = w._ensure_executed(parsed)
print(
    "fabrication guard (exec) -> ok:", guarded.get("ok"),
    "executed:", guarded.get("executed"),
    "answer:", guarded.get("answer"),
)
assert guarded.get("executed") is True, guarded
assert guarded.get("answer") != "none", guarded
print("no-fabrication post-condition: OK")

# 5) End-to-end: the worker must actually execute and return the 62 zero-attribute
#    categories rather than fabricating "none".
sql_plan = w.plan("Please help me review which of product category has no attribute yet.")
print("worker executed:", sql_plan.get("executed"), "ok:", sql_plan.get("ok"))
print("worker rowCount:", [q.get("rowCount") for q in sql_plan.get("queries", [])])
print("worker answer (first 160):", (sql_plan.get("answer") or "")[:160])
assert sql_plan.get("executed") is True, sql_plan
assert sql_plan.get("ok") is True, sql_plan
row_count = sql_plan.get("queries", [{}])[0].get("rowCount")
print("EXPECT 62 rows; got", row_count)
assert row_count == 62, sql_plan.get("queries")
print("62-row end-to-end check: OK")

# 6) Manager rebuilds an answer from executed rows when the model replies with no JSON.
from apps.core.services.AI.Agents.manager_product import ProductMasterAgent

agent = ProductMasterAgent(mode="plan")
fake_manager = {
    "messages": [
        type(
            "M",
            (),
            {
                "type": "tool",
                "content": (
                    '{"ok": true, "query": "SELECT 1", "rowCount": 62, '
                    '"rows": [{"name": "Baby Crib"}]}'
                ),
            },
        )(),
        type("M", (), {"type": "ai", "content": ""})(),
    ]
}
p = agent._parse_result(fake_manager)
print("manager rebuilt answer:", p.get("answer"))
assert "62" in p.get("answer", ""), p
print("manager empty-reply synthesis: OK")
