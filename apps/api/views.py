"""HTTP controllers (the "Controller" layer in MVC).

These views are thin: they translate an HTTP request into service parameters,
delegate to the registered operation, and return a JSON response.  All business
logic lives in :mod:`apps.core.services` / :mod:`apps.core.operations`.
"""

import json

from django.http import JsonResponse, StreamingHttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from apps.core import commands as registry
import apps.core.operations  # noqa: F401  (populates the command registry)
from apps.core.models import PriceQuote
from apps.core.services.AI.Agents.manager_product import ProductMasterAgent, format_plan
from apps.core.services.AI.Agents.worker_product_name import ProductNameWorker
from apps.core.services.AI.Agents.worker_web_research import WebResearchWorker
from apps.core.services.AI.Agents.worker_product_price import ProductPriceWorker
from apps.core.services.AI.Agents.worker_product_attributes import ProductAttributesWorker


def _json_body(request):
    try:
        return json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return {}


# Coerce raw request values to the expected Python types.
def _coerce(value, data_type):
    """Coerce a raw request value to the expected Python type."""
    if data_type is bool:
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("true", "1", "yes")
    if data_type is int:
        if value in ("", None):
            return 0
        return int(value)
    if data_type is float:
        if value in ("", None):
            return 0.0
        return float(value)
    if data_type is list:
        if value is None:
            return []
        return value if isinstance(value, list) else [value]
    if value is None:
        return ""
    return value


def _param_schema(param_spec):
    """Serialize a [default, type, remark?] spec into a JSON-safe list."""
    out = list(param_spec)
    if len(out) > 1 and isinstance(out[1], type):
        out[1] = out[1].__name__
    return out


@require_http_methods(["GET"])
def list_commands(request):
    """List every registered command with its parameter schema."""
    payload = {}
    for name, spec in registry.all_commands().items():
        payload[name] = {
            "name": spec.name,
            "aliases": list(spec.aliases),
            "kind": spec.kind,
            "params": {k: _param_schema(v) for k, v in spec.params.items()},
        }
    return JsonResponse({"commands": payload})


@csrf_exempt
@require_http_methods(["POST"])
def run_command(request, name):
    """Dispatch a command by name with parameters taken from the JSON body."""
    spec = registry.match(name)
    if spec is None:
        return JsonResponse({"error": f"unknown command: {name}"}, status=404)

    body = _json_body(request)
    params = {}
    for key, (default, data_type, *_rest) in spec.params.items():
        params[key] = _coerce(body.get(key, default), data_type)

    try:
        result = spec.func(**params)
    except Exception as exc:  # noqa: BLE001 - surface the error to the caller
        return JsonResponse({"error": str(exc)}, status=500)

    # Persist price quotes for reporting / admin.
    if spec.name == "price":
        PriceQuote.objects.create(
            qty_string=params.get("qtyString", ""),
            cost=params.get("cost", 0.0),
            profit_max=params.get("profit_max", 0.0),
            profit_min=params.get("profit_min", 0.0),
            result=result,
        )

    return JsonResponse({"command": spec.name, "result": result})


@csrf_exempt
@require_http_methods(["POST"])
def manager_product(request):
    """Run the read-only product-master agent and return a plan + SQL.

    Accepts JSON ``{"request": "...", "links": [...]}`` and returns both the
    structured ``plan`` and a human-readable ``text`` rendering.  The agent is
    read-only: it only reads the master schema / rows and emits a maintenance
    plan with SQL, never executing anything.
    """
    body = _json_body(request)
    task = body.get("request") or body.get("task") or body.get("message") or ""
    if not str(task).strip():
        return JsonResponse({"error": "missing 'request' field"}, status=400)

    links = body.get("links") or []
    if not isinstance(links, list):
        links = [links]

    images = body.get("images") or []
    if not isinstance(images, list):
        images = [images]

    # Streaming mode: ?stream=1 or body {"stream": true} -> NDJSON of events.
    want_stream = body.get("stream") is True or request.GET.get("stream") in (
        "1",
        "true",
    )
    if want_stream:

        def events():
            for event in ProductMasterAgent().stream(str(task), links, images):
                yield json.dumps(event, ensure_ascii=False) + "\n"

        response = StreamingHttpResponse(
            events(),
            content_type="application/x-ndjson",
        )
        response["X-Accel-Buffering"] = "no"
        return response

    try:
        plan = ProductMasterAgent().plan(str(task), links, images)
    except Exception as exc:  # noqa: BLE001 - surface the error to the caller
        return JsonResponse({"error": str(exc)}, status=500)

    return JsonResponse({"plan": plan, "text": format_plan(plan)})


@csrf_exempt
@require_http_methods(["POST"])
def product_name_worker(request):
    """Rename a Chinese product name into SEO English (optionally from photos)."""
    body = _json_body(request)
    name = body.get("name") or body.get("chinese_name") or ""
    if not str(name).strip():
        return JsonResponse({"error": "missing 'name' field"}, status=400)

    images = body.get("images") or []
    if not isinstance(images, list):
        images = [images]

    result = ProductNameWorker().rename(
        chinese_name=str(name),
        images=images,
        seo_keywords=body.get("seo_keywords"),
        seo_file=body.get("seo_file"),
        name_type=body.get("name_type", "seo"),
        tone=body.get("tone", "professional"),
        product_id=body.get("product_id"),
    )
    return JsonResponse(result)


@csrf_exempt
@require_http_methods(["POST"])
def web_research_worker(request):
    """Summarise one or more web pages against target fields."""
    body = _json_body(request)
    urls = body.get("urls") or []
    if not isinstance(urls, list):
        urls = [urls]
    result = WebResearchWorker().research(
        urls=urls,
        target_fields=body.get("target_fields"),
        question=body.get("question"),
    )
    return JsonResponse(result)


@csrf_exempt
@require_http_methods(["POST"])
def product_price_worker(request):
    """Define a product price and draft SQL from cost + research inputs."""
    body = _json_body(request)
    if "cost" not in body:
        return JsonResponse({"error": "missing 'cost' field"}, status=400)
    result = ProductPriceWorker().define_price(
        cost=body.get("cost"),
        qty_tiers=body.get("qty_tiers", "10,100,500"),
        profit_max=body.get("profit_max", 60.0),
        profit_min=body.get("profit_min", 30.0),
        currency=body.get("currency", "USD"),
        exchange_rate=body.get("exchange_rate"),
        selling_by_mode=body.get("selling_by_mode", "by_qty"),
        currency_id=body.get("currency_id"),
        product_id=body.get("product_id"),
        research_summary=body.get("research_summary"),
    )
    return JsonResponse(result)


@csrf_exempt
@require_http_methods(["POST"])
def product_attributes_worker(request):
    """Create / assign product attributes (plan + idempotent SQL text)."""
    body = _json_body(request)
    task = body.get("request") or body.get("task") or body.get("message") or ""
    if not str(task).strip():
        return JsonResponse({"error": "missing 'request' field"}, status=400)

    want_stream = body.get("stream") is True or request.GET.get("stream") in ("1", "true")
    if want_stream:

        def events():
            for event in ProductAttributesWorker().stream(str(task)):
                yield json.dumps(event, ensure_ascii=False) + "\n"

        response = StreamingHttpResponse(events(), content_type="application/x-ndjson")
        response["X-Accel-Buffering"] = "no"
        return response

    try:
        result = ProductAttributesWorker().plan(str(task))
    except Exception as exc:  # noqa: BLE001 - surface the error to the caller
        return JsonResponse({"error": str(exc)}, status=500)
    return JsonResponse(result)
