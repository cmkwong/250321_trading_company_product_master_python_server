import os, json

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django

django.setup()

from unittest.mock import patch
from django.test import Client

import apps.api.views as v


class _FakeAgent:
    def __init__(self, *a, **k):
        pass

    def stream(self, task, links=None):
        yield {"type": "status", "text": "calling tool: x"}
        yield {"type": "token", "text": "hello"}
        yield {
            "type": "plan",
            "plan": {"summary": "s", "sql": [], "risks": []},
            "text": "Summary: s",
        }

    def plan(self, task, links=None):
        return {"summary": "s", "plan": [], "sql": [], "missing_data": [], "risks": []}


c = Client()

# streaming via query param (no trailing slash)
with patch.object(v, "ProductMasterAgent", _FakeAgent):
    r = c.post(
        "/api/agents/manager_product?stream=1",
        data=json.dumps({"request": "do it"}),
        content_type="application/json",
        HTTP_HOST="127.0.0.1:8000",
    )
print("stream status:", r.status_code, "streaming:", r.streaming)
print("content-type:", r["Content-Type"])
body = b"".join(r.streaming_content)
lines = [l for l in body.decode("utf-8").splitlines() if l.strip()]
print("ndjson lines:", len(lines))
for l in lines:
    print("  ", l)

# non-stream default (no query param)
with patch.object(v, "ProductMasterAgent", _FakeAgent):
    r2 = c.post(
        "/api/agents/manager_product",
        data=json.dumps({"request": "do it"}),
        content_type="application/json",
        HTTP_HOST="127.0.0.1:8000",
    )
print("non-stream status:", r2.status_code, "streaming:", r2.streaming)
print("non-stream body:", r2.content.decode("utf-8"))
