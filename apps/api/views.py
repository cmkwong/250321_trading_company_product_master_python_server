"""HTTP controllers (the "Controller" layer in MVC).

These views are thin: they translate an HTTP request into service parameters,
delegate to the registered operation, and return a JSON response.  All business
logic lives in :mod:`apps.core.services` / :mod:`apps.core.operations`.
"""

import json

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from apps.core import commands as registry
import apps.core.operations  # noqa: F401  (populates the command registry)
from apps.core.models import PriceQuote


def _json_body(request):
    try:
        return json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return {}


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
