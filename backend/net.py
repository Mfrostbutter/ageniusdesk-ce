"""Shared outbound-network safety helpers.

Every module that fetches an operator- or user-influenced URL server-side must
route it through :func:`assert_safe_probe_url` first, and every ``httpx`` client
must pass :func:`tls_verify` so ``AGD_TLS_VERIFY`` is honored uniformly. These
lived in ``modules/assistant/providers.py``; they were promoted here so no module
has to cross-import from ``assistant`` to get the guard (see the 2026-07-01
cross-module review, findings C1/C2).
"""

from __future__ import annotations

import ipaddress
import logging
import os
import socket
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


class UnsafeProbeURL(ValueError):
    """Raised when an operator-supplied fetch URL targets a blocked host."""


def _egress_allow_networks() -> list:
    """Parse AGD_EGRESS_ALLOW_CIDRS into networks. Empty list = no allowlist."""
    from backend.config import settings

    raw = (settings.agd_egress_allow_cidrs or "").strip()
    if not raw:
        return []
    nets = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            nets.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            # A typo must not silently widen egress to everything, so an
            # unparseable entry is dropped loudly and the rest still applies.
            logger.warning("AGD_EGRESS_ALLOW_CIDRS: ignoring invalid entry %r", item)
    return nets


def assert_safe_probe_url(raw: str) -> str:
    """Validate an operator-supplied URL before the server fetches it.

    Self-hosted services (Ollama, MCP servers, Qdrant, LAN n8n) legitimately run
    on loopback or a private LAN/Docker host, so those ranges stay allowed by
    default — blocking RFC1918 would break the product's core use case. We block
    the SSRF targets that are never a real service: the cloud metadata endpoint
    and link-local space (169.254.0.0/16, fe80::/10), multicast, and
    reserved/unspecified addresses. Hostnames are resolved so a name pointing at
    metadata is caught too.

    An operator who wants a tighter boundary sets ``AGD_EGRESS_ALLOW_CIDRS``;
    then every resolved address must also fall inside one of those ranges, which
    turns "any private host" into "only the subnet my n8n fleet lives on".
    Returns the trimmed base URL (no trailing slash).
    """
    base = (raw or "").strip()
    if not base:
        raise UnsafeProbeURL("No URL provided")
    parsed = urlparse(base if "://" in base else f"http://{base}")
    if parsed.scheme not in ("http", "https"):
        raise UnsafeProbeURL("Only http/https URLs are allowed")
    host = parsed.hostname
    if not host:
        raise UnsafeProbeURL("URL has no host")
    try:
        addrinfos = socket.getaddrinfo(host, parsed.port or 80, proto=socket.IPPROTO_TCP)
    except OSError as e:
        raise UnsafeProbeURL(f"Host could not be resolved: {e}") from e
    allow_nets = _egress_allow_networks()
    for info in addrinfos:
        ip = ipaddress.ip_address(info[4][0])
        # Loopback is checked first and short-circuits: a service on localhost is
        # legitimate and not the SSRF risk, and IPv6 ::1 would otherwise trip the
        # is_reserved check below (it sits inside ::/8).
        if ip.is_loopback:
            continue
        if ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
            raise UnsafeProbeURL("Target address is not allowed")
        # Every resolved address must satisfy the allowlist: a name that resolves
        # to both an allowed and a disallowed address is not safe to fetch, since
        # which one httpx connects to is not ours to decide.
        if allow_nets and not any(ip in net for net in allow_nets):
            raise UnsafeProbeURL(
                "Target address is outside AGD_EGRESS_ALLOW_CIDRS"
            )
    return base.rstrip("/")


def tls_verify() -> bool:
    """Global TLS certificate verification default for outbound httpx clients.

    Default on; flip off only for self-signed services on a private LAN via
    ``AGD_TLS_VERIFY=false``. Every outbound client in the app should pass this
    as ``verify=`` so the flag behaves consistently.

    Prefer :func:`tls_verify_for_instance` on any call that targets a specific
    n8n instance: one self-signed box should not cost the whole fleet its cert
    checking.
    """
    val = os.environ.get("AGD_TLS_VERIFY", "true").strip().lower()
    return val not in ("0", "false", "no", "off")


def tls_verify_for_instance(inst: dict | None) -> bool:
    """TLS verification for one n8n instance.

    An instance carrying an explicit ``tls_verify`` wins; otherwise the global
    ``AGD_TLS_VERIFY`` applies. This exists so an operator with one self-signed
    LAN box can turn verification off for that box alone instead of setting
    AGD_TLS_VERIFY=false and silently disabling cert checks for every outbound
    call in the app, including the ones to public HTTPS providers.
    """
    if inst is not None and "tls_verify" in inst:
        return bool(inst["tls_verify"])
    return tls_verify()
