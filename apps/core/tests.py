"""Tests for the core service layer and the HTTP API (vertical slice)."""

import json

from django.test import TestCase
from django.urls import reverse

from apps.core.services.price_service import PriceController


class PriceServiceTests(TestCase):
    def test_price_range_matches_original_formula(self):
        service = PriceController()
        data = service.price_range(
            qtyString="10,100", cost=50.0, profit_max=60.0, profit_min=30.0
        )
        self.assertEqual(set(data.keys()), {10, 100})
        # cost * (1 + percent/100) / Exchange.ToUSD['RMB']  (RMB = 6.87)
        self.assertAlmostEqual(data[10], 50 * 1.6 / 6.87, places=6)
        self.assertAlmostEqual(data[100], 50 * 1.3 / 6.87, places=6)


class CommandApiTests(TestCase):
    def test_list_commands_lists_price(self):
        resp = self.client.get(reverse("command-list"))
        self.assertEqual(resp.status_code, 200)
        self.assertIn("price", resp.json()["commands"])

    def test_run_price_command(self):
        resp = self.client.post(
            reverse("command-run", args=["price"]),
            data={
                "cost": 50,
                "profit_max": 60,
                "profit_min": 30,
                "qtyString": "10,100",
            },
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["command"], "price")
        self.assertIn("10", body["result"])
        self.assertAlmostEqual(float(body["result"]["10"]), 50 * 1.6 / 6.87, places=6)

    def test_run_unknown_command_returns_404(self):
        resp = self.client.post(
            reverse("command-run", args=["nope"]),
            data={},
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 404)


class ProductMasterToolTests(TestCase):
    """Offline tests for the product-master tools (no network, no LLM)."""

    def setUp(self):
        from apps.core.services.AI.Tools import product_master_data_retrieve as t

        self.t = t
        self._original_rows = t._rows
        self.fixtures = {
            "master_categories": [
                {"id": "c1", "name": "Pet Playpens", "parent_id": None},
                {"id": "c2", "name": "Cat Litter", "parent_id": None},
            ],
            "master_product_attributes": [
                {"id": "a1", "name": "material", "label": "Material"},
                {"id": "a2", "name": "color", "label": "Color"},
                {"id": "a3", "name": "size", "label": "Size"},
            ],
            "master_product_category_attribute_assign": [
                {"id": "x1", "category_id": "c1", "attribute_id": "a1"},
            ],
        }

        def fake_rows(table_name):
            return list(self.fixtures.get(table_name, []))

        t._rows = fake_rows
        self.addCleanup(lambda: setattr(t, "_rows", self._original_rows))

    def test_build_sql_skips_already_assigned(self):
        result = self.t.build_category_attribute_assign_sql.invoke(
            {"category": "Pet Playpens", "attributes": ["material", "color"]}
        )
        data = json.loads(result)
        self.assertTrue(data["ok"])
        self.assertEqual(data["to_add"], ["color"])
        self.assertEqual(data["already_assigned"], ["material"])
        self.assertIn(
            "INSERT INTO master_product_category_attribute_assign", data["sql"]
        )
        self.assertIn("UUID()", data["sql"])
        self.assertIn("NOT EXISTS", data["sql"])

    def test_build_sql_unknown_attribute_reports_candidates(self):
        result = self.t.build_category_attribute_assign_sql.invoke(
            {"category": "Pet Playpens", "attributes": ["colr"]}
        )
        data = json.loads(result)
        # Nothing resolved -> tool refuses to emit SQL and reports the miss.
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "no attributes resolved")
        self.assertTrue(any(u["attribute"] == "colr" for u in data["unknown"]))

    def test_build_sql_missing_category_fails(self):
        result = self.t.build_category_attribute_assign_sql.invoke(
            {"category": "Nope", "attributes": ["material"]}
        )
        data = json.loads(result)
        self.assertFalse(data["ok"])
        self.assertIn("category not found", data["error"])

    def test_read_only_guard_drops_delete(self):
        from apps.core.services.AI.Agents import manager_product as p

        plan = {
            "summary": "s",
            "plan": ["a"],
            "sql": [
                {"purpose": "select", "statement": "SELECT 1;", "verification": ""},
                {
                    "purpose": "drop",
                    "statement": "DROP TABLE master_categories;",
                    "verification": "",
                },
                {
                    "purpose": "update",
                    "statement": "UPDATE master_categories SET name='x';",
                    "verification": "",
                },
            ],
            "missing_data": [],
            "risks": [],
        }
        cleaned = p.assert_plan_read_only(plan)
        statements = [step["statement"] for step in cleaned["sql"]]
        self.assertEqual(statements, ["SELECT 1;"])
        self.assertTrue(any("DROP" in r for r in cleaned["risks"]))
        self.assertTrue(any("UPDATE" in r for r in cleaned["risks"]))


class ProductMasterAgentStreamTests(TestCase):
    """Offline tests for the agent stream() event generator (no LLM/network)."""

    def test_stream_emits_tokens_status_and_guarded_plan(self):
        from unittest.mock import patch

        from apps.core.services.AI.Agents import manager_product as p

        class _Chunk:
            def __init__(self, content="", tool_call_chunks=None):
                self.content = content
                self.tool_call_chunks = tool_call_chunks or []

        class _Msg:
            def __init__(self, type_="ai", content=""):
                self.type = type_
                self.content = content

        final_state = {
            "messages": [
                _Msg("human", "Task"),
                _Msg(
                    "ai",
                    json.dumps(
                        {
                            "summary": "done",
                            "plan": ["step"],
                            "sql": [{"statement": "DROP TABLE master_categories;"}],
                            "risks": [],
                        }
                    ),
                ),
            ]
        }

        class _FakeAgent:
            def stream(self, inp, config=None, stream_mode=None):
                yield (
                    "messages",
                    (_Chunk(content="reading "), {"langgraph_node": "model"}),
                )
                yield (
                    "messages",
                    (
                        _Chunk(tool_call_chunks=[{"name": "get_master_table_schema"}]),
                        {"langgraph_node": "model"},
                    ),
                )
                yield (
                    "messages",
                    (_Chunk(content="schema"), {"langgraph_node": "model"}),
                )
                yield ("values", final_state)

        agent = p.ProductMasterAgent()
        with patch.object(agent, "_get_agent", return_value=_FakeAgent()):
            events = list(agent.stream("do it"))

        tokens = [e["text"] for e in events if e["type"] == "token"]
        statuses = [e["text"] for e in events if e["type"] == "status"]
        plan_events = [e for e in events if e["type"] == "plan"]

        self.assertEqual(tokens, ["reading ", "schema"])
        self.assertTrue(any("get_master_table_schema" in s for s in statuses))
        self.assertEqual(len(plan_events), 1)

        final = plan_events[0]["plan"]
        self.assertEqual(final["summary"], "done")
        self.assertEqual(final["sql"], [])  # DROP was removed by the guardrail
        self.assertTrue(any("DROP" in r for r in final["risks"]))

    def test_stream_emits_heartbeat_when_agent_is_idle(self):
        import time as _time

        from unittest.mock import patch

        from apps.core.services.AI.Agents import manager_product as p

        class _Chunk:
            def __init__(self, content=""):
                self.content = content
                self.tool_call_chunks = []

        class _SlowAgent:
            def stream(self, inp, config=None, stream_mode=None):
                _time.sleep(0.5)  # stall past the (patched) heartbeat interval
                yield (
                    "messages",
                    (_Chunk(content="hi"), {"langgraph_node": "model"}),
                )
                yield ("values", {"messages": []})

        agent = p.ProductMasterAgent()
        with patch.object(agent, "_get_agent", return_value=_SlowAgent()):
            with patch.object(p, "HEARTBEAT_SECONDS", 0.2):
                events = list(agent.stream("do it"))

        heartbeats = [
            e["text"]
            for e in events
            if e["type"] == "status" and "waiting for model" in e["text"]
        ]
        self.assertTrue(heartbeats)
        self.assertTrue(any(e["type"] == "plan" for e in events))


class WorkerSqlGuardrailTests(TestCase):
    def test_assert_draft_sql_keeps_select_and_allowed_insert(self):
        from apps.core.services.AI.Agents import worker_base as base

        safe, risks = base.assert_draft_sql(
            [
                {"statement": "SELECT 1;"},
                {"statement": "INSERT INTO product_sale_prices_by_qty (id) SELECT UUID();"},
                {"statement": "DROP TABLE product_sale_prices_by_qty;"},
                {"statement": "INSERT INTO master_categories (id) SELECT UUID();"},
            ],
            ["product_sale_prices_by_qty"],
        )
        statements = [s["statement"] for s in safe]
        self.assertIn("SELECT 1;", statements)
        self.assertIn("INSERT INTO product_sale_prices_by_qty", statements[1])
        self.assertEqual(len(safe), 2)
        self.assertTrue(any("DROP" in r for r in risks))
        self.assertTrue(any("INSERT outside" in r for r in risks))


class ProductPriceWorkerTests(TestCase):
    def test_prices_match_price_controller_formula(self):
        from apps.core.services.AI.Agents.worker_product_price import ProductPriceWorker
        from codes.datas import Exchange

        result = ProductPriceWorker().define_price(
            cost=50.0, qty_tiers="10,100", profit_max=60.0, profit_min=30.0,
            currency_id="c-usd", product_id="p1",
        )
        self.assertTrue(result["ok"])
        tiers = {t["min_qty"]: t["sale_price"] for t in result["data"]["tiers"]}
        rate = Exchange.ToUSD["RMB"]
        self.assertAlmostEqual(tiers[10], 50 * 1.6 / rate, places=3)
        self.assertAlmostEqual(tiers[100], 50 * 1.3 / rate, places=3)
        statements = [s["statement"] for s in result["sql"]]
        self.assertTrue(all("INSERT INTO product_sale_prices_by_qty" in s for s in statements))
        self.assertTrue(all("UUID()" in s and "NOT EXISTS" in s for s in statements))

    def test_by_variants_reports_missing_data(self):
        from apps.core.services.AI.Agents.worker_product_price import ProductPriceWorker

        result = ProductPriceWorker().define_price(
            cost=50.0, selling_by_mode="by_variants", currency_id="c-usd", product_id="p1"
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["sql"], [])
        self.assertTrue(any("by_variants" in m for m in result["missing_data"]))


class ProductNameWorkerTests(TestCase):
    def test_to_data_url_reads_local_png(self):
        import os
        import tempfile

        from apps.core.services.AI.Agents.worker_product_name import _to_data_url
        from PIL import Image

        fd, path = tempfile.mkstemp(suffix=".png")
        os.close(fd)
        try:
            Image.new("RGB", (4, 4), "red").save(path)
            url = _to_data_url(path)
            self.assertTrue(url.startswith("data:image/png;base64,"))
        finally:
            os.unlink(path)

    def test_rename_parses_seo_json_without_network(self):
        from apps.core.services.AI.Agents.worker_product_name import ProductNameWorker

        class _Resp:
            def __init__(self, content):
                self.content = content

        class _FakeLLM:
            def invoke(self, messages):
                return _Resp(
                    json.dumps({
                        "source_name_cn": "宠物围栏",
                        "plain_translation": "Pet Playpen",
                        "seo_name": "Foldable Pet Playpen For Dogs And Cats",
                        "short_name": "Pet Playpen",
                        "slug": "foldable-pet-playpen-for-dogs-and-cats",
                        "keywords": ["pet playpen", "dog fence"],
                        "detected_attributes": {"material": "polyester"},
                        "description": "A foldable pet playpen.",
                        "confidence": 0.9,
                        "missing_data": [],
                        "risks": [],
                    })
                )

        result = ProductNameWorker(llm=_FakeLLM()).rename(chinese_name="宠物围栏")
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["seo_name"], "Foldable Pet Playpen For Dogs And Cats")
        self.assertEqual(result["sql"], [])  # no product_id -> no product_names SQL


class WebResearchWorkerTests(TestCase):
    def test_research_extracts_fields_and_flags_missing(self):
        from unittest.mock import patch

        from apps.core.services.AI.Agents.worker_web_research import WebResearchWorker

        class _Resp:
            def __init__(self, content):
                self.content = content

        class _FakeLLM:
            def invoke(self, messages):
                return _Resp(json.dumps({
                    "summary": "A pet product.",
                    "fields": {"title": "Dog Bed", "price": None},
                    "quotes": ["best seller"],
                    "missing_fields": ["price"],
                }))

        worker = WebResearchWorker(llm=_FakeLLM())
        with patch(
            "apps.core.services.AI.Agents.worker_web_research.fetch_text",
            return_value="Dog bed for large breeds. " * 20,
        ):
            result = worker.research(
                urls=["http://example.com/item"], target_fields=["title", "price"]
            )
        self.assertTrue(result["ok"])
        self.assertEqual(result["fields"]["title"], "Dog Bed")
        self.assertIn("price", result["missing_fields"])


class ProductAttributesWorkerTests(TestCase):
    def test_guardrail_strips_drop_keeps_assign_insert(self):
        from apps.core.services.AI.Agents.worker_product_attributes import ProductAttributesWorker

        class _Msg:
            def __init__(self, type_, content):
                self.type = type_
                self.content = content

        final = {
            "messages": [
                _Msg(
                    "ai",
                    json.dumps({
                        "summary": "assign attributes",
                        "plan": ["assign"],
                        "sql": [
                            {"statement": "DROP TABLE master_categories;"},
                            {
                                "statement": (
                                    "INSERT INTO master_product_category_attribute_assign "
                                    "(id, category_id, attribute_id) SELECT UUID(), 'c1', 'a1' "
                                    "WHERE NOT EXISTS (SELECT 1);"
                                ),
                            },
                        ],
                        "missing_data": [],
                        "risks": [],
                    }),
                )
            ]
        }
        result = ProductAttributesWorker()._parse_result(final)
        statements = [s["statement"] for s in result["sql"]]
        self.assertTrue(any("master_product_category_attribute_assign" in s for s in statements))
        self.assertFalse(any("DROP" in s for s in statements))
        self.assertTrue(any("DROP" in r for r in result["risks"]))


class ManagerDelegationTests(TestCase):
    def test_manager_exposes_worker_tools(self):
        from apps.core.services.AI.Agents import manager_product as m

        names = {t.name for t in m.WORKER_TOOLS}
        self.assertTrue(
            {"rename_product_with_seo", "research_product_web",
             "define_product_price", "manage_product_attributes"} <= names
        )

    def test_human_message_includes_images(self):
        from apps.core.services.AI.Agents import manager_product as m

        msg = m.ProductMasterAgent()._build_human_message("do it", images=["a.png"])
        self.assertIn("a.png", msg)

    def test_collect_worker_results_aggregates_sql(self):
        from apps.core.services.AI.Agents import manager_product as m

        class _ToolMsg:
            def __init__(self, content):
                self.type = "tool"
                self.content = content

        result = {
            "messages": [
                _ToolMsg(json.dumps({
                    "worker": "product_price", "ok": True,
                    "sql": [{"purpose": "p", "statement": "INSERT INTO product_sale_prices_by_qty ...;"}],
                }))
            ]
        }
        worker_results, drafts = m.ProductMasterAgent._collect_worker_results(result)
        self.assertEqual(len(worker_results), 1)
        self.assertEqual(drafts[0]["worker"], "product_price")
        self.assertIn("product_sale_prices_by_qty", drafts[0]["statement"])


