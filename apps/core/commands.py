"""Command registry shared by the CLI and the HTTP layer.

Each command is declared once with its name, aliases, parameter schema and
execution ``kind``.  The CLI uses this metadata to prompt for parameters and
print results; the web layer uses the same metadata to validate request bodies
and render forms (so the two entry points can never drift).

Parameter schema follows the existing convention::

    {param_name: [default_value, type, remark?]}
"""

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional, Tuple

# Execution modes:
#   sync    - fast, pure function that can run inside an HTTP request
#   job     - slow / file-producing task that should run in a worker
#   desktop - needs an unlocked interactive desktop session (kept CLI-only)
KIND_SYNC = "sync"
KIND_JOB = "job"
KIND_DESKTOP = "desktop"


@dataclass
class CommandSpec:
    name: str
    func: Callable[..., Any]
    aliases: Tuple[str, ...] = ()
    params: Dict[str, list] = field(default_factory=dict)
    kind: str = KIND_SYNC


# registry: command name -> CommandSpec
COMMANDS: Dict[str, CommandSpec] = {}


def command(name: Optional[str] = None, aliases: tuple = (), params: Optional[dict] = None, kind: str = KIND_SYNC):
    """Decorator that registers the wrapped callable as a command."""
    def decorator(func):
        cmd_name = name or func.__name__
        COMMANDS[cmd_name] = CommandSpec(
            name=cmd_name,
            func=func,
            aliases=tuple(dict.fromkeys((cmd_name, *aliases))),
            params=params or {},
            kind=kind,
        )
        return func

    return decorator


def match(raw_command: str) -> Optional[CommandSpec]:
    """Return the CommandSpec whose name/alias matches ``raw_command`` (or None).

    Leading ``-`` is tolerated because the CLI historically prefixed commands.
    """
    raw = (raw_command or "").strip().lstrip("-")
    for spec in COMMANDS.values():
        if raw in spec.aliases:
            return spec
    return None


def all_commands() -> Dict[str, CommandSpec]:
    """Return a snapshot of the registry (for introspection endpoints)."""
    return dict(COMMANDS)
