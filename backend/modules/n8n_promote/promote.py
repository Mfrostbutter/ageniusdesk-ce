"""Workflow promotion — move a workflow definition from one n8n instance to
another (dev -> staging -> prod), the OSS answer to n8n Enterprise environments.

Design constraints that shape this module:

  * n8n's Public REST API has NO "list credentials" endpoint (only POST, DELETE,
    and GET schema/{type}). So we cannot auto-discover a target instance's
    credentials by name. The caller supplies an explicit source-cred-id ->
    target-cred-id map; anything unmapped is surfaced LOUDLY, never imported
    silently broken. That "no silent failures" stance is the whole point.
  * The n8n client (backend.modules.n8n_proxy.client) is bound to the *active*
    instance. We aim it at an explicit instance with config.use_instance(), a
    context-local override — no client refactor needed.
  * A workflow's node.credentials block is {credType: {id, name}}. n8n resolves
    a node's credential by id, so on the target the id must point at a real
    credential there. We rewrite ids per the caller's map.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional

import httpx

from backend.config import decrypt_value, get_instance_by_id, load_secrets, use_instance
from backend.modules.n8n_credentials.known_types import detect_type_from_name
from backend.modules.n8n_credentials.mappings import build_credential_payload, fetch_live_schemas
from backend.modules.n8n_credentials.router import (
    _load_mirrors,
    _resolve_instance_creds,
    _resolve_secret,
    _save_mirrors,
    _schemas_for_instance,
)
from backend.modules.n8n_proxy import client
from backend.modules.n8n_proxy.client import _verify

logger = logging.getLogger(__name__)

# Fast liveness probe timeout. A single try, no retries — an unreachable
# source/target must fail the request in a few seconds, not hang the UI for
# ~30s of exponential-backoff retries against a dead instance.
_PROBE_TIMEOUT = 4.0


async def _probe_instance(inst: dict) -> tuple[bool, str]:
    """One-shot reachability + auth check for an instance. Returns (ok, detail).

    Deliberately does NOT use the retrying client — the point is to fail fast."""
    url = decrypt_value(inst.get("url", "") or "").rstrip("/")
    key = decrypt_value(inst.get("api_key", "") or "")
    if not url:
        return False, "no URL configured"
    try:
        async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT, verify=_verify()) as c:
            r = await c.get(f"{url}/api/v1/workflows",
                            headers={"X-N8N-API-KEY": key}, params={"limit": 1})
        if r.status_code == 200:
            return True, "ok"
        if r.status_code in (401, 403):
            return False, f"HTTP {r.status_code}: n8n rejected the API key."
        return False, f"HTTP {r.status_code}"
    except httpx.TimeoutException:
        return False, f"timed out after {_PROBE_TIMEOUT:.0f}s (is the instance running?)"
    except httpx.ConnectError:
        return False, "connection refused / host not found (is the instance running?)"
    except Exception as e:  # noqa: BLE001
        return False, str(e) or type(e).__name__


# ── Credential auto-provisioning ─────────────────────────────────────────────
#
# n8n's API never returns credential secret values, so a promoted workflow can't
# carry its credentials. Auto-provision closes that gap using AgeniusDesk's own
# Secrets store: for each credential a workflow needs, we (1) reuse a credential
# AGD already mirrored to the target of that type, else (2) create one from a
# uniquely-matching stored secret. Anything ambiguous or missing is surfaced,
# never guessed — the same no-silent-failures stance as the rest of promote.
# Credentials created here are recorded in the shared credential_mirrors.json so
# the Secrets UI and promote stay consistent.


async def _provision_credential(target: dict, secret_name: str, cred_type: str) -> tuple[str, str]:
    """Create a credential of `cred_type` on `target` from AGD secret
    `secret_name`. Returns (credential_id, credential_name). Idempotent: replaces
    any prior mirror of the same secret on this instance. Raises on failure."""
    tid = target["id"]
    url, api_key = _resolve_instance_creds(target)
    if not url or not api_key:
        raise ValueError("target instance is missing its URL or API key")
    schemas = await _schemas_for_instance(tid)
    secret_value = _resolve_secret(secret_name)  # decrypted str or compound dict
    payload = build_credential_payload(secret_name, secret_value, cred_type,
                                       schema=schemas.get(cred_type))
    mirrors = _load_mirrors()
    state = mirrors.setdefault(tid, {})
    async with httpx.AsyncClient(timeout=15.0, verify=_verify()) as c:
        prior = state.get(secret_name)
        if prior and prior.get("credential_id"):
            try:
                await c.delete(f"{url}/api/v1/credentials/{prior['credential_id']}",
                               headers={"X-N8N-API-KEY": api_key})
            except httpx.HTTPError:
                pass  # already gone in n8n — create the new one anyway
        r = await c.post(f"{url}/api/v1/credentials",
                         headers={"X-N8N-API-KEY": api_key, "Content-Type": "application/json"},
                         json=payload)
    if r.status_code >= 400:
        detail = r.text[:300]
        try:
            b = r.json(); detail = b.get("message") or b.get("detail") or detail
        except Exception:  # noqa: BLE001
            pass
        raise ValueError(f"n8n {r.status_code}: {detail}")
    body = r.json()
    cred_id = body.get("id") or ""
    cred_name = body.get("name") or payload["name"]
    state[secret_name] = {
        "credential_id": cred_id,
        "credential_name": cred_name,
        "credential_type": cred_type,
        "mirrored_at": datetime.utcnow().isoformat() + "Z",
    }
    mirrors[tid] = state
    _save_mirrors(mirrors)
    return cred_id, cred_name


async def resolve_target_credentials(
    target: dict,
    source_creds: list[dict],
    auto_provision: bool = True,
    secret_choices: Optional[dict[str, str]] = None,
) -> list[dict[str, Any]]:
    """Resolve each source credential to a target credential id.

    Priority per credential: explicit secret choice -> reuse an existing target
    mirror of the same type -> (auto) provision from the one stored secret whose
    name maps to that type. `>1` candidate is reported as ambiguous (not
    guessed); `0` is reported as no_secret. Returns a per-credential resolution
    list the UI turns into filled-in mapping rows + status notes."""
    secret_choices = secret_choices or {}
    tid = target["id"]

    existing = _load_mirrors().get(tid, {})
    by_type: dict[str, list[dict]] = {}
    for sname, m in existing.items():
        by_type.setdefault(m.get("credential_type"), []).append({
            "secret_name": sname,
            "credential_id": m.get("credential_id", ""),
            "credential_name": m.get("credential_name", ""),
        })

    stored = load_secrets()
    resolutions: list[dict[str, Any]] = []
    for c in source_creds:
        sid = c.get("source_id")
        ctype = c.get("cred_type")
        if not sid:
            continue
        res: dict[str, Any] = {"source_id": sid, "cred_type": ctype, "source_name": c.get("name", "")}
        choice = secret_choices.get(sid)
        try:
            if choice:
                cid, cname = await _provision_credential(target, choice, ctype)
                res.update(method="provisioned", target_id=cid, target_name=cname,
                           secret=choice, note=f"created on target from ${choice}")
            elif ctype in by_type and by_type[ctype]:
                m = by_type[ctype][0]
                res.update(method="reused", target_id=m["credential_id"],
                           target_name=m["credential_name"],
                           note=f"reused existing credential (from ${m['secret_name']})")
            elif auto_provision:
                cands = [n for n in stored if detect_type_from_name(n) == ctype]
                if len(cands) == 1:
                    cid, cname = await _provision_credential(target, cands[0], ctype)
                    res.update(method="provisioned", target_id=cid, target_name=cname,
                               secret=cands[0], note=f"created on target from ${cands[0]}")
                elif not cands:
                    res.update(method="no_secret", target_id="", candidates=[],
                               note=f"no stored secret matches type '{ctype}' — add one in Secrets")
                else:
                    res.update(method="ambiguous", target_id="", candidates=cands,
                               note=f"{len(cands)} candidate secrets — pick one: {', '.join(cands)}")
            else:
                res.update(method="manual", target_id="", note="")
        except Exception as e:  # noqa: BLE001
            logger.exception("auto-provision failed for cred %s (%s)", sid, ctype)
            res.update(method="error", target_id="", note=str(e))
        resolutions.append(res)
    return resolutions


async def auto_provision_credentials(
    target_instance_id: str,
    credentials: list[dict],
    secret_choices: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    """Service entry for the /auto-provision endpoint: resolve (and create where
    possible) target credentials for a set of source credentials."""
    target = get_instance_by_id(target_instance_id)
    if not target:
        return {"ok": False, "error": f"Target instance {target_instance_id} not found."}
    ok_t, msg_t = await _probe_instance(target)
    if not ok_t:
        return {"ok": False, "error": f"Target instance '{target.get('name', '')}' is unreachable: {msg_t}"}
    resolutions = await resolve_target_credentials(
        target, credentials, auto_provision=True, secret_choices=secret_choices
    )
    provisioned = sum(1 for r in resolutions if r.get("method") == "provisioned")
    reused = sum(1 for r in resolutions if r.get("method") == "reused")
    unresolved = [r for r in resolutions if not r.get("target_id")]
    return {
        "ok": True,
        "target": {"id": target["id"], "name": target.get("name", "")},
        "resolutions": resolutions,
        "provisioned": provisioned,
        "reused": reused,
        "unresolved": len(unresolved),
    }


def _extract_node_credentials(workflow: dict) -> list[dict[str, str]]:
    """Return the distinct credentials a workflow's nodes reference.

    Each entry: {"cred_type": <n8n cred type key>, "name": <display name>,
    "source_id": <id on the source instance>}. Deduped by (cred_type, source_id).
    """
    seen: set[tuple[str, str]] = set()
    out: list[dict[str, str]] = []
    for node in workflow.get("nodes") or []:
        creds = node.get("credentials") or {}
        for cred_type, ref in creds.items():
            if not isinstance(ref, dict):
                continue
            source_id = str(ref.get("id", "") or "")
            key = (cred_type, source_id)
            if key in seen:
                continue
            seen.add(key)
            out.append({
                "cred_type": cred_type,
                "name": str(ref.get("name", "") or ""),
                "source_id": source_id,
            })
    return out


def _rewrite_node_credentials(
    workflow: dict,
    cred_map: dict[str, str],
    cred_names: Optional[dict[str, str]] = None,
) -> tuple[dict, list[dict[str, str]]]:
    """Rewrite each node's credential id per cred_map (source_id -> target_id).

    Returns (workflow, unmapped) where `unmapped` lists creds with no target id.
    Mutates a shallow-copied nodes list; the input workflow is not modified.
    """
    cred_names = cred_names or {}
    wf = dict(workflow)
    new_nodes = []
    unmapped: list[dict[str, str]] = []
    unmapped_seen: set[tuple[str, str]] = set()
    for node in wf.get("nodes") or []:
        node = dict(node)
        creds = node.get("credentials") or {}
        if creds:
            new_creds = {}
            for cred_type, ref in creds.items():
                if not isinstance(ref, dict):
                    new_creds[cred_type] = ref
                    continue
                source_id = str(ref.get("id", "") or "")
                target_id = cred_map.get(source_id)
                if target_id:
                    new_ref = {"id": target_id,
                               "name": cred_names.get(source_id) or ref.get("name", "")}
                    new_creds[cred_type] = new_ref
                else:
                    # Leave the source ref in place but flag it — the workflow
                    # will visibly fail on this node until the cred is linked.
                    new_creds[cred_type] = ref
                    key = (cred_type, source_id)
                    if key not in unmapped_seen:
                        unmapped_seen.add(key)
                        unmapped.append({
                            "cred_type": cred_type,
                            "name": str(ref.get("name", "") or ""),
                            "source_id": source_id,
                        })
            node["credentials"] = new_creds
        new_nodes.append(node)
    wf["nodes"] = new_nodes
    return wf, unmapped


async def _target_supported_cred_types(target: dict) -> set[str]:
    """The credential types the target instance actually ships (schema 200)."""
    url = decrypt_value(target.get("url", ""))
    key = decrypt_value(target.get("api_key", ""))
    schemas = await fetch_live_schemas(url, key)
    return set(schemas.keys())


async def _target_workflow_names(target: dict) -> dict[str, str]:
    """name.lower() -> workflow_id for every workflow on the target (dup detect)."""
    names: dict[str, str] = {}
    with use_instance(target):
        cursor = ""
        for _ in range(200):
            page = await client.list_workflows(limit=250, cursor=cursor)
            for w in page.get("workflows", []) or []:
                nm = (w.get("name") or "").strip().lower()
                if nm:
                    names[nm] = w.get("id", "")
            cursor = page.get("next_cursor") or ""
            if not cursor:
                break
    return names


async def list_instance_workflows(instance_id: str) -> dict[str, Any]:
    """List every workflow on an explicit instance (source picker in the UI).

    Pages through all workflows so inactive ones show up too — promote is most
    useful for workflows that are NOT yet live on the target.
    """
    inst = get_instance_by_id(instance_id)
    if not inst:
        return {"ok": False, "error": f"Instance {instance_id} not found."}
    items: list[dict] = []
    with use_instance(inst):
        cursor = ""
        for _ in range(200):
            page = await client.list_workflows(limit=250, cursor=cursor)
            items.extend(page.get("workflows", []) or [])
            cursor = page.get("next_cursor") or ""
            if not cursor:
                break
    return {"ok": True, "instance": {"id": inst["id"], "name": inst.get("name", "")}, "workflows": items}


async def preflight(
    source_instance_id: str,
    target_instance_id: str,
    workflow_ids: list[str],
) -> dict[str, Any]:
    """Inspect what a promotion would do, without writing anything.

    Per workflow: the credentials it needs, whether the target instance supports
    each credential *type*, and whether a same-named workflow already exists on
    the target (would create a duplicate). Also returns the flat, deduped set of
    source credentials the caller must map before running.
    """
    source = get_instance_by_id(source_instance_id)
    target = get_instance_by_id(target_instance_id)
    if not source:
        return {"ok": False, "error": f"Source instance {source_instance_id} not found."}
    if not target:
        return {"ok": False, "error": f"Target instance {target_instance_id} not found."}
    if source_instance_id == target_instance_id:
        return {"ok": False, "error": "Source and target instances must differ."}

    ok_s, msg_s = await _probe_instance(source)
    if not ok_s:
        return {"ok": False, "error": f"Source instance '{source.get('name', '')}' is unreachable: {msg_s}"}
    ok_t, msg_t = await _probe_instance(target)
    if not ok_t:
        return {"ok": False, "error": f"Target instance '{target.get('name', '')}' is unreachable: {msg_t}"}

    supported_types = await _target_supported_cred_types(target)
    target_names = await _target_workflow_names(target)

    plans: list[dict[str, Any]] = []
    all_creds: dict[str, dict[str, str]] = {}  # source_id -> cred info
    for wf_id in workflow_ids:
        with use_instance(source):
            wf = await client.export_workflow(wf_id)
        if not wf:
            plans.append({"workflow_id": wf_id, "ok": False, "error": "Not found on source."})
            continue
        creds = _extract_node_credentials(wf)
        for c in creds:
            c = dict(c)
            c["type_supported_on_target"] = (c["cred_type"] in supported_types) if supported_types else None
            all_creds.setdefault(c["source_id"] or f"__noid__{c['cred_type']}", c)
        name = wf.get("name", "")
        dup_id = target_names.get((name or "").strip().lower())
        plans.append({
            "workflow_id": wf_id,
            "ok": True,
            "name": name,
            "active": bool(wf.get("active")),
            "node_count": len(wf.get("nodes") or []),
            "tags": [t.get("name", "") for t in (wf.get("tags") or [])],
            "credentials": creds,
            "duplicate_on_target": bool(dup_id),
            "duplicate_target_id": dup_id or "",
        })

    return {
        "ok": True,
        "source": {"id": source["id"], "name": source.get("name", "")},
        "target": {"id": target["id"], "name": target.get("name", "")},
        "workflows": plans,
        # Flat, deduped credential list the UI turns into a mapping form.
        "credentials_to_map": list(all_creds.values()),
        "target_supports_schema_introspection": bool(supported_types),
    }


async def promote(
    source_instance_id: str,
    target_instance_id: str,
    workflow_ids: list[str],
    cred_map: Optional[dict[str, str]] = None,
    cred_names: Optional[dict[str, str]] = None,
    activate: bool = False,
    name_suffix: str = "",
    dry_run: bool = False,
) -> dict[str, Any]:
    """Export each workflow from the source and import it onto the target,
    remapping credential ids per cred_map. Each workflow is independent — one
    failure never aborts the rest. Unmapped credentials are reported per
    workflow (loud), not swallowed.
    """
    cred_map = cred_map or {}
    cred_names = cred_names or {}
    source = get_instance_by_id(source_instance_id)
    target = get_instance_by_id(target_instance_id)
    if not source:
        return {"ok": False, "error": f"Source instance {source_instance_id} not found."}
    if not target:
        return {"ok": False, "error": f"Target instance {target_instance_id} not found."}
    if source_instance_id == target_instance_id:
        return {"ok": False, "error": "Source and target instances must differ."}

    ok_s, msg_s = await _probe_instance(source)
    if not ok_s:
        return {"ok": False, "error": f"Source instance '{source.get('name', '')}' is unreachable: {msg_s}"}
    ok_t, msg_t = await _probe_instance(target)
    if not ok_t:
        return {"ok": False, "error": f"Target instance '{target.get('name', '')}' is unreachable: {msg_t}"}

    results: list[dict[str, Any]] = []
    for wf_id in workflow_ids:
        try:
            with use_instance(source):
                wf = await client.export_workflow(wf_id)
            if not wf:
                results.append({"workflow_id": wf_id, "success": False, "error": "Not found on source."})
                continue

            src_name = wf.get("name", "Workflow")
            tags = [t.get("name", "") for t in (wf.get("tags") or []) if t.get("name")]
            rewritten, unmapped = _rewrite_node_credentials(wf, cred_map, cred_names)
            new_name = f"{src_name}{name_suffix}" if name_suffix else src_name

            if dry_run:
                results.append({
                    "workflow_id": wf_id,
                    "success": True,
                    "dry_run": True,
                    "name": new_name,
                    "tags": tags,
                    "unmapped_credentials": unmapped,
                    "would_activate": activate and not unmapped,
                })
                continue

            with use_instance(target):
                imported = await client.import_workflow(rewritten, name_override=new_name, tags=tags)

            if not imported.get("success"):
                results.append({
                    "workflow_id": wf_id, "success": False,
                    "error": imported.get("error", "import failed"),
                })
                continue

            new_id = imported.get("workflow_id", "")
            activated = False
            activation_error = ""
            # Refuse to activate a workflow with unmapped credentials — that is
            # exactly the green-but-broken state we exist to prevent.
            if activate and new_id:
                if unmapped:
                    activation_error = (
                        f"Not activated: {len(unmapped)} credential(s) unmapped "
                        "on target. Link them, then activate."
                    )
                else:
                    with use_instance(target):
                        act = await client.set_workflow_active(new_id, True)
                    activated = bool(act.get("success"))
                    if not activated:
                        activation_error = act.get("error", "activation failed")

            results.append({
                "workflow_id": wf_id,
                "success": True,
                "target_workflow_id": new_id,
                "name": imported.get("name", new_name),
                "tags_applied": imported.get("tags_applied", []),
                "unmapped_credentials": unmapped,
                "activated": activated,
                "activation_error": activation_error,
                "warning": imported.get("warning", ""),
            })
        except Exception as e:  # one workflow's blow-up never kills the batch
            logger.exception("promote workflow %s failed", wf_id)
            results.append({"workflow_id": wf_id, "success": False, "error": str(e)})

    ok_count = sum(1 for r in results if r.get("success"))
    return {
        "ok": True,
        "dry_run": dry_run,
        "source": {"id": source["id"], "name": source.get("name", "")},
        "target": {"id": target["id"], "name": target.get("name", "")},
        "promoted": ok_count,
        "failed": len(results) - ok_count,
        "results": results,
    }
