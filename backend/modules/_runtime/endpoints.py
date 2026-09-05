"""Effective endpoint config for the http.request bridge.

The manifest only proposes an endpoint's base URL, methods, and TLS policy. The
host persists what the operator actually consented to, per (module, endpoint),
as a revision in data/module-endpoints.json. The bridge reads the revision at
call time, so the manifest is never the runtime source of truth and a config
change needs no worker restart.

Pinned IPs are resolved when a revision is created (and on explicit re-pin); the
bridge dials only those addresses. A hostname that later resolves elsewhere
fails closed until an operator re-pins.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import socket
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from backend.module_registry import (
    MUTATING_METHODS,
    HttpAuthDecl,
    HttpEndpointDecl,
    ModuleManifest,
    normalize_base_url,
    normalize_methods,
)

logger = logging.getLogger(__name__)

STORE_FILE = Path("data/module-endpoints.json")
_lock = threading.RLock()

STATUS_ACTIVE = "active"
STATUS_PENDING = "pending"


class EndpointConfigError(ValueError):
    """Operator-facing configuration error."""


# ── persistence ───────────────────────────────────────────────────────────────


def _load() -> dict[str, dict[str, dict[str, Any]]]:
    if STORE_FILE.exists():
        try:
            data = json.loads(STORE_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception:
            logger.warning("module-endpoints.json unreadable; treating as empty")
    return {}


def _save(data: dict) -> None:
    STORE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STORE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(STORE_FILE)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ── pinning ───────────────────────────────────────────────────────────────────


def host_of(base_url: str) -> tuple[str, int, str]:
    """(hostname, port, scheme) of a base URL."""
    p = urlsplit(base_url)
    port = p.port or (443 if p.scheme == "https" else 80)
    return (p.hostname or "").lower(), port, p.scheme


def is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def resolve_ips(host: str, port: int) -> list[str]:
    """Resolve a host to its IP set (sorted, de-duplicated). Empty on failure."""
    if is_ip_literal(host):
        return [host]
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, OSError):
        return []
    ips: list[str] = []
    for info in infos:
        ip = info[4][0]
        if ip not in ips:
            ips.append(ip)
    return sorted(ips)


# ── revision shaping ──────────────────────────────────────────────────────────


def effective_methods(declared: list[str], requested: list[str] | None) -> list[str]:
    """Reduce `requested` to a subset of `declared`. HEAD rides with GET."""
    decl = set(normalize_methods(declared))
    if "GET" in decl:
        decl.add("HEAD")
    if requested is None:
        return sorted(decl)
    req = set(normalize_methods(requested))
    extra = req - decl
    if extra:
        raise EndpointConfigError(f"methods {sorted(extra)} are not declared by the module")
    if "GET" in req:
        req.add("HEAD")
    return sorted(req)


def _build_revision(
    decl: HttpEndpointDecl,
    *,
    base_url: str,
    methods: list[str],
    verify_tls: bool,
    status: str,
    consented_by: str,
    revision: int,
) -> dict[str, Any]:
    host, port, _ = host_of(base_url)
    pinned = resolve_ips(host, port) if status == STATUS_ACTIVE else []
    return {
        "revision": revision,
        "status": status,
        "base_url": base_url,
        "declared_methods": list(decl.methods),
        "methods": methods,
        "verify_tls": bool(verify_tls),
        "auth": decl.auth.model_dump() if decl.auth else None,
        "pinned_host": host,
        "pinned_ips": pinned,
        "consented_by": consented_by,
        "consented_at": _now() if status == STATUS_ACTIVE else "",
    }


def _declared(manifest: ModuleManifest) -> list[HttpEndpointDecl]:
    caps = manifest.capabilities
    if not caps or not caps.host.http.enabled:
        return []
    return list(caps.host.http.endpoints)


# ── public API ────────────────────────────────────────────────────────────────


def get(module_id: str, endpoint_id: str) -> dict[str, Any] | None:
    with _lock:
        return _load().get(module_id, {}).get(endpoint_id)


def list_for(module_id: str) -> dict[str, dict[str, Any]]:
    with _lock:
        return dict(_load().get(module_id, {}))


def seed(
    manifest: ModuleManifest,
    overrides: dict[str, dict[str, Any]] | None = None,
    *,
    consented_by: str = "",
    activate: bool = True,
) -> dict[str, dict[str, Any]]:
    """Create revisions for every declared endpoint from manifest defaults plus
    operator overrides `{eid: {base_url, methods, verify_tls}}`. With
    activate=False (registration of a pre-existing install) missing endpoints
    are seeded PENDING; existing revisions are left untouched."""
    overrides = overrides or {}
    with _lock:
        data = _load()
        mod = data.setdefault(manifest.id, {})
        for decl in _declared(manifest):
            if decl.id in mod and not activate:
                continue
            ov = overrides.get(decl.id, {})
            base_url = normalize_base_url(ov.get("base_url") or decl.base_url)
            methods = effective_methods(decl.methods, ov.get("methods"))
            verify_tls = bool(ov.get("verify_tls", decl.verify_tls))
            prev = mod.get(decl.id) or {}
            mod[decl.id] = _build_revision(
                decl,
                base_url=base_url,
                methods=methods,
                verify_tls=verify_tls,
                status=STATUS_ACTIVE if activate else STATUS_PENDING,
                consented_by=consented_by,
                revision=int(prev.get("revision", 0)) + 1,
            )
            # Preserve declared auth on re-seed; secrets are never stored here.
        # Drop revisions for endpoints the manifest no longer declares.
        declared_ids = {d.id for d in _declared(manifest)}
        for eid in list(mod):
            if eid not in declared_ids:
                mod.pop(eid)
        if not mod:
            data.pop(manifest.id, None)
        _save(data)
        return dict(mod)


def needs_consent(current: dict[str, Any], *, base_url: str, methods: list[str], verify_tls: bool) -> list[str]:
    """Reasons an update requires explicit operator consent (empty = none)."""
    reasons: list[str] = []
    if current.get("status") != STATUS_ACTIVE:
        reasons.append("endpoint is not yet confirmed")
    if host_of(current.get("base_url", "")) != host_of(base_url):
        reasons.append("host, scheme, or port changed")
    if current.get("verify_tls", True) and not verify_tls:
        reasons.append("TLS verification disabled")
    added = set(methods) - set(current.get("methods", []))
    if added & MUTATING_METHODS:
        reasons.append(f"mutating methods added: {sorted(added & MUTATING_METHODS)}")
    return reasons


def update(
    manifest: ModuleManifest,
    endpoint_id: str,
    *,
    base_url: str | None = None,
    methods: list[str] | None = None,
    verify_tls: bool | None = None,
    consent: bool = False,
    consented_by: str = "",
) -> dict[str, Any]:
    """Operator update. Any host/TLS/method widening starts a new consented
    revision (requires consent=True); a reduction is applied in place as a new
    revision without the consent flag."""
    decl = next((d for d in _declared(manifest) if d.id == endpoint_id), None)
    if decl is None:
        raise EndpointConfigError(f"endpoint {endpoint_id!r} is not declared by module {manifest.id!r}")
    with _lock:
        data = _load()
        mod = data.setdefault(manifest.id, {})
        current = mod.get(endpoint_id) or _build_revision(
            decl, base_url=decl.base_url, methods=effective_methods(decl.methods, None),
            verify_tls=decl.verify_tls, status=STATUS_PENDING, consented_by="", revision=0,
        )
        new_base = normalize_base_url(base_url) if base_url is not None else current["base_url"]
        new_methods = effective_methods(decl.methods, methods) if methods is not None else list(current["methods"])
        new_verify = bool(verify_tls) if verify_tls is not None else bool(current["verify_tls"])
        reasons = needs_consent(current, base_url=new_base, methods=new_methods, verify_tls=new_verify)
        if reasons and not consent:
            raise EndpointConfigError("consent required: " + "; ".join(reasons))
        mod[endpoint_id] = _build_revision(
            decl,
            base_url=new_base,
            methods=new_methods,
            verify_tls=new_verify,
            status=STATUS_ACTIVE,
            consented_by=consented_by or current.get("consented_by", ""),
            revision=int(current.get("revision", 0)) + 1,
        )
        _save(data)
        return dict(mod[endpoint_id])


def repin(module_id: str, endpoint_id: str) -> dict[str, Any]:
    """Re-resolve and pin the endpoint host (operator action after an IP change)."""
    with _lock:
        data = _load()
        rev = data.get(module_id, {}).get(endpoint_id)
        if rev is None:
            raise EndpointConfigError(f"no revision for {module_id}/{endpoint_id}")
        host, port, _ = host_of(rev["base_url"])
        rev["pinned_host"] = host
        rev["pinned_ips"] = resolve_ips(host, port)
        rev["revision"] = int(rev.get("revision", 0)) + 1
        _save(data)
        return dict(rev)


def remove_module(module_id: str) -> None:
    with _lock:
        data = _load()
        if data.pop(module_id, None) is not None:
            _save(data)


def summary(module_id: str, endpoint_ids: list[str] | None = None) -> list[dict[str, Any]]:
    """Worker/UI-facing view: no secret refs, no pinned IPs, host only."""
    out = []
    for eid, rev in sorted(list_for(module_id).items()):
        if endpoint_ids is not None and eid not in endpoint_ids:
            continue
        host, port, scheme = host_of(rev.get("base_url", ""))
        out.append({
            "id": eid,
            "status": rev.get("status", STATUS_PENDING),
            "methods": list(rev.get("methods", [])),
            "declared_methods": list(rev.get("declared_methods", [])),
            "verify_tls": bool(rev.get("verify_tls", True)),
            "host": f"{scheme}://{host}:{port}",
            "revision": int(rev.get("revision", 0)),
            "pinned": bool(rev.get("pinned_ips")),
        })
    return out


def operator_view(module_id: str) -> list[dict[str, Any]]:
    """Module-manager view: everything except the secret value (which is never
    stored). Includes base_url, pinned IPs, and the auth secret NAME."""
    out = []
    for eid, rev in sorted(list_for(module_id).items()):
        auth = rev.get("auth") or {}
        out.append({
            "id": eid,
            "status": rev.get("status", STATUS_PENDING),
            "revision": int(rev.get("revision", 0)),
            "base_url": rev.get("base_url", ""),
            "methods": list(rev.get("methods", [])),
            "declared_methods": list(rev.get("declared_methods", [])),
            "verify_tls": bool(rev.get("verify_tls", True)),
            "pinned_host": rev.get("pinned_host", ""),
            "pinned_ips": list(rev.get("pinned_ips", [])),
            "secret_ref": auth.get("secret_ref", ""),
            "auth_type": auth.get("type", ""),
            "consented_by": rev.get("consented_by", ""),
            "consented_at": rev.get("consented_at", ""),
        })
    return out


def auth_decl(rev: dict[str, Any]) -> HttpAuthDecl | None:
    raw = rev.get("auth")
    return HttpAuthDecl(**raw) if raw else None
