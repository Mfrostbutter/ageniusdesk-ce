"""Fleet-source registry: where a module publishes rows the Fleet Health roll-up
merges. Two contributor kinds land as one FleetSource:

  - provider (core built-ins): an async callable returning rows, run in-process.
  - pull (community modules): a health route the host PULLS over the same
    reverse-proxy transport that fronts the module (isolated tier: the worker
    client). The module never pushes; a dead/slow module simply reads as `down`.

In-memory singleton; sources register at module load / discovery and unregister
on uninstall. See docs/specs/2026-07-01-fleet-health-contribution-api.md.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# Default per-source result cache seconds (a rapid Fleet-Health poll shares one
# fetch rather than re-hitting every source).
DEFAULT_TTL = 25.0


@dataclass
class FleetSource:
    id: str
    kind: str                                          # 'provider' | 'pull'
    ttl: float = DEFAULT_TTL
    provider: Callable[[], Awaitable[list[dict]]] | None = None   # kind == 'provider'
    module_id: str = ""                                # kind == 'pull'
    route: str = "fleet-health"                        # kind == 'pull'
    # cache: {"t": monotonic, "rows": [...]}. Negative results are cached briefly too.
    cache: dict[str, Any] = field(default_factory=dict)


_sources: dict[str, FleetSource] = {}


def register_source(source_id: str, provider: Callable[[], Awaitable[list[dict]]], ttl: float = DEFAULT_TTL) -> None:
    """Register an in-process provider (core built-in). `provider` is an async
    callable returning a list of rows (see aggregator for the row schema)."""
    _sources[source_id] = FleetSource(id=source_id, kind="provider", provider=provider, ttl=ttl)
    logger.info("fleet-health: registered provider source %s", source_id)


def register_pull_source(module_id: str, route: str = "fleet-health", ttl: float = DEFAULT_TTL) -> None:
    """Register a community module the host pulls at `/api/{module_id}/{route}`."""
    sid = f"module:{module_id}"
    _sources[sid] = FleetSource(id=sid, kind="pull", module_id=module_id, route=route or "fleet-health", ttl=ttl)
    logger.info("fleet-health: registered pull source %s (route %s)", sid, route)


def unregister(source_id: str) -> None:
    _sources.pop(source_id, None)


def unregister_module(module_id: str) -> None:
    _sources.pop(f"module:{module_id}", None)


def all_sources() -> list[FleetSource]:
    return list(_sources.values())


def clear() -> None:
    """Test hook: drop every registered source."""
    _sources.clear()
