"""Resolve an OTLP trace's exporter to a configured AGD instance.

n8n's native OTel export carries no attribute that names or points at the AGD
instance it was configured under. Its resource identity is an opaque
``n8n.instance.id`` hash plus a container ``host.name`` and ``service.name=n8n``,
none of which match an AGD instance's name or URL. So the old ingest matcher
never matched and fell back to *the active instance*, stamping every observed
trace with whatever instance happened to be active at ingest time. That silently
mis-attributes per-instance counts, and it breaks cost/health enrichment (they
fetch run-data from the active instance's API, which does not have the foreign
execution, so enrichment reads empty and writes $0 / no health).

This module gives ingest a real mapping, in two tiers:

1. **Deterministic** — an AGD-provisioned n8n stamps ``agd.instance.name`` on its
   resource via ``OTEL_RESOURCE_ATTRIBUTES`` (see docker_mgr templates). Ingest
   matches that against the configured instance names, no probe needed.
2. **Learned** — for external / legacy instances with no stamp, the opaque
   ``n8n.instance.id`` hash is resolved *once* by probing each configured
   instance's API for the trace's execution id and confirming the workflow id
   matches (execution ids are small integers and collide across instances, so
   the workflow id is the disambiguator). The hash→instance result is pinned in
   ``otel_instance_map``; every later trace with that hash maps in O(1).

The mapping itself (``map_from_attrs``) is pure and synchronous so it can run in
the ingest flatten path; the async probe (``learn_unknowns``) runs after insert,
best-effort, and re-attributes the just-stored ``unknown-<hash>`` rows once the
hash resolves.
"""

from __future__ import annotations

import logging

import httpx

from backend.config import decrypt_value, get_instances
from backend.database import get_db
from backend.modules.n8n_proxy.client import dockerize_url

logger = logging.getLogger(__name__)

# Prefix for a resource whose instance is not yet known. Kept distinct from any
# real AGD instance id so these rows never contaminate a configured instance's
# metrics, and so learn_unknowns can find and re-attribute them by exact match.
UNKNOWN_PREFIX = "unknown-"

_PROBE_TIMEOUT = 4.0

# Process-local cache of the pin table (resource_hash -> instance_id). Loaded
# lazily and refreshed after a new pin so the sync mapper needs no DB call.
_pins: dict[str, str] | None = None


async def load_pins(force: bool = False) -> dict[str, str]:
    """Return the resource_hash -> instance_id pin map, cached in-process."""
    global _pins
    if _pins is not None and not force:
        return _pins
    db = await get_db()
    cur = await db.execute("SELECT resource_hash, instance_id FROM otel_instance_map")
    rows = await cur.fetchall()
    _pins = {r["resource_hash"]: r["instance_id"] for r in rows}
    return _pins


def invalidate() -> None:
    """Drop the in-process pin cache (next load_pins re-reads the table)."""
    global _pins
    _pins = None


async def pin(resource_hash: str, instance_id: str, source: str = "learned") -> None:
    """Persist a hash→instance mapping and refresh the cache. Upsert on hash."""
    if not resource_hash or not instance_id:
        return
    db = await get_db()
    await db.execute(
        """
        INSERT INTO otel_instance_map (resource_hash, instance_id, source)
        VALUES (?, ?, ?)
        ON CONFLICT(resource_hash) DO UPDATE SET
            instance_id = excluded.instance_id,
            source = excluded.source,
            learned_at = datetime('now')
        """,
        (resource_hash, instance_id, source),
    )
    await db.commit()
    if _pins is not None:
        _pins[resource_hash] = instance_id


def map_from_attrs(
    resource_attrs: dict,
    pins: dict[str, str],
    name_to_id: dict[str, str],
) -> str:
    """Pure, synchronous resource→instance mapping. Never touches the network.

    Order: deterministic stamp, then a learned pin, then the raw hash as a stable
    ``unknown-<hash>`` bucket. It deliberately does NOT fall back to the active
    instance: attributing a foreign trace to whoever is active is exactly the
    corruption this replaces. A resource with no hash at all (should not happen
    for real n8n) maps to a bare ``unknown`` bucket.
    """
    # 1. Deterministic stamp from an AGD-provisioned instance.
    stamped = str(resource_attrs.get("agd.instance.name") or "").strip().lower()
    if stamped and stamped in name_to_id:
        return name_to_id[stamped]
    stamped_id = str(resource_attrs.get("agd.instance.id") or "").strip()
    if stamped_id and stamped_id in name_to_id.values():
        return stamped_id

    # 2. Learned pin keyed on the stable n8n instance hash.
    rhash = str(resource_attrs.get("n8n.instance.id") or "").strip()
    if rhash and rhash in pins:
        return pins[rhash]

    # 3. A real exporter hash we do not recognize yet: keep a stable per-hash
    #    bucket so learn_unknowns can resolve and re-attribute it. Never active.
    if rhash:
        return f"{UNKNOWN_PREFIX}{rhash}"

    # 4. No instance identity at all (a degenerate/non-n8n exporter, or a test).
    #    Leave it unattributed (""), which downstream enrichment treats as
    #    "belongs to the default/active instance" — the pre-mapping behavior for
    #    single-instance installs. Real n8n always carries n8n.instance.id, so
    #    this branch is not hit by a genuine n8n exporter.
    return ""


async def _probe_instance(inst: dict, execution_id: str, workflow_id: str) -> bool:
    """True if this instance's n8n API holds ``execution_id`` for ``workflow_id``.

    Confirms the workflow id because execution ids are small integers that repeat
    across instances; matching the id alone would mis-resolve. Never raises: an
    unreachable / rejecting instance is simply not a match.
    """
    try:
        url = dockerize_url(decrypt_value(inst.get("url", ""))).rstrip("/")
        api_key = decrypt_value(inst.get("api_key", ""))
    except Exception:
        return False
    if not url:
        return False
    headers = {"X-N8N-API-KEY": api_key, "Accept": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT) as client:
            resp = await client.get(f"{url}/api/v1/executions/{execution_id}", headers=headers)
        if resp.status_code != 200:
            return False
        body = resp.json() or {}
    except Exception:
        return False
    if str(body.get("id", "")) != str(execution_id):
        return False
    # Match the workflow id when both sides have one; if the trace carried none,
    # fall back to id-existence (best effort).
    body_wf = str(body.get("workflowId", "") or (body.get("workflowData") or {}).get("id", ""))
    if workflow_id and body_wf:
        return body_wf == str(workflow_id)
    return True


async def resolve_hash(resource_hash: str, execution_id: str, workflow_id: str) -> str:
    """Resolve a hash to an instance id by probing every configured instance in
    parallel for the execution, or '' if none owns it. Does not pin."""
    import asyncio

    instances = get_instances()
    if not instances or not execution_id:
        return ""
    results = await asyncio.gather(
        *[_probe_instance(i, execution_id, workflow_id) for i in instances],
        return_exceptions=True,
    )
    matches = [i["id"] for i, ok in zip(instances, results) if ok is True]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        # Ambiguous (same exec id + workflow id on two instances is improbable but
        # possible with imported workflows); refuse to guess.
        logger.warning("instance_map: hash %s ambiguous across %s", resource_hash[:12], matches)
    return ""


async def learn_unknowns(unknown_hashes: dict[str, tuple[str, str]]) -> int:
    """Resolve and pin a batch of unknown hashes, re-attributing their spans.

    ``unknown_hashes`` maps resource_hash -> (execution_id, workflow_id) sampled
    from the just-ingested rows. For each unresolved hash, probe, pin, and
    UPDATE the ``unknown-<hash>`` spans to the resolved instance id. Returns the
    number of hashes newly pinned. Best-effort: a hash that will not resolve is
    left as its ``unknown-<hash>`` bucket to retry on the next trace.
    """
    pins = await load_pins()
    newly = 0
    for rhash, (exec_id, wf_id) in unknown_hashes.items():
        if not rhash or rhash in pins:
            continue
        inst_id = await resolve_hash(rhash, exec_id, wf_id)
        if not inst_id:
            continue
        await pin(rhash, inst_id, "learned")
        db = await get_db()
        await db.execute(
            "UPDATE otel_spans SET instance_id = ? WHERE instance_id = ?",
            (inst_id, f"{UNKNOWN_PREFIX}{rhash}"),
        )
        await db.commit()
        newly += 1
        logger.info("instance_map: learned %s -> %s, re-attributed its spans", rhash[:12], inst_id)
    return newly
