"""Tests for the core service layer and the HTTP API (vertical slice)."""

import json
import os

from django.test import TestCase
from django.urls import reverse

from apps.core.services.price_service import PriceController

# Keep the suite hermetic: never POST traces to LangSmith even when a developer
# has LANGSMITH_API_KEY in .env.  core.tracing.configure_tracing() never
# overwrites an existing LANGSMITH_TRACING value, so this wins over .env.
os.environ["LANGSMITH_TRACING"] = "false"


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
        from apps.core.services.AI.Agents.core import plan_tools as t
        from apps.core.services.AI.Agents.core import master_api, schema_catalog
        from apps.core.services.AI.Agents.domains.product.attributes import tools as attr_tools

        self.t = t
        self.attr_tools = attr_tools
        self.utils = master_api
        self._original_rows = master_api._rows
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

        master_api._rows = fake_rows
        self.addCleanup(lambda: setattr(master_api, "_rows", self._original_rows))

    def test_build_sql_skips_already_assigned(self):
        result = self.attr_tools.build_category_attribute_assign_sql.invoke(
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
        result = self.attr_tools.build_category_attribute_assign_sql.invoke(
            {"category": "Pet Playpens", "attributes": ["colr"]}
        )
        data = json.loads(result)
        # Nothing resolved -> tool refuses to emit SQL and reports the miss.
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "no attributes resolved")
        self.assertTrue(any(u["attribute"] == "colr" for u in data["unknown"]))

    def test_build_sql_missing_category_fails(self):
        result = self.attr_tools.build_category_attribute_assign_sql.invoke(
            {"category": "Nope", "attributes": ["material"]}
        )
        data = json.loads(result)
        self.assertFalse(data["ok"])
        self.assertIn("category not found", data["error"])

    def test_plan_guard_keeps_update_removes_delete_and_ddl(self):
        from apps.core.services.AI.Agents.core.engine import assert_plan_read_only

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
                    "statement": "UPDATE master_categories SET name='x' WHERE id='c1';",
                    "verification": "",
                },
                {
                    "purpose": "delete",
                    "statement": "DELETE FROM master_categories WHERE id='c1';",
                    "verification": "",
                },
            ],
            "missing_data": [],
            "risks": [],
        }
        cleaned = assert_plan_read_only(plan)
        statements = [step["statement"] for step in cleaned["sql"]]
        self.assertIn("SELECT 1;", statements)
        # UPDATE is kept for review in plan mode; DELETE and DDL are removed.
        self.assertTrue(any("UPDATE master_categories" in s for s in statements))
        self.assertFalse(any("DELETE" in s for s in statements))
        self.assertTrue(any("DROP" in r for r in cleaned["risks"]))
        self.assertTrue(any("DELETE" in r for r in cleaned["risks"]))
        skipped_verbs = {s["verb"] for s in cleaned.get("skipped_sql", [])}
        self.assertIn("DROP", skipped_verbs)
        self.assertIn("DELETE", skipped_verbs)

    def test_execute_select_rejects_write_statements(self):
        from unittest.mock import patch

        with patch.object(self.utils, "_api_post") as post:
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

        with patch.object(self.utils, "_api_post") as post:
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

        with patch.object(self.utils, "_api_post", side_effect=fake_post) as post:
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

        with patch.object(self.utils, "_get_session", return_value=_Session()), patch.object(
            self.utils, "_service_headers", return_value={"Authorization": "Bearer x"}
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

        from apps.core.services.AI.Agents.domains.product import manager as p

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

        from apps.core.services.AI.Agents.domains.product import manager as p
        from apps.core.services.AI.Agents.core import engine as g

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
            with patch.object(g, "HEARTBEAT_SECONDS", 0.2):
                events = list(agent.stream("do it"))

        heartbeats = [
            e["text"]
            for e in events
            if e["type"] == "status" and "waiting for model" in e["text"]
        ]
        self.assertTrue(heartbeats)
        self.assertTrue(any(e["type"] == "plan" for e in events))

    def test_stream_replays_history_for_follow_up_turns(self):
        from unittest.mock import patch

        from apps.core.services.AI.Agents.domains.product import manager as p

        class _Msg:
            def __init__(self, type_="ai", content=""):
                self.type = type_
                self.content = content

        seen = []

        class _RecordingAgent:
            def stream(self, inp, config=None, stream_mode=None):
                messages = list(inp["messages"])
                seen.append(messages)
                final = messages + [_Msg("ai", json.dumps({"summary": "ok"}))]
                yield ("values", {"messages": final})

        agent = p.ProductMasterAgent()
        with patch.object(agent, "_get_agent", return_value=_RecordingAgent()):
            list(agent.stream("first question"))
            list(agent.stream("second question"))

        # Turn one sees only the new human message.
        self.assertEqual(len(seen[0]), 1)
        # Turn two replays the first turn before the new human message, so the
        # model keeps the conversation context.
        self.assertEqual(len(seen[1]), 3)
        self.assertEqual(seen[1][0][0], "human")
        self.assertEqual(seen[1][1].type, "ai")
        self.assertEqual(seen[1][2][0], "human")
        self.assertIn("second question", seen[1][2][1])

        agent.reset()
        self.assertEqual(agent.messages, [])


class ProductMasterCommandTests(TestCase):
    """The -mp / -mg commands are REPLs that keep the conversation until /q."""

    def test_mp_loop_keeps_conversation_until_quit(self):
        import builtins
        from unittest.mock import patch

        from codes.CommandChecker import CommandChecker
        from apps.core.services.AI.Agents.core import router as g

        tasks_seen = []
        compacted = []
        resets = []

        class _FakeAgent:
            def __init__(self, pinned=None, **kwargs):
                self.pinned = pinned
                self.last_plan = None

            def reset(self):
                resets.append(True)

            def compact(self, force=True):
                compacted.append(force)
                return {"compacted": True, "before": 4, "after": 2, "words": 10}

            def stream(self, task, domain=None):
                tasks_seen.append(task)
                plan = {"summary": task, "plan": [], "sql": [], "risks": []}
                yield {"type": "plan", "plan": plan, "text": "Summary: " + task}

            def act(self, plan, dry_run=True):
                return plan, []

        # Bypass __init__: it wires up browser / ollama controllers we do not need.
        checker = object.__new__(CommandChecker)
        checker.COMMAND_CHECKED = "CHECKED"
        checker.COMMAND_HIT = False
        checker.ans = "-mp"

        scripted = iter(
            ["first question", "", "/help", "/compact", "second question", "/new", "/q", "never"]
        )

        def fake_input(prompt=""):
            return next(scripted)

        with patch.object(g, "GeneralManagerAgent", _FakeAgent), patch.object(
            builtins, "input", fake_input
        ), patch.object(builtins, "print"):
            result = checker.product_master()

        self.assertEqual(result, "CHECKED")
        # Both questions ran in the same session; input after /q was not read.
        self.assertEqual(tasks_seen, ["first question", "second question"])
        # /compact and /new were handled by the loop.
        self.assertEqual(compacted, [True])
        self.assertEqual(len(resets), 1)


class AgentSqlGuardrailTests(TestCase):
    def test_assert_draft_sql_keeps_select_and_allowed_insert(self):
        from apps.core.services.AI.Agents.core import guardrail as base

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


class ProductNameAgentTests(TestCase):
    def test_to_data_url_reads_local_png(self):
        import os
        import tempfile

        from apps.core.services.AI.Agents.domains.product.naming.tools import _to_data_url
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
        from langchain_core.messages import AIMessage

        from apps.core.services.AI.Agents.domains.product.naming.manager import ProductNameAgent

        class _FakeLLM:
            def invoke(self, messages):
                return AIMessage(
                    content=json.dumps({
                        "source_name_cn": "宠物?��?",
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

        result = ProductNameAgent(llm=_FakeLLM()).rename(chinese_name="宠物?��?")
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["seo_name"], "Foldable Pet Playpen For Dogs And Cats")
        self.assertEqual(result["sql"], [])  # no product_id -> no product_names SQL


class WebResearchAgentTests(TestCase):
    def test_research_extracts_fields_and_flags_missing(self):
        from unittest.mock import patch

        from apps.core.services.AI.Agents.domains.product.web_research.manager import WebResearchAgent

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

        worker = WebResearchAgent(llm=_FakeLLM())
        with patch(
            "apps.core.services.AI.Agents.domains.product.web_research.tools.fetch_text",
            return_value="Dog bed for large breeds. " * 20,
        ):
            result = worker.research(
                urls=["http://example.com/item"], target_fields=["title", "price"]
            )
        self.assertTrue(result["ok"])
        self.assertEqual(result["fields"]["title"], "Dog Bed")
        self.assertIn("price", result["missing_fields"])


class ProductAttributesAgentTests(TestCase):
    def test_guardrail_strips_drop_keeps_assign_insert(self):
        from apps.core.services.AI.Agents.domains.product.attributes.manager import ProductAttributesAgent

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
        result = ProductAttributesAgent()._parse_result(final)
        statements = [s["statement"] for s in result["sql"]]
        self.assertTrue(any("master_product_category_attribute_assign" in s for s in statements))
        self.assertFalse(any("DROP" in s for s in statements))
        self.assertTrue(any("DROP" in r for r in result["risks"]))


class ManagerDelegationTests(TestCase):
    def test_manager_exposes_subagent_tools(self):
        from apps.core.services.AI.Agents.domains.product import manager as m

        names = {t.name for t in m.PLAN_TOOLS}
        self.assertTrue(
            {"rename_product_with_seo", "research_product_web",
             "manage_product_attributes"} <= names
        )

    def test_manager_does_not_expose_attribute_builders(self):
        from apps.core.services.AI.Agents.domains.product import manager as m
        from apps.core.services.AI.Agents.domains.product.attributes.tools import (
            ATTRIBUTE_TOOLS,
        )

        attribute_builders = {
            "build_category_attribute_assign_sql",
            "build_create_attribute_sql",
            "build_attribute_dropdown_sql",
        }
        manager_names = {t.name for t in m.PLAN_TOOLS}
        agent_names = {t.name for t in ATTRIBUTE_TOOLS}

        self.assertFalse(attribute_builders & manager_names)
        self.assertIn("manage_product_attributes", manager_names)
        self.assertTrue(attribute_builders <= agent_names)

    def test_human_message_includes_images(self):
        from apps.core.services.AI.Agents.domains.product import manager as m

        msg = m.ProductMasterAgent()._build_human_message("do it", images=["a.png"])
        self.assertIn("a.png", msg)

    def test_collect_agent_results_aggregates_sql(self):
        from apps.core.services.AI.Agents.domains.product import manager as m

        class _ToolMsg:
            def __init__(self, content):
                self.type = "tool"
                self.content = content

        result = {
            "messages": [
                _ToolMsg(json.dumps({
                    "agent": "product_sql_builder", "ok": True,
                    "sql": [{"purpose": "p", "statement": "INSERT INTO product_sale_prices_by_qty ...;"}],
                }))
            ]
        }
        agent_results, drafts = m.ProductMasterAgent._collect_agent_results(result)
        self.assertEqual(len(agent_results), 1)
        self.assertEqual(drafts[0]["agent"], "product_sql_builder")
        self.assertIn("product_sale_prices_by_qty", drafts[0]["statement"])


class DomainManagerGraphTests(TestCase):
    """Offline tests for the explicit LangGraph tool-calling loop (no LLM/network)."""

    def test_graph_has_model_and_tools_nodes(self):
        from apps.core.services.AI.Agents.domains.product import manager as m

        agent = m.ProductMasterAgent()
        graph = agent._get_agent()
        nodes = set(graph.get_graph().nodes.keys())
        self.assertIn("model", nodes)
        self.assertIn("tools", nodes)

    def test_system_prompt_prepended_not_stored(self):
        from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

        from apps.core.services.AI.Agents.domains.product import manager as m
        from apps.core.services.AI.Agents.domains.product.prompts import SYSTEM_PROMPT

        captured = []

        class _Bound:
            def invoke(self, messages):
                captured.append(list(messages))
                return AIMessage(content='{"summary": "ok"}')

        class _FakeLLM:
            def bind_tools(self, tools):
                return _Bound()

        agent = m.ProductMasterAgent()
        agent.llm = _FakeLLM()
        agent._agent = None
        graph = agent._get_agent()

        state = graph.invoke({"messages": [HumanMessage(content="hi")]})

        self.assertTrue(captured)
        self.assertIsInstance(captured[0][0], SystemMessage)
        self.assertIn(SYSTEM_PROMPT, captured[0][0].content)
        # The system prompt is injected per call, never written into state.
        self.assertFalse(
            any(isinstance(msg, SystemMessage) for msg in state["messages"])
        )

    def test_tool_call_loops_through_tool_node(self):
        from langchain_core.messages import AIMessage, HumanMessage
        from langchain_core.tools import tool

        from apps.core.services.AI.Agents.core.graph import build_tool_graph

        calls = []

        @tool
        def echo(text: str) -> str:
            """Echo the given text."""
            calls.append(text)
            return text

        class _Bound:
            def __init__(self):
                self.step = 0

            def invoke(self, messages):
                self.step += 1
                if self.step == 1:
                    return AIMessage(
                        content="",
                        tool_calls=[
                            {"name": "echo", "args": {"text": "ping"}, "id": "1"}
                        ],
                    )
                return AIMessage(content='{"summary": "done"}')

        class _FakeLLM:
            def bind_tools(self, tools):
                return _Bound()

        graph = build_tool_graph(_FakeLLM(), [echo])

        state = graph.invoke({"messages": [HumanMessage(content="hi")]})
        types = [type(m).__name__ for m in state["messages"]]

        self.assertEqual(calls, ["ping"])
        self.assertEqual(
            types, ["HumanMessage", "AIMessage", "ToolMessage", "AIMessage"]
        )

    def test_mode_switch_rebuilds_graph(self):
        from apps.core.services.AI.Agents.domains.product import manager as m

        agent = m.ProductMasterAgent(mode="plan")
        first = agent._get_agent()
        agent.mode = "act"
        second = agent._get_agent()
        self.assertIsNot(first, second)


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
            first_name="Ai",
            last_name="Agent",
            email="ai@example.com",
            password="pw",
            is_active=True,
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

        acct = NodeServiceAccount.objects.get(email="ai@example.com")
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
            first_name="Ai",
            last_name="Agent",
            email="ai@example.com",
            password="bad",
            is_active=True,
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


    def test_load_account_uses_first_and_last_name(self):
        from apps.core.models import NodeServiceAccount
        from apps.core.services import node_auth

        NodeServiceAccount.objects.create(
            first_name="Ai",
            last_name="Agent",
            email="ai@example.com",
            password="pw",
            is_active=True,
        )

        account = node_auth._load_account()
        self.assertEqual(account["email"], "ai@example.com")
        self.assertEqual(account["password"], "pw")
        self.assertEqual(account["name"], "Ai Agent")

    def test_load_account_falls_back_to_email_when_names_blank(self):
        from apps.core.models import NodeServiceAccount
        from apps.core.services import node_auth

        NodeServiceAccount.objects.create(
            first_name="",
            last_name="",
            email="ai@example.com",
            password="pw",
            is_active=True,
        )

        self.assertEqual(node_auth._load_account()["name"], "ai@example.com")

    def test_ensure_django_is_idempotent(self):
        from apps.core.services import node_auth

        # Under the test runner apps are already populated, so this must be a
        # cheap no-op that reports success every time.
        self.assertTrue(node_auth._ensure_django())
        self.assertTrue(node_auth._ensure_django())

    def test_ensure_django_bootstraps_standalone_process(self):
        import os
        import subprocess
        import sys

        from apps.core import config as core_config

        # Simulate the CLI: a fresh interpreter with no DJANGO_SETTINGS_MODULE,
        # started from an arbitrary cwd.  node_auth must bootstrap Django from
        # config.BASE_DIR before touching the ORM.
        code = (
            "import sys; sys.path.insert(0, r'%s'); "
            "from apps.core.services import node_auth; "
            "print(node_auth._ensure_django()); "
            "from django.apps import apps; print(apps.ready)"
        ) % core_config.BASE_DIR
        env = os.environ.copy()
        env.pop("DJANGO_SETTINGS_MODULE", None)
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=os.path.dirname(core_config.BASE_DIR),
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.split(), ["True", "True"])


class ParseModeTests(TestCase):
    def test_parse_mode_defaults_to_plan(self):
        from apps.core.services.AI.Agents.core.router import parse_mode

        self.assertEqual(parse_mode("add material"), ("plan", "add material"))

    def test_parse_mode_act(self):
        from apps.core.services.AI.Agents.core.router import parse_mode

        self.assertEqual(parse_mode("/act add material"), ("act", "add material"))

    def test_parse_mode_plan_explicit(self):
        from apps.core.services.AI.Agents.core.router import parse_mode

        self.assertEqual(parse_mode("/plan add material"), ("plan", "add material"))

    def test_parse_mode_embedded_slash_not_triggered(self):
        from apps.core.services.AI.Agents.core.router import parse_mode

        self.assertEqual(
            parse_mode("see /act/x for docs"), ("plan", "see /act/x for docs")
        )


class ActModeTests(TestCase):
    def test_act_guard_keeps_delete_removes_ddl(self):
        from apps.core.services.AI.Agents.core.engine import assert_plan_sql

        plan = {
            "summary": "s",
            "plan": ["a"],
            "sql": [
                {"statement": "DELETE FROM master_categories WHERE id='c1';"},
                {"statement": "DROP TABLE master_categories;"},
            ],
            "missing_data": [],
            "risks": [],
        }
        cleaned = assert_plan_sql(plan, mode="act")
        statements = [s["statement"] for s in cleaned["sql"]]
        self.assertTrue(any("DELETE" in s for s in statements))
        self.assertFalse(any("DROP" in s for s in statements))

    def test_agent_mode_selects_tools(self):
        from apps.core.services.AI.Agents.domains.product import manager as m

        plan_names = {t.name for t in m.PLAN_TOOLS}
        act_names = {t.name for t in m.ACT_TOOLS}
        self.assertIn("execute_update_query", act_names)
        self.assertIn("execute_delete_query", act_names)
        self.assertNotIn("execute_update_query", plan_names)
        self.assertNotIn("execute_delete_query", plan_names)

    def test_execute_plan_batch_routes_all_statements_in_one_call(self):
        from unittest.mock import patch

        from apps.core.services.AI.Agents.core import master_api, schema_catalog
        from apps.core.services.AI.Agents.core.act_tools import execute_plan

        calls = []

        def fake_post(path, payload, *, label="query"):
            calls.append((path, payload))
            results = [
                {"index": 0, "verb": "SELECT", "rowCount": 1, "rows": [{"id": "c1"}]},
                {"index": 1, "verb": "WRITE", "affectedRows": 1, "changedRows": 0, "insertId": "x"},
                {"index": 2, "verb": "WRITE", "affectedRows": 1, "changedRows": 1, "insertId": None},
                {"index": 3, "verb": "DELETE", "affectedRows": 1, "info": ""},
            ]
            return {"data": {"results": results, "committed": True}}

        plan = {"sql": [
            {"purpose": "read", "statement": "SELECT * FROM master_categories LIMIT 1;"},
            {"purpose": "insert", "statement": "INSERT INTO master_product_attributes (id, name, label) VALUES (UUID(), 'zzz', 'Z');"},
            {"purpose": "update", "statement": "UPDATE master_categories SET name='y' WHERE id='c1';"},
            {"purpose": "delete", "statement": "DELETE FROM master_categories WHERE id='c1';"},
        ]}
        with patch.object(master_api, "_api_post", side_effect=fake_post):
            results = execute_plan(plan, plan_id="p1")

        self.assertEqual(len(results), 4)
        self.assertTrue(all(r["ok"] for r in results))
        self.assertEqual(len(calls), 1)
        path, payload = calls[0]
        self.assertEqual(path, master_api.BATCH_QUERY_PATH)
        self.assertEqual(payload["planId"], "p1")
        statements = [s["statement"] for s in payload["statements"]]
        self.assertEqual(statements[0], "SELECT * FROM master_categories LIMIT 1;")
        # UPDATE/DELETE are capped with maxRows for the Node-side check.
        self.assertIsNotNone(payload["statements"][2]["maxRows"])
        self.assertIsNotNone(payload["statements"][3]["maxRows"])

    def test_execute_plan_sequential_fallback(self):
        from unittest.mock import patch

        from apps.core.services.AI.Agents.core import master_api, schema_catalog
        from apps.core.services.AI.Agents.core import act_tools

        calls = []

        def fake_post(path, payload, *, label="query"):
            calls.append(path)
            return {"data": {"affectedRows": 1, "changedRows": 0, "insertId": "x"}}

        plan = {"sql": [
            {"purpose": "read", "statement": "SELECT * FROM master_categories LIMIT 1;"},
            {"purpose": "insert", "statement": "INSERT INTO master_product_attributes (id, name, label) VALUES (UUID(), 'zzz', 'Z');"},
            {"purpose": "update", "statement": "UPDATE master_categories SET name='y' WHERE id='c1';"},
            {"purpose": "delete", "statement": "DELETE FROM master_categories WHERE id='c1';"},
        ]}
        with patch.object(act_tools._settings, "AI_ACT_BATCH", False):
            with patch.object(master_api, "_api_post", side_effect=fake_post):
                results = act_tools.execute_plan(plan)

        self.assertEqual(len(results), 4)
        self.assertTrue(all(r["ok"] for r in results))
        self.assertEqual(calls, [
            master_api.SELECT_QUERY_PATH,
            master_api.UPDATE_QUERY_PATH,
            master_api.UPDATE_QUERY_PATH,
            master_api.DELETE_QUERY_PATH,
        ])

    def test_execute_plan_requires_confirmation_for_no_where(self):
        from unittest.mock import patch

        from apps.core.services.AI.Agents.core import master_api, schema_catalog
        from apps.core.services.AI.Agents.core.act_tools import execute_plan

        plan = {"sql": [{"statement": "UPDATE master_categories SET name='y';"}]}
        with patch.object(master_api, "_api_post") as post:
            results = execute_plan(plan)

        self.assertEqual(len(results), 1)
        self.assertFalse(results[0]["ok"])
        self.assertTrue(results[0]["needs_confirmation"])
        post.assert_not_called()


    def test_execute_plan_dry_run_batch_preview(self):
        from unittest.mock import patch

        from apps.core.services.AI.Agents.core import master_api, schema_catalog
        from apps.core.services.AI.Agents.core.act_tools import execute_plan

        def fake_post(path, payload, *, label="query"):
            return {"data": {"committed": False, "dryRun": True,
                             "results": [{"index": 0, "verb": "WRITE", "affectedRows": 2}]}}

        plan = {"sql": [{"statement": "UPDATE master_categories SET name='y' WHERE id='c1';"}]}
        with patch.object(master_api, "_api_post", side_effect=fake_post) as post:
            results = execute_plan(plan, dry_run=True)

        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["dry_run"])
        self.assertFalse(results[0]["committed"])
        self.assertEqual(post.call_args[0][0], master_api.BATCH_QUERY_PATH)

    def test_execute_plan_rejects_disallowed_table(self):
        from unittest.mock import patch

        from apps.core.services.AI.Agents.core import master_api, schema_catalog
        from apps.core.services.AI.Agents.core.act_tools import execute_plan

        plan = {"sql": [{"statement": "DELETE FROM users WHERE id=1;"}]}
        with patch.object(master_api, "_api_post") as post:
            results = execute_plan(plan)

        self.assertEqual(results, [])
        post.assert_not_called()

    def test_execute_update_query_tool(self):
        from unittest.mock import patch

        from apps.core.services.AI.Agents.core import master_api, schema_catalog
        from apps.core.services.AI.Agents.core.act_tools import execute_update_query

        def fake_post(path, payload, *, label="query"):
            return {"data": {"affectedRows": 2, "changedRows": 2, "insertId": None}}

        with patch.object(master_api, "_api_post", side_effect=fake_post) as post:
            data = json.loads(execute_update_query.invoke(
                {"query": "UPDATE master_categories SET name='y' WHERE id='c1';"}
            ))

        self.assertTrue(data["ok"])
        self.assertEqual(data["affectedRows"], 2)
        self.assertEqual(post.call_args[0][0], master_api.UPDATE_QUERY_PATH)

    def test_execute_delete_query_tool(self):
        from unittest.mock import patch

        from apps.core.services.AI.Agents.core import master_api, schema_catalog
        from apps.core.services.AI.Agents.core.act_tools import execute_delete_query

        def fake_post(path, payload, *, label="query"):
            return {"data": {"affectedRows": 1, "info": ""}}

        with patch.object(master_api, "_api_post", side_effect=fake_post) as post:
            data = json.loads(execute_delete_query.invoke(
                {"query": "DELETE FROM master_categories WHERE id='c1';"}
            ))

        self.assertTrue(data["ok"])
        self.assertEqual(post.call_args[0][0], master_api.DELETE_QUERY_PATH)


class BuildWriteSqlTests(TestCase):
    def test_build_update_sql_requires_where(self):
        from unittest.mock import patch

        from apps.core.services.AI.Agents.core import plan_tools as t
        from apps.core.services.AI.Agents.core import master_api, schema_catalog

        with patch.object(schema_catalog, "extract_table_fields", return_value={"name": {}, "id": {}}):
            data = json.loads(t.build_update_sql.invoke(
                {"table": "master_categories", "set_values": {"name": "x"}}
            ))
        self.assertFalse(data["ok"])
        self.assertIn("WHERE", data["error"])

    def test_build_update_sql_ok(self):
        from unittest.mock import patch

        from apps.core.services.AI.Agents.core import plan_tools as t
        from apps.core.services.AI.Agents.core import master_api, schema_catalog

        with patch.object(schema_catalog, "extract_table_fields", return_value={"name": {}, "id": {}}):
            data = json.loads(t.build_update_sql.invoke(
                {"table": "master_categories", "set_values": {"name": "Cat"},
                 "where": "id = 'c1'"}
            ))
        self.assertTrue(data["ok"])
        self.assertIn("UPDATE master_categories", data["sql"])
        self.assertIn("WHERE id = 'c1'", data["sql"])

    def test_build_insert_sql_rejects_unknown_column(self):
        from unittest.mock import patch

        from apps.core.services.AI.Agents.core import plan_tools as t
        from apps.core.services.AI.Agents.core import master_api, schema_catalog

        with patch.object(schema_catalog, "extract_table_fields", return_value={"name": {}}):
            data = json.loads(t.build_insert_sql.invoke(
                {"table": "master_categories", "values": {"bogus": "x"}}
            ))
        self.assertFalse(data["ok"])
        self.assertIn("bogus", data["error"])


class WebToolTests(TestCase):
    def test_fetch_web_content_blocks_private_host(self):
        from apps.core.services.AI.Agents.core import plan_tools as t

        data = json.loads(t.fetch_web_content.invoke({"url": "http://127.0.0.1/x"}))
        self.assertFalse(data["ok"])

    def test_search_web_disabled_provider(self):
        from unittest.mock import patch

        from apps.core.services.AI.Agents.core import plan_tools as t

        with patch.object(
            t.config, "get",
            side_effect=lambda k, d=None: {"AI_WEB_SEARCH_PROVIDER": "none"}.get(k, d),
        ):
            data = json.loads(t.search_web.invoke({"query": "pets"}))
        self.assertFalse(data["ok"])
        self.assertIn("disabled", data["error"])

    def test_search_web_parses_ddg_html(self):
        from unittest.mock import patch

        from apps.core.services.AI.Agents.core import plan_tools as t

        html = (
            '<html><body>'
            '<div class="result">'
            '<a class="result__a" href="//duckduckgo.com/l/?uddg='
            'https%3A%2F%2Fexample.com%2Fpets">Example Pets</a>'
            '<a class="result__snippet">A page about pets.</a>'
            '</div>'
            '<div class="result result--ad">'
            '<a class="result__a" href="//duckduckgo.com/l/?uddg='
            'https%3A%2F%2Fads.com">Ad</a>'
            '</div>'
            '</body></html>'
        )

        class _Resp:
            status_code = 200
            text = html

            def raise_for_status(self):
                pass

        class _Session:
            trust_env = False

            def get(self, url, params=None, headers=None, timeout=None):
                return _Resp()

        with patch.object(t.requests, "Session", _Session):
            data = json.loads(t.search_web.invoke({"query": "pets", "max_results": 5}))

        self.assertTrue(data["ok"])
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["results"][0]["url"], "https://example.com/pets")
        self.assertEqual(data["results"][0]["title"], "Example Pets")


class AllowlistSettingsTests(TestCase):
    def test_defaults_include_product_and_sales_read_tables(self):
        from apps.core.services.AI.Agents.core import settings

        self.assertIn("master_categories", settings.ALLOWED_READ_TABLES)
        self.assertIn("products", settings.QUERY_ALLOWED_TABLES)
        self.assertIn("product_names", settings.QUERY_ALLOWED_TABLES)
        self.assertIn("sales_quotations", settings.QUERY_ALLOWED_TABLES)
        self.assertIn("master_product_attributes", settings.WRITE_ALLOWED_TABLES)
        self.assertNotIn("products", settings.ALLOWED_READ_TABLES)

    def test_env_tuple_splits_and_trims(self):
        from apps.core.services.AI.Agents.core import settings

        self.assertEqual(settings._env_tuple("X", ("a", "b")), ("a", "b"))

    def test_settings_allowlist_resolves_same_tuples(self):
        from apps.core.services.AI.Agents.core import settings

        self.assertIn("products", settings.QUERY_ALLOWED_TABLES)


class SchemaCatalogTests(TestCase):
    def _write_schema(self, tmp_dir):
        content = (
            "module.exports = {\n"
            "  TEST_CATS: {\n"
            "    name: 'master_categories',\n"
            "    fields: {\n"
            "      id: { type: 'VARCHAR(36)', primaryKey: true },\n"
            "      parent_id: {\n"
            "        type: 'VARCHAR(36)',\n"
            "        references: { table: 'master_categories', field: 'id', onDelete: 'SET NULL' },\n"
            "      },\n"
            "    },\n"
            "    constraints: { uq: { type: 'UNIQUE', fields: ['name'] } },\n"
            "  },\n"
            "};\n"
        )
        import os

        path = os.path.join(tmp_dir, "master.js")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
        return path

    def test_load_schema_catalog_parses_fields_and_fks(self):
        import tempfile
        from unittest.mock import patch

        from apps.core.services.AI.Agents.core import master_api, schema_catalog

        with tempfile.TemporaryDirectory() as tmp_dir:
            self._write_schema(tmp_dir)
            with patch.object(schema_catalog, "SCHEMA_DIR", type(schema_catalog.SCHEMA_DIR)(tmp_dir)):
                catalog = schema_catalog.load_schema_catalog(reload=True)

        fields = catalog.get("master_categories")
        self.assertIsNotNone(fields)
        self.assertEqual(fields["id"]["type"], "VARCHAR(36)")
        self.assertTrue(fields["id"]["primaryKey"])
        self.assertEqual(fields["parent_id"]["references"]["table"], "master_categories")
        self.assertEqual(fields["parent_id"]["references"]["field"], "id")
        self.assertNotIn("references", fields)
        self.assertNotIn("constraints", fields)

    def test_render_schema_catalog_marks_foreign_keys(self):
        import tempfile
        from unittest.mock import patch

        from apps.core.services.AI.Agents.core import master_api, schema_catalog

        with tempfile.TemporaryDirectory() as tmp_dir:
            self._write_schema(tmp_dir)
            with patch.object(schema_catalog, "SCHEMA_DIR", type(schema_catalog.SCHEMA_DIR)(tmp_dir)):
                schema_catalog.load_schema_catalog(reload=True)
                text = schema_catalog.render_schema_catalog(["master_categories"])

        self.assertIn("parent_id", text)
        self.assertIn("-> master_categories.id", text)


class SqlBuilderAgentTests(TestCase):
    @staticmethod
    def _ai_message(content):
        class _Ai:
            type = "ai"

            def __init__(self, content):
                self.content = content

        return _Ai(content)

    @staticmethod
    def _tool_message(content):
        class _Tool:
            type = "tool"

            def __init__(self, content):
                self.content = content

        return _Tool(content)

    def test_parse_result_keeps_select_strips_write(self):
        from apps.core.services.AI.Agents.domains.product.sql.manager import (
            ProductSqlAgent,
        )

        worker = ProductSqlAgent.__new__(ProductSqlAgent)
        payload = {
            "answer": "3 categories",
            "summary": "anti-join",
            "sql": [
                {"purpose": "lookup", "statement": "SELECT c.name FROM master_categories c;", "verification": ""},
                {"purpose": "bad", "statement": "UPDATE master_categories SET name='x' WHERE id='1';", "verification": ""},
            ],
            "tables_used": ["master_categories"],
        }
        result = worker._parse_result({"messages": [self._ai_message(json.dumps(payload))]})

        self.assertTrue(result["ok"])
        statements = [s["statement"] for s in result["sql"]]
        self.assertTrue(any(s.startswith("SELECT") for s in statements))
        self.assertFalse(any("UPDATE" in s for s in statements))
        self.assertTrue(any("UPDATE" in r for r in result["risks"]))

    def test_parse_result_collects_executed_queries(self):
        from apps.core.services.AI.Agents.domains.product.sql.manager import (
            ProductSqlAgent,
        )

        worker = ProductSqlAgent.__new__(ProductSqlAgent)
        query_json = json.dumps({"ok": True, "query": "SELECT 1", "rowCount": 1, "rows": [{"1": 1}]})
        payload = {
            "answer": "one row",
            "summary": "s",
            "sql": [{"purpose": "p", "statement": "SELECT 1;", "verification": ""}],
            "tables_used": [],
        }
        result = worker._parse_result(
            {"messages": [self._ai_message(json.dumps(payload)), self._tool_message(query_json)]}
        )

        self.assertTrue(result["executed"])
        self.assertEqual(len(result["queries"]), 1)
        self.assertEqual(result["queries"][0]["rowCount"], 1)

    def test_parse_result_flags_non_executable_table(self):
        from apps.core.services.AI.Agents.domains.product.sql.manager import (
            ProductSqlAgent,
        )

        worker = ProductSqlAgent.__new__(ProductSqlAgent)
        payload = {
            "answer": "a",
            "summary": "s",
            "sql": [{"purpose": "p", "statement": "SELECT * FROM master_invoice_types;", "verification": ""}],
            "tables_used": [],
        }
        result = worker._parse_result({"messages": [self._ai_message(json.dumps(payload))]})
        self.assertTrue(any("outside AI_QUERY_TABLES" in m for m in result["missing_data"]))

    def test_parse_result_without_json_reports_error(self):
        from apps.core.services.AI.Agents.domains.product.sql.manager import (
            ProductSqlAgent,
        )

        worker = ProductSqlAgent.__new__(ProductSqlAgent)
        result = worker._parse_result({"messages": [self._ai_message("just some prose, no JSON here")]})
        self.assertFalse(result["ok"])
        self.assertIn("error", result)

    def test_tables_in_sql_extracts_from_and_join(self):
        from apps.core.services.AI.Agents.domains.product.sql.manager import _tables_in_sql

        tables = _tables_in_sql(
            "SELECT c.name FROM master_categories c "
            "LEFT JOIN master_product_category_attribute_assign a ON a.category_id = c.id"
        )
        self.assertEqual(tables, ["master_categories", "master_product_category_attribute_assign"])

    def test_build_product_sql_tool_is_registered(self):
        from apps.core.services.AI.Agents.domains.product import manager as m

        names = {t.name for t in m.PLAN_TOOLS}
        self.assertIn("build_product_sql", names)


class AgentGraphTests(TestCase):
    """Offline tests for the workers' explicit LangGraph loop (no LLM/network)."""

    def test_attributes_agent_graph_has_model_and_tools_nodes(self):
        from apps.core.services.AI.Agents.domains.product.attributes.manager import (
            ProductAttributesAgent,
        )

        graph = ProductAttributesAgent()._get_agent()
        nodes = set(graph.get_graph().nodes.keys())
        self.assertIn("model", nodes)
        self.assertIn("tools", nodes)

    def test_sql_builder_agent_graph_has_model_and_tools_nodes(self):
        from apps.core.services.AI.Agents.domains.product.sql.manager import (
            ProductSqlAgent,
        )

        graph = ProductSqlAgent()._get_agent()
        nodes = set(graph.get_graph().nodes.keys())
        self.assertIn("model", nodes)
        self.assertIn("tools", nodes)

    def test_attributes_agent_injects_system_prompt(self):
        from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

        from apps.core.services.AI.Agents.domains.product.attributes.prompts import (
            ATTRIBUTE_SYSTEM_PROMPT,
        )
        from apps.core.services.AI.Agents.domains.product.attributes.manager import (
            ProductAttributesAgent,
        )

        captured = []

        class _Bound:
            def invoke(self, messages):
                captured.append(list(messages))
                return AIMessage(content='{"summary": "ok"}')

        class _FakeLLM:
            def bind_tools(self, tools):
                return _Bound()

        worker = ProductAttributesAgent(llm=_FakeLLM())
        state = worker._get_agent().invoke(
            {"messages": [HumanMessage(content="create a color attribute")]}
        )

        self.assertTrue(captured)
        self.assertIsInstance(captured[0][0], SystemMessage)
        self.assertIn(ATTRIBUTE_SYSTEM_PROMPT, captured[0][0].content)
        self.assertFalse(
            any(isinstance(m, SystemMessage) for m in state["messages"])
        )

    def test_sql_builder_agent_injects_system_prompt(self):
        from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

        from apps.core.services.AI.Agents.domains.product.sql.manager import (
            ProductSqlAgent,
        )
        from apps.core.services.AI.Agents.domains.product.sql.prompts import _system_prompt

        captured = []

        class _Bound:
            def invoke(self, messages):
                captured.append(list(messages))
                return AIMessage(content='{"summary": "ok"}')

        class _FakeLLM:
            def bind_tools(self, tools):
                return _Bound()

        worker = ProductSqlAgent(llm=_FakeLLM())
        state = worker._get_agent().invoke(
            {"messages": [HumanMessage(content="how many categories?")]}
        )

        self.assertTrue(captured)
        self.assertIsInstance(captured[0][0], SystemMessage)
        # The formatted prompt embeds the executable-table index and core schemas.
        self.assertIn(_system_prompt(), captured[0][0].content)
        self.assertFalse(
            any(isinstance(m, SystemMessage) for m in state["messages"])
        )

    def test_sql_builder_agent_parses_tool_messages(self):
        # The graph's ToolNode writes type="tool" messages; _parse_result's
        # query collection must keep reading them after the create_agent swap.
        from apps.core.services.AI.Agents.domains.product.sql.manager import (
            ProductSqlAgent,
        )

        worker = ProductSqlAgent.__new__(ProductSqlAgent)

        class _Tool:
            type = "tool"

            def __init__(self, content):
                self.content = content

        class _Ai:
            type = "ai"

            def __init__(self, content):
                self.content = content

        query_json = json.dumps(
            {"ok": True, "query": "SELECT 1", "rowCount": 1, "rows": [{"1": 1}]}
        )
        result = worker._parse_result(
            {
                "messages": [
                    _Ai(json.dumps({"summary": "s", "sql": [], "tables_used": []})),
                    _Tool(query_json),
                ]
            }
        )
        self.assertTrue(result["executed"])
        self.assertEqual(len(result["queries"]), 1)




    def test_name_agent_graph_has_single_model_node(self):
        from apps.core.services.AI.Agents.domains.product.naming.manager import (
            ProductNameAgent,
        )

        class _NoopLLM:
            pass

        graph = ProductNameAgent(llm=_NoopLLM())._get_agent()
        nodes = set(graph.get_graph().nodes.keys())
        self.assertEqual(nodes, {"__start__", "model", "__end__"})
        self.assertNotIn("tools", nodes)
        self.assertEqual(len(graph.get_graph().edges), 2)

    def test_name_agent_injects_system_prompt(self):
        from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

        from apps.core.services.AI.Agents.domains.product.naming.prompts import (
            NAME_SYSTEM_PROMPT,
        )
        from apps.core.services.AI.Agents.domains.product.naming.manager import (
            ProductNameAgent,
        )

        captured = []

        class _FakeLLM:
            def invoke(self, messages):
                captured.append(list(messages))
                return AIMessage(content='{"seo_name": "Dog Bed"}')

        worker = ProductNameAgent(llm=_FakeLLM())
        state = worker._get_agent().invoke(
            {"messages": [HumanMessage(content="宠物窝")]}
        )

        self.assertTrue(captured)
        self.assertIsInstance(captured[0][0], SystemMessage)
        self.assertEqual(captured[0][0].content, NAME_SYSTEM_PROMPT)
        self.assertFalse(
            any(isinstance(m, SystemMessage) for m in state["messages"])
        )



class ManagerGeneralTests(TestCase):
    """Conversation memory, compaction and slash-command parsing."""

    def test_parse_command_table(self):
        from apps.core.services.AI.Agents.core.router import parse_command

        self.assertEqual(parse_command("").name, "empty")
        self.assertEqual(parse_command("/q").name, "quit")
        self.assertEqual(parse_command("quit").name, "quit")
        self.assertEqual(parse_command("/new").name, "reset")
        self.assertEqual(parse_command("/compact").name, "compact")
        self.assertEqual(parse_command("/dry").name, "dry")
        self.assertEqual(parse_command("/live").name, "live")
        self.assertEqual(parse_command("/help").name, "help")

        cmd = parse_command("hello")
        self.assertEqual(
            (cmd.name, cmd.mode, cmd.domain, cmd.text),
            ("message", "plan", None, "hello"),
        )
        cmd = parse_command("/act add material")
        self.assertEqual(
            (cmd.name, cmd.mode, cmd.domain, cmd.text),
            ("message", "act", None, "add material"),
        )
        cmd = parse_command("/plan list categories")
        self.assertEqual((cmd.name, cmd.mode), ("message", "plan"))
        cmd = parse_command("/sales show quotations")
        self.assertEqual(
            (cmd.name, cmd.mode, cmd.domain, cmd.text),
            ("message", "plan", "sales", "show quotations"),
        )
        cmd = parse_command("@product prices")
        self.assertEqual((cmd.name, cmd.domain, cmd.text), ("message", "product", "prices"))
        cmd = parse_command("/act")
        self.assertEqual((cmd.name, cmd.mode, cmd.text), ("message", "act", ""))

    def test_compact_replaces_history_with_summary(self):
        from apps.core.services.AI.Agents.core.memory import ConversationMemory

        class _FakeLLM:
            def invoke(self, messages):
                class _Resp:
                    content = "A short summary of everything."

                return _Resp()

        mem = ConversationMemory()
        mem.llm = _FakeLLM()
        mem.record("list master tables", {"summary": "18 tables", "answer": "..."})
        mem.record("how many?", {"summary": "18", "answer": "18"})
        self.assertEqual(len(mem.messages), 4)

        info = mem.compact(force=True)
        self.assertTrue(info["compacted"])
        self.assertEqual(info["before"], 4)
        self.assertEqual(info["after"], 2)
        self.assertEqual(len(mem.messages), 2)
        self.assertIn("Summary of the conversation", mem.messages[0][1])

        # A non-forced compact on an already-minimal history is a no-op.
        self.assertFalse(mem.compact()["compacted"])

    def test_compact_noop_when_already_minimal(self):
        from apps.core.services.AI.Agents.core.memory import ConversationMemory

        mem = ConversationMemory()
        self.assertFalse(mem.compact()["compacted"])

    def test_record_stores_rendered_plan(self):
        from apps.core.services.AI.Agents.core.memory import ConversationMemory

        mem = ConversationMemory()
        mem.record("do x", {"summary": "did", "plan": ["a"], "sql": [], "risks": []})
        self.assertEqual(mem.messages[0], ("human", "do x"))
        # The assistant turn concatenates the human request + summary + plan.
        block = mem.messages[1][1]
        self.assertIn("Human request: do x", block)
        self.assertIn("Summary: did", block)
        self.assertIn("1. a", block)

    def test_render_turn_includes_answer_summary_plan(self):
        from apps.core.services.AI.Agents.core.memory import render_turn

        plan = {
            "answer": "3 rows",
            "summary": "Found categories",
            "plan": ["read categories", "join assignments"],
            "sql": [
                {
                    "purpose": "count",
                    "statement": "SELECT COUNT(*)",
                    "verification": "expected 3",
                }
            ],
            "drafts": [
                {
                    "agent": "product_sql_builder",
                    "purpose": "insert category",
                    "statement": "INSERT INTO master_categories (name) VALUES ('x')",
                }
            ],
            "missing_data": ["category id missing"],
            "risks": ["assumes parent_id"],
        }
        text = render_turn("list unassigned categories", plan)
        self.assertIn("Human request: list unassigned categories", text)
        self.assertIn("Answer: 3 rows", text)
        self.assertIn("Summary: Found categories", text)
        self.assertIn("1. read categories", text)
        self.assertIn("2. join assignments", text)
        self.assertIn("SQL (count):", text)
        self.assertIn("SELECT COUNT(*)", text)
        self.assertIn("verify: expected 3", text)
        self.assertIn("Drafts (not executed):", text)
        self.assertIn("insert category", text)
        self.assertIn("INSERT INTO master_categories", text)
        self.assertIn("Missing data:", text)
        self.assertIn("- category id missing", text)
        self.assertIn("Risks:", text)
        self.assertIn("- assumes parent_id", text)

    def test_record_replays_previous_human_input_and_plan(self):
        from apps.core.services.AI.Agents.core.memory import ConversationMemory

        mem = ConversationMemory()
        mem.record("first question", {"summary": "one", "plan": ["step 1"]})
        mem.record("second question", {"summary": "two", "plan": ["step 2"]})

        self.assertEqual(len(mem.history()), 4)
        self.assertEqual(mem.history()[0], ("human", "first question"))
        self.assertIn("Summary: one", mem.history()[1][1])
        self.assertIn("Human request: first question", mem.history()[1][1])
        self.assertEqual(mem.history()[2], ("human", "second question"))
        self.assertIn("Summary: two", mem.history()[3][1])
        self.assertIn("Human request: second question", mem.history()[3][1])

    def test_render_turn_falls_back_to_agent_results(self):
        from apps.core.services.AI.Agents.core.memory import render_turn

        plan = {
            "answer": "",
            "summary": "",
            "plan": [],
            "sql": [],
            "missing_data": [
                "[unknown] Table not allowed for reading: master_product_attribute_assignments"
            ],
            "risks": [
                "The model produced no structured reply and no executed SELECT rows were available."
            ],
            "agent_results": [
                {
                    "agent": "unknown",
                    "ok": False,
                    "error": "Table not allowed for reading: master_product_attribute_assignments",
                },
                {
                    "agent": "product_sql_builder",
                    "ok": True,
                    "summary": "SELECT query finds all product categories with no attribute assignments.",
                    "answer": "Found 2 product categories with no attributes:\n\n1. Baby Crib\n2. Bowl",
                    "sql": [
                        {
                            "purpose": "Find product categories with no attribute assignments",
                            "statement": "SELECT mc.id FROM master_categories mc",
                            "verification": "LEFT JOIN filters NULL",
                        }
                    ],
                    "plan": ["Describe schema", "Write LEFT JOIN", "Execute"],
                    "tables_used": [
                        "master_categories",
                        "master_product_category_attribute_assign",
                    ],
                    "queries": [
                        {
                            "query": "SELECT mc.id FROM master_categories mc",
                            "rowCount": 2,
                            "rows": [],
                        }
                    ],
                    "executed": True,
                },
            ],
        }
        text = render_turn("which category has no attribute", plan)
        self.assertIn("Answer: Found 2 product categories", text)
        self.assertIn("Summary: SELECT query finds", text)
        self.assertIn("[product_sql_builder] Describe schema", text)
        self.assertIn("SQL ([product_sql_builder] Find product categories", text)
        self.assertIn("verify: LEFT JOIN filters NULL", text)
        self.assertIn(
            "Tables: master_categories, master_product_category_attribute_assign",
            text,
        )
        self.assertIn("[unknown] FAILED", text)
        self.assertIn("error: Table not allowed for reading", text)
        self.assertIn("- 2 row(s):", text)

    def test_render_turn_omits_row_payloads(self):
        from apps.core.services.AI.Agents.core.memory import render_turn

        plan = {
            "agent_results": [
                {
                    "agent": "product_sql_builder",
                    "ok": True,
                    "answer": "Found 62 product categories.",
                    "queries": [
                        {
                            "query": "SELECT mc.id FROM master_categories mc",
                            "rowCount": 62,
                            "rows": [
                                {
                                    "id": "f1000000-0000-0000-0000-000000000012",
                                    "name": "Baby Crib",
                                }
                            ],
                        }
                    ],
                    "executed": True,
                }
            ],
        }
        text = render_turn("", plan)
        self.assertNotIn("f1000000-0000-0000-0000-000000000012", text)
        self.assertNotIn("Baby Crib", text)
        self.assertIn("62 row(s)", text)

    def test_render_turn_dedupes_missing_data_and_risks(self):
        from apps.core.services.AI.Agents.core.memory import render_turn

        plan = {
            "missing_data": ["dup"],
            "risks": ["risk dup"],
            "agent_results": [
                {
                    "agent": "w",
                    "ok": True,
                    "missing_data": ["dup", "unique"],
                    "risks": ["risk dup"],
                }
            ],
        }
        text = render_turn("", plan)
        self.assertEqual(text.count("- dup"), 1)
        self.assertEqual(text.count("- risk dup"), 1)
        self.assertIn("- unique", text)

    def test_render_turn_respects_max_chars(self):
        from unittest.mock import patch

        from apps.core.services.AI.Agents.core.memory import render_turn

        plan = {"summary": "x" * 5000}
        with patch.dict("os.environ", {"AI_TURN_RENDER_MAX_CHARS": "100"}):
            text = render_turn("t", plan)
        self.assertIn("… (turn truncated)", text)
        self.assertLessEqual(len(text), 100 + len("\n… (turn truncated)"))

    def test_reset_clears_messages(self):
        from apps.core.services.AI.Agents.core.memory import ConversationMemory

        mem = ConversationMemory()
        mem.record("a", {"summary": "b"})
        mem.reset()
        self.assertEqual(mem.messages, [])








class GeneralRoutingTests(TestCase):
    """The general agent classifies and dispatches to product / sales."""

    def test_pinned_skips_router(self):
        from apps.core.services.AI.Agents.core.router import GeneralManagerAgent

        class _FakeLLM:
            def invoke(self, messages):
                raise AssertionError("router must not run when pinned")

        agent = GeneralManagerAgent(pinned="product", llm=_FakeLLM())
        self.assertEqual(agent.classify("list master tables"), "product")

    def test_llm_router_selects_sales(self):
        from apps.core.services.AI.Agents.core.router import GeneralManagerAgent

        class _FakeLLM:
            def invoke(self, messages):
                class _Resp:
                    content = '{"domain": "sales", "reason": "quotations"}'

                return _Resp()

        agent = GeneralManagerAgent(llm=_FakeLLM())
        self.assertEqual(agent.classify("show the quotations"), "sales")
        self.assertEqual(agent.last_domain, "sales")

    def test_keyword_fallback_when_llm_returns_junk(self):
        from apps.core.services.AI.Agents.core.router import GeneralManagerAgent

        class _FakeLLM:
            def invoke(self, messages):
                class _Resp:
                    content = "not json at all"

                return _Resp()

        agent = GeneralManagerAgent(llm=_FakeLLM())
        self.assertEqual(agent.classify("packing list for this shipment"), "sales")

    def test_keyword_classify_product(self):
        from apps.core.services.AI.Agents.core.router import GeneralManagerAgent

        self.assertEqual(
            GeneralManagerAgent._keyword_classify("add a new category"), "product"
        )

    def test_stream_routes_to_domain_and_records(self):
        from apps.core.services.AI.Agents.core.router import GeneralManagerAgent

        agent = GeneralManagerAgent(pinned="product")
        calls = []

        class _Sub:
            def stream(self, task, history=None):
                calls.append(("stream", task, history))
                plan = {"summary": "done", "sql": [], "risks": []}
                yield {"type": "plan", "plan": plan, "text": "Summary: done"}

        agent._managers["product"] = _Sub()

        out = list(agent.stream("do a thing"))
        self.assertEqual(calls[0][0], "stream")
        self.assertEqual(calls[0][1], "do a thing")
        # First turn hands the (empty) history to the sub-agent.
        self.assertEqual(calls[0][2], [])
        self.assertTrue(any(e["type"] == "plan" for e in out))
        # The finished turn was recorded as (human, ai).
        self.assertEqual(len(agent.messages), 2)
        self.assertEqual(agent.messages[0], ("human", "do a thing"))

    def test_act_guards_and_executes(self):
        from unittest.mock import patch

        from apps.core.services.AI.Agents.core.router import GeneralManagerAgent

        agent = GeneralManagerAgent(pinned="product")
        plan = {"summary": "s", "sql": [{"statement": "SELECT 1;"}], "risks": []}
        with patch(
            "apps.core.services.AI.Agents.core.router.execute_plan",
            return_value=[{"verb": "SELECT", "ok": True}],
        ) as ep:
            _, executed = agent.act(plan, dry_run=True)
        self.assertEqual(executed, [{"verb": "SELECT", "ok": True}])
        ep.assert_called_once()


class GuardrailTests(TestCase):
    """The single SQL guardrail (core/guardrail.py)."""

    def test_multi_statement_rejected(self):
        from apps.core.services.AI.Agents.core import guardrail

        plan = {"sql": [{"statement": "SELECT 1; DELETE FROM master_categories;"}],
                "drafts": [], "risks": []}
        cleaned = guardrail.assert_plan_sql(plan, mode="plan")
        self.assertEqual(cleaned["sql"], [])
        self.assertTrue(any("multiple" in r for r in cleaned["risks"]))

    def test_cte_delete_rejected_in_plan(self):
        from apps.core.services.AI.Agents.core import guardrail

        statement = "WITH x AS (SELECT id FROM master_categories) DELETE FROM x;"
        plan = {"sql": [{"statement": statement}], "drafts": [], "risks": []}
        cleaned = guardrail.assert_plan_sql(plan, mode="plan")
        self.assertEqual(cleaned["sql"], [])
        self.assertTrue(any("DELETE" in r for r in cleaned["risks"]))

    def test_delete_without_where_rejected_in_act(self):
        from apps.core.services.AI.Agents.core import guardrail

        plan = {"sql": [{"statement": "DELETE FROM master_categories;"}],
                "drafts": [], "risks": []}
        cleaned = guardrail.assert_plan_sql(plan, mode="act")
        self.assertEqual(cleaned["sql"], [])
        self.assertTrue(any("WHERE" in r for r in cleaned["risks"]))

    def test_update_capped_with_limit_in_act(self):
        from apps.core.services.AI.Agents.core import guardrail

        plan = {"sql": [{"statement": "UPDATE master_categories SET name='y' WHERE id='c1';"}],
                "drafts": [], "risks": []}
        cleaned = guardrail.assert_plan_sql(plan, mode="act")
        self.assertIn("LIMIT", cleaned["sql"][0]["statement"])

    def test_drafts_filtered_independently(self):
        from apps.core.services.AI.Agents.core import guardrail

        plan = {
            "sql": [{"statement": "SELECT 1;"}],
            "drafts": [{"statement": "DROP TABLE master_categories;"}],
            "risks": [],
        }
        cleaned = guardrail.assert_plan_sql(plan, mode="plan")
        self.assertEqual(len(cleaned["sql"]), 1)
        self.assertEqual(cleaned["drafts"], [])
        self.assertTrue(any("DROP" in r for r in cleaned["risks"]))

    def test_comments_rejected(self):
        from apps.core.services.AI.Agents.core import guardrail

        decision = guardrail.validate_statement(
            "SELECT 1; -- sneaky", mode="plan"
        )
        self.assertFalse(decision.ok)

    def test_string_semicolon_is_not_multi_statement(self):
        from apps.core.services.AI.Agents.core import guardrail

        decision = guardrail.validate_statement(
            "SELECT 'a;b' AS x;", mode="plan"
        )
        self.assertTrue(decision.ok)


class AgentResultContractTests(TestCase):
    def test_roundtrip_preserves_extra_fields(self):
        from apps.core.services.AI.Agents.core.contracts import AgentResult

        payload = {
            "agent": "product_sql_builder",
            "ok": True,
            "summary": "s",
            "data": {"tiers": [{"min_qty": 10, "sale_price": 1.0}]},
            "sql": [{"statement": "INSERT INTO product_sale_prices_by_qty ..."}],
            "missing_data": [],
            "risks": [],
        }
        parsed = AgentResult.from_tool_content(payload)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.agent, "product_sql_builder")
        self.assertEqual(parsed.data["tiers"][0]["min_qty"], 10)

    def test_failure_envelope_without_agent_is_surfaced(self):
        # A bare error envelope (no "worker" key) must still be collected.
        from apps.core.services.AI.Agents.core.engine import DomainManagerAgent

        class _Msg:
            type = "tool"
            content = json.dumps({"ok": False, "error": "boom"})

        agent_results, _ = DomainManagerAgent._collect_agent_results(
            {"messages": [_Msg()]}
        )
        self.assertEqual(len(agent_results), 1)
        self.assertFalse(agent_results[0]["ok"])
        self.assertEqual(agent_results[0]["agent"], "unknown")


class PlanStoreTests(TestCase):
    def test_put_get_and_hash_mismatch(self):
        from apps.core.services.AI.Agents.core import plan_store

        plan = {"summary": "s", "sql": [{"statement": "SELECT 1;"}], "drafts": []}
        plan_id, plan_hash = plan_store.put(plan, domain="product")
        envelope = plan_store.get(plan_id, plan_hash=plan_hash)
        self.assertEqual(envelope["plan"], plan)

        with self.assertRaises(plan_store.PlanHashMismatch):
            plan_store.get(plan_id, plan_hash="deadbeef")

    def test_consume_removes_plan(self):
        from apps.core.services.AI.Agents.core import plan_store

        plan_id, _ = plan_store.put({"sql": []}, domain="product")
        plan_store.consume(plan_id)
        with self.assertRaises(plan_store.PlanNotFound):
            plan_store.get(plan_id)


class ToolSchemaTests(TestCase):
    def test_build_sales_sql_coerces_csv_and_bool(self):
        from unittest.mock import patch

        from apps.core.services.AI.Agents.domains.sales.tools import build_sales_sql
        from apps.core.services.AI.Agents.domains.product.sql.manager import ProductSqlAgent

        seen = {}

        def fake_plan(self, question, tables=None, execute=True):
            seen["tables"] = tables
            seen["execute"] = execute
            return {"agent": "sales_sql_builder", "ok": True, "sql": [],
                    "summary": "ok", "missing_data": [], "risks": []}

        with patch.object(ProductSqlAgent, "plan", fake_plan):
            build_sales_sql.invoke(
                {"question": "q", "tables": "sales_quotations, master_currencies",
                 "execute": "false"}
            )

        self.assertEqual(seen["tables"], ["sales_quotations", "master_currencies"])
        self.assertFalse(seen["execute"])


class TracingConfigTests(TestCase):
    """Offline tests for LangSmith tracing config (core.tracing)."""

    def setUp(self):
        from apps.core.services.AI.Agents.core import tracing

        tracing._configured = False

    def _configure(self, env):
        from unittest.mock import patch

        from apps.core.services.AI.Agents.core import tracing

        with patch.dict("os.environ", env):
            enabled = tracing.configure_tracing()
            result = dict(os.environ)
        return enabled, result

    def test_disabled_without_api_key(self):
        enabled, env = self._configure(
            {
                "LANGSMITH_API_KEY": "",
                "LANGSMITH_TRACING": "",
                "LANGSMITH_AGENT_ID": "",
                "LANGSMITH_AGENT_ENVIRONMENT": "",
                "LANGSMITH_PROJECT": "",
            }
        )
        self.assertFalse(enabled)
        self.assertEqual(env["LANGSMITH_TRACING"], "false")

    def test_flag_true_without_key_still_disabled(self):
        enabled, env = self._configure(
            {
                "LANGSMITH_API_KEY": "",
                "LANGSMITH_TRACING": "true",
                "LANGSMITH_AGENT_ID": "",
                "LANGSMITH_AGENT_ENVIRONMENT": "",
            }
        )
        self.assertFalse(enabled)
        self.assertEqual(env["LANGSMITH_TRACING"], "false")

    def test_enabled_with_api_key_applies_defaults(self):
        enabled, env = self._configure(
            {
                "LANGSMITH_API_KEY": "lsv2_pt_test",
                "LANGSMITH_TRACING": "",
                "LANGSMITH_AGENT_ID": "",
                "LANGSMITH_AGENT_ENVIRONMENT": "",
                "LANGSMITH_PROJECT": "",
            }
        )
        self.assertTrue(enabled)
        self.assertEqual(env["LANGSMITH_TRACING"], "true")
        self.assertEqual(env["LANGSMITH_ENDPOINT"], "https://api.smith.langchain.com")
        self.assertEqual(env["LANGSMITH_AGENT_ID"], "ollama-agents-qwen3-5")
        self.assertEqual(env["LANGSMITH_AGENT_ENVIRONMENT"], "local")

    def test_does_not_override_explicit_values(self):
        enabled, env = self._configure(
            {
                "LANGSMITH_API_KEY": "lsv2_pt_test",
                "LANGSMITH_TRACING": "true",
                "LANGSMITH_ENDPOINT": "https://custom.example.com",
                "LANGSMITH_AGENT_ID": "my-agent",
                "LANGSMITH_AGENT_ENVIRONMENT": "production",
            }
        )
        self.assertTrue(enabled)
        self.assertEqual(env["LANGSMITH_ENDPOINT"], "https://custom.example.com")
        self.assertEqual(env["LANGSMITH_AGENT_ID"], "my-agent")
        self.assertEqual(env["LANGSMITH_AGENT_ENVIRONMENT"], "production")

    def test_project_suppresses_agent_address(self):
        enabled, env = self._configure(
            {
                "LANGSMITH_API_KEY": "lsv2_pt_test",
                "LANGSMITH_TRACING": "",
                "LANGSMITH_PROJECT": "my-project",
                "LANGSMITH_AGENT_ID": "",
                "LANGSMITH_AGENT_ENVIRONMENT": "",
            }
        )
        self.assertTrue(enabled)
        self.assertEqual(env["LANGSMITH_AGENT_ID"], "")
        self.assertEqual(env["LANGSMITH_AGENT_ENVIRONMENT"], "")

    def test_invalid_agent_id_disables_tracing(self):
        enabled, env = self._configure(
            {
                "LANGSMITH_API_KEY": "lsv2_pt_test",
                "LANGSMITH_TRACING": "",
                "LANGSMITH_AGENT_ID": "INVALID_ID!",
                "LANGSMITH_AGENT_ENVIRONMENT": "local",
            }
        )
        self.assertFalse(enabled)
        self.assertEqual(env["LANGSMITH_TRACING"], "false")

    def test_invalid_environment_disables_tracing(self):
        enabled, env = self._configure(
            {
                "LANGSMITH_API_KEY": "lsv2_pt_test",
                "LANGSMITH_TRACING": "",
                "LANGSMITH_AGENT_ID": "ollama-agents-qwen3-5",
                "LANGSMITH_AGENT_ENVIRONMENT": "qa",
            }
        )
        self.assertFalse(enabled)
        self.assertEqual(env["LANGSMITH_TRACING"], "false")

    def test_run_config_shape(self):
        from apps.core.services.AI.Agents.core import tracing

        cfg = tracing.run_config(
            run_name="ProductMasterAgent.plan",
            domain="product",
            mode="plan",
            model="qwen3.5",
        )
        self.assertEqual(cfg["run_name"], "ProductMasterAgent.plan")
        self.assertIn("langsmith-trace", cfg["tags"])
        self.assertIn("product", cfg["tags"])
        self.assertIn("plan", cfg["tags"])
        self.assertEqual(cfg["metadata"]["domain"], "product")
        self.assertEqual(cfg["metadata"]["mode"], "plan")
        self.assertEqual(cfg["metadata"]["model"], "qwen3.5")
        self.assertIn("agent_id", cfg["metadata"])
        self.assertIn("agent_environment", cfg["metadata"])
        # No session_id by default.
        self.assertNotIn("session_id", cfg["metadata"])

    def test_run_config_includes_session_id(self):
        from apps.core.services.AI.Agents.core import tracing

        cfg = tracing.run_config(
            run_name="ProductMasterAgent.plan",
            domain="product",
            mode="plan",
            model="qwen3.5",
            session_id="conv-123",
        )
        self.assertEqual(cfg["metadata"]["session_id"], "conv-123")

    def test_stream_agent_forwards_config(self):
        from apps.core.services.AI.Agents.core import streaming

        captured = {}

        class _Agent:
            def stream(self, inp, config=None, stream_mode=None):
                captured["config"] = config
                yield ("values", {"messages": []})

        events = list(
            streaming.stream_agent(
                _Agent(),
                {"messages": []},
                recursion_limit=7,
                config={"run_name": "x", "tags": ["t"]},
            )
        )
        self.assertTrue(any(e["type"] == "_done" for e in events))
        self.assertEqual(captured["config"]["recursion_limit"], 7)
        self.assertEqual(captured["config"]["run_name"], "x")


class TracingTurnSpanTests(TestCase):
    """The turn span records the parsed plan + rendered summary as outputs."""

    @staticmethod
    def _patch_tracing(runs):
        from contextlib import contextmanager
        from unittest.mock import patch

        from apps.core.services.AI.Agents.core import tracing

        class _FakeRun:
            def __init__(self):
                self.outputs = None
                self.ended = False

            def end(self, outputs=None, error=None):
                self.outputs = outputs
                self.ended = True

        @contextmanager
        def fake_trace(name, run_type="chain", inputs=None, tags=None,
                       metadata=None, **kwargs):
            run = _FakeRun()
            runs.append({
                "name": name, "inputs": inputs, "tags": tags,
                "metadata": metadata, "run": run,
            })
            yield run

        return (
            patch.object(tracing, "is_enabled", return_value=True),
            patch("langsmith.trace", fake_trace),
        )

    def test_plan_turn_span_carries_summary(self):
        import json
        from unittest.mock import patch

        from apps.core.services.AI.Agents.domains.product import manager as p

        runs = []
        enabled, trace_patch = self._patch_tracing(runs)

        class _Msg:
            def __init__(self, type_="ai", content=""):
                self.type = type_
                self.content = content

        class _Agent:
            def invoke(self, inp, config=None):
                return {
                    "messages": [
                        *inp["messages"],
                        _Msg("ai", json.dumps(
                            {"summary": "done", "plan": ["step"], "risks": []}
                        )),
                    ]
                }

        with enabled, trace_patch:
            agent = p.ProductMasterAgent()
            with patch.object(agent, "_get_agent", return_value=_Agent()):
                plan = agent.plan("do it")

        self.assertEqual(plan["summary"], "done")
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["name"], "ProductMasterAgent.plan")
        self.assertTrue(runs[0]["run"].ended)
        self.assertEqual(runs[0]["run"].outputs["plan"]["summary"], "done")
        self.assertIn("Summary: done", runs[0]["run"].outputs["summary"])

    def test_stream_turn_span_carries_summary(self):
        import json
        from unittest.mock import patch

        from apps.core.services.AI.Agents.domains.product import manager as p

        runs = []
        enabled, trace_patch = self._patch_tracing(runs)

        class _Msg:
            def __init__(self, type_="ai", content=""):
                self.type = type_
                self.content = content

        class _Agent:
            def stream(self, inp, config=None, stream_mode=None):
                final = {
                    "messages": [
                        *inp["messages"],
                        _Msg("ai", json.dumps({"summary": "ok"})),
                    ]
                }
                yield ("values", final)

        with enabled, trace_patch:
            agent = p.ProductMasterAgent()
            with patch.object(agent, "_get_agent", return_value=_Agent()):
                events = list(agent.stream("do it"))

        plan_events = [e for e in events if e["type"] == "plan"]
        self.assertEqual(len(plan_events), 1)
        self.assertEqual(plan_events[0]["plan"]["summary"], "ok")
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["name"], "ProductMasterAgent.stream")
        self.assertTrue(runs[0]["run"].ended)
        self.assertIn("Summary: ok", runs[0]["run"].outputs["summary"])

    def test_stream_agent_propagates_context_into_thread(self):
        import contextvars

        from apps.core.services.AI.Agents.core import streaming

        probe = contextvars.ContextVar("probe", default=None)
        seen = []
        probe.set("caller")

        class _Agent:
            def stream(self, inp, config=None, stream_mode=None):
                seen.append(probe.get())
                yield ("values", {"messages": []})

        events = list(streaming.stream_agent(_Agent(), {"messages": []}))
        self.assertTrue(any(e["type"] == "_done" for e in events))
        self.assertEqual(seen, ["caller"])


class ConversationThreadTests(TestCase):
    """Turns carry the replayed history and a shared LangSmith thread id."""

    @staticmethod
    def _patch_tracing(runs):
        from contextlib import contextmanager
        from unittest.mock import patch

        from apps.core.services.AI.Agents.core import tracing

        class _FakeRun:
            def __init__(self):
                self.outputs = None
                self.ended = False

            def end(self, outputs=None, error=None):
                self.outputs = outputs
                self.ended = True

        @contextmanager
        def fake_trace(name, run_type="chain", inputs=None, tags=None,
                       metadata=None, **kwargs):
            run = _FakeRun()
            runs.append({
                "name": name, "inputs": inputs, "tags": tags,
                "metadata": metadata, "run": run,
            })
            yield run

        return (
            patch.object(tracing, "is_enabled", return_value=True),
            patch("langsmith.trace", fake_trace),
        )

    def test_stream_trace_inputs_include_replayed_history(self):
        import json
        from unittest.mock import patch

        from apps.core.services.AI.Agents.domains.product import manager as p

        runs = []
        enabled, trace_patch = self._patch_tracing(runs)

        class _Msg:
            def __init__(self, type_="ai", content=""):
                self.type = type_
                self.content = content

        class _Agent:
            def stream(self, inp, config=None, stream_mode=None):
                final = {
                    "messages": [
                        *inp["messages"],
                        _Msg("ai", json.dumps({"summary": "ok"})),
                    ]
                }
                yield ("values", final)

        with enabled, trace_patch:
            agent = p.ProductMasterAgent()
            with patch.object(agent, "_get_agent", return_value=_Agent()):
                list(agent.stream("first question"))
                list(agent.stream("second question"))

        self.assertEqual(len(runs), 2)
        # First turn replays nothing.
        self.assertEqual(runs[0]["inputs"]["history_len"], 0)
        self.assertEqual(runs[0]["inputs"]["history"], [])
        # Second turn's trace input carries the actual prior conversation.
        self.assertEqual(runs[1]["inputs"]["history_len"], 2)
        history = runs[1]["inputs"]["history"]
        self.assertEqual([h["role"] for h in history], ["human", "ai"])
        self.assertIn("first question", history[0]["content"])
        self.assertIn("ok", history[1]["content"])

    def test_turn_config_and_identity_carry_session_id(self):
        from apps.core.services.AI.Agents.domains.product import manager as p

        agent = p.ProductMasterAgent(conversation_id="conv-abc")
        self.assertEqual(agent._run_identity("stream")["metadata"]["session_id"], "conv-abc")
        self.assertEqual(
            agent._turn_config()["metadata"]["session_id"], "conv-abc"
        )

    def test_reset_rotates_conversation_id(self):
        from apps.core.services.AI.Agents.core.memory import ConversationMemory

        mem = ConversationMemory()
        first = mem.conversation_id
        mem.reset()
        self.assertNotEqual(mem.conversation_id, first)
        self.assertEqual(mem.messages, [])

    def test_history_grows_across_turns(self):
        from apps.core.services.AI.Agents.core.router import GeneralManagerAgent

        agent = GeneralManagerAgent(pinned="product")
        calls = []

        class _Sub:
            def stream(self, task, history=None):
                calls.append(list(history or []))
                plan = {"summary": "done", "sql": [], "risks": []}
                yield {"type": "plan", "plan": plan, "text": "Summary: done"}

        agent._managers["product"] = _Sub()

        list(agent.stream("one"))
        list(agent.stream("two"))
        list(agent.stream("three"))

        self.assertEqual([len(c) for c in calls], [0, 2, 4])


