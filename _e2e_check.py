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
