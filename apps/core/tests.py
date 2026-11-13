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

    def test_execute_select_rejects_write_statements(self):
        from unittest.mock import patch

        with patch.object(self.t, "_api_post") as post:
            for bad in (
                "UPDATE master_categories SET name='x'",
                "DROP TABLE master_categories",
                "SELECT 1; SELECT 2",
                "SELECT id FROM master_categories -- comment",
            ):
                data = json.loads(self.t.execute_select_query.invoke({"query": bad}))
                self.assertFalse(data["ok"], bad)
            post.assert_not_called()

    def test_execute_select_rejects_unknown_table(self):
        from unittest.mock import patch

        with patch.object(self.t, "_api_post") as post:
            data = json.loads(
                self.t.execute_select_query.invoke({"query": "SELECT * FROM users"})
            )
        self.assertFalse(data["ok"])
        self.assertIn("users", data["error"])
        self.assertIn("master_categories", data["hint"])
        post.assert_not_called()

    def test_execute_select_appends_limit_and_truncates(self):
        from unittest.mock import patch

        rows = [{"id": i, "name": f"n{i}"} for i in range(5)]

        def fake_post(path, payload):
            return {"data": {"rowCount": 5, "rows": rows}}

        with patch.object(self.t, "_api_post", side_effect=fake_post) as post:
            data = json.loads(
                self.t.execute_select_query.invoke(
                    {"query": "SELECT * FROM master_categories", "limit": 2}
                )
            )
        self.assertTrue(data["ok"])
        self.assertIn("LIMIT 2", post.call_args[0][1]["query"])
        self.assertTrue(data["truncated"])
        self.assertEqual(data["returned"], 2)
        self.assertEqual(len(data["rows"]), 2)

    def test_execute_select_retries_after_401(self):
        from unittest.mock import patch

        class _Resp:
            def __init__(self, status, body):
                self.status_code = status
                self.text = "" if status == 200 else body
                self._body = body

            def json(self):
                return self._body

        calls = []

        def fake_post(url, json=None, headers=None, timeout=None):
            calls.append(1)
            if len(calls) == 1:
                return _Resp(401, "unauthorized")
            return _Resp(200, {"data": {"rowCount": 1, "rows": [{"id": "c1"}]}})

        class _Session:
            def post(self, url, json=None, headers=None, timeout=None):
                return fake_post(url)

        with patch.object(self.t, "_get_session", return_value=_Session()), patch.object(
            self.t, "_service_headers", return_value={"Authorization": "Bearer x"}
        ):
            data = json.loads(
                self.t.execute_select_query.invoke(
                    {"query": "SELECT id FROM master_categories LIMIT 1"}
                )
            )
        self.assertTrue(data["ok"])
        self.assertEqual(len(calls), 2)


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


class NodeServiceAccountTests(TestCase):
    """Tests for the Node JWT client (no network)."""

    @staticmethod
    def _make_token(exp_offset: int = 3600) -> str:
        import base64
        import time

        def enc(obj):
            return (
                base64.urlsafe_b64encode(json.dumps(obj).encode())
                .decode()
                .rstrip("=")
            )

        header = enc({"alg": "HS256", "typ": "JWT"})
        payload = enc({"exp": int(time.time()) + exp_offset})
        return f"{header}.{payload}.sig"

    def _fake_session(self, token, body_override=None):
        from unittest.mock import MagicMock

        session = MagicMock()
        body = body_override or {
            "status": "success",
            "email": "ai@example.com",
            "role": "developer",
            "token": token,
        }
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = body
        resp.text = ""
        session.post.return_value = resp
        return session

    def test_token_cached_and_refreshed_on_demand(self):
        from unittest.mock import patch

        from apps.core.models import NodeServiceAccount
        from apps.core.services import node_auth

        NodeServiceAccount.objects.create(
            name="ai-agent", email="ai@example.com", password="pw", is_active=True
        )
        session = self._fake_session(self._make_token(3600))
        node_auth._cache.clear()
        with patch.object(node_auth, "_get_session", return_value=session):
            first = node_auth.get_service_token()
            second = node_auth.get_service_token()
            # A fresh login on force_refresh should mint a *different* token.
            session.post.return_value.json.return_value["token"] = self._make_token(7200)
            refreshed = node_auth.get_service_token(force_refresh=True)

        self.assertEqual(first, second)
        self.assertNotEqual(first, refreshed)
        self.assertEqual(session.post.call_count, 2)

        acct = NodeServiceAccount.objects.get(name="ai-agent")
        self.assertEqual(acct.role, "developer")
        self.assertEqual(acct.token, refreshed)

    def test_env_fallback_when_no_row(self):
        from unittest.mock import patch

        from apps.core.services import node_auth

        session = self._fake_session(self._make_token(3600))
        node_auth._cache.clear()
        with patch.object(
            node_auth.config, "get",
            side_effect=lambda key, default=None: (
                {"MASTER_API_EMAIL": "env@example.com", "MASTER_API_PASSWORD": "pw"}.get(key, default)
            ),
        ), patch.object(node_auth, "_get_session", return_value=session):
            token = node_auth.get_service_token()

        self.assertIsNotNone(token)
        self.assertEqual(session.post.call_count, 1)

    def test_rejected_credentials_raise_node_auth_error(self):
        from unittest.mock import MagicMock, patch

        from apps.core.models import NodeServiceAccount
        from apps.core.services import node_auth

        NodeServiceAccount.objects.create(
            name="ai-agent", email="ai@example.com", password="bad", is_active=True
        )
        session = MagicMock()
        resp = MagicMock()
        resp.status_code = 404
        resp.text = '{"status": "failed", "msg": "Wrong user / wrong password"}'
        session.post.return_value = resp
        node_auth._cache.clear()
        with patch.object(node_auth, "_get_session", return_value=session):
            with self.assertRaises(node_auth.NodeAuthError) as ctx:
                node_auth.get_service_token()
        self.assertIn("NODE_AUTH_REJECTED", str(ctx.exception))
        self.assertTrue(ctx.exception.hint)



