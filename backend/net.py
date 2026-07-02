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
import os
import socket
from urllib.parse import urlparse


class UnsafeProbeURL(ValueError):
    """Raised when an operator-supplied fetch URL targets a blocked host."""


def assert_safe_probe_url(raw: str) -> str:
    """Validate an operator-supplied URL before the server fetches it.

    Self-hosted services (Ollama, MCP servers, Qdrant, LAN n8n) legitimately run
    on loopback or a private LAN/Docker host, so those ranges stay allowed. We
    block the SSRF targets that are never a real service: the cloud metadata
    endpoint and link-local space (169.254.0.0/16, fe80::/10), multicast, and
    reserved/unspecified addresses. Hostnames are resolved so a name pointing at
    metadata is caught too. Returns the trimmed base URL (no trailing slash).
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
    for info in addrinfos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_loopback:
            continue  # A service on localhost is legitimate and not the SSRF risk.
        if ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
            raise UnsafeProbeURL("Target address is not allowed")
    return base.rstrip("/")


def tls_verify() -> bool:
    """TLS certificate verification flag for outbound httpx clients.

    Default on; flip off only for self-signed services on a private LAN via
    ``AGD_TLS_VERIFY=false``. Every outbound client in the app should pass this
    as ``verify=`` so the flag behaves consistently.
    """
    val = os.environ.get("AGD_TLS_VERIFY", "true").strip().lower()
    return val not in ("0", "false", "no", "off")
