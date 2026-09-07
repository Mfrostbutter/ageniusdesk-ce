"""Host-side collector for module pane contributions.

A community module declares a pane it feeds via `manifest.contributes` (see
module_registry.ContributesDecl). The host fetches its rows through one
authenticated in-process request so the host pane shows module data next to the
native content, with the SAME code path in every isolation mode: for an
in-process module the ASGI call lands on its mounted router; for an isolated one
it lands on the reverse proxy, which forwards to the worker. Per-module timeout
and degrade-not-fatal, so one broken contributor never sinks the pane.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from backend import module_registry

logger = logging.getLogger(__name__)

# A contributor is a convenience surface, not the pane. Keep it short so a slow
# module degrades to "unreachable" instead of stalling the whole pane.
TIMEOUT_S = 6.0


def _fleet_health_contributors() -> list[tuple[str, str, str]]:
    """(module_id, module_name, subpath) for every loaded community module that
    declares a fleet-health contribution."""
    out: list[tuple[str, str, str]] = []
    for entry in module_registry.get_registry().values():
        if entry.source != "community" or entry.status != "loaded":
            continue
        contributes = entry.manifest.contributes
        sub = (contributes.fleet_health.strip("/") if contributes else "")
        if not sub:
            continue
        out.append((entry.manifest.id, entry.manifest.name, sub))
    return out


async def _fetch_rows(app: Any, module_id: str, name: str, sub: str) -> dict[str, Any]:
    """One authenticated self-call to a module's contribution endpoint.

    Returns a per-module summary carrying its rows, or an error and no rows.
    Never raises: a contributor failure is data, not an exception.
    """
    import httpx

    from backend.modules._runtime import identity

    base = {"module": module_id, "module_name": name}
    url = f"http://agd-internal/api/{module_id}/{sub}"
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, timeout=TIMEOUT_S) as client:
            resp = await client.get(url, headers=identity.internal_headers("viewer"))
        if resp.status_code != 200:
            return {**base, "reachable": False, "error": f"http {resp.status_code}", "rows": []}
        data = resp.json()
        rows = data.get("rows") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            return {**base, "reachable": False, "error": "malformed response", "rows": []}
        clean = [r for r in rows if isinstance(r, dict)]
        return {**base, "reachable": True, "error": None, "rows": clean}
    except (httpx.TimeoutException, asyncio.TimeoutError):
        return {**base, "reachable": False, "error": "timeout", "rows": []}
    except Exception as e:  # noqa: BLE001 - one bad contributor must not sink the pane
        logger.warning("fleet-health contribution from %s failed: %s", module_id, e)
        return {**base, "reachable": False, "error": str(e)[:120], "rows": []}


async def collect_fleet_health() -> dict[str, Any]:
    """Gather fleet-health rows from every contributing community module.

    Returns {"modules": [per-module summary], "rows": [row tagged with module]}.
    Each row is stamped with its source module id/name so the pane can badge it.
    """
    contributors = _fleet_health_contributors()
    if not contributors:
        return {"modules": [], "rows": []}

    from backend.main import app

    results = await asyncio.gather(
        *[_fetch_rows(app, mid, name, sub) for mid, name, sub in contributors]
    )

    rows: list[dict[str, Any]] = []
    for r in results:
        for row in r["rows"]:
            row.setdefault("module", r["module"])
            row.setdefault("module_name", r["module_name"])
            rows.append(row)
    return {"modules": results, "rows": rows}
