"""Worker and domain registries.

``WORKERS`` maps a worker name to its class (used by the HTTP dispatcher and the
manager's tool list); ``DOMAINS`` maps a domain name to a manager factory.  Both
are populated at import time by the package ``__init__`` via the decorators so
this module stays free of import cycles.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Type

WORKERS: Dict[str, Type[Any]] = {}
DOMAINS: Dict[str, Callable[..., Any]] = {}


def register_worker(name: str) -> Callable[[Type[Any]], Type[Any]]:
    """Register a worker class under ``name``."""

    def _decorator(cls: Type[Any]) -> Type[Any]:
        WORKERS[name] = cls
        return cls

    return _decorator


def register_domain(name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Register a domain manager factory under ``name``."""

    def _decorator(factory: Callable[..., Any]) -> Callable[..., Any]:
        DOMAINS[name] = factory
        return factory

    return _decorator


__all__ = ["WORKERS", "DOMAINS", "register_worker", "register_domain"]
