"""Per-kind search dispatch. Each kind implements `search(source, query, limit)`
returning a normalized `{results: [...], error?: str}` shape.

Currently only `qdrant` is implemented. Adding kinds: add an async function
and register it in DISPATCH. Keep the signature stable — the MCP tool and
HTTP router depend on the return shape.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

import httpx

from backend.config import _resolve_secret_ref
from backend.net import UnsafeProbeURL, assert_safe_probe_url, tls_verify

logger = logging.getLogger(__name__)


async def _openai_embed(text: str, api_key: str, model: str) -> list[float] | None:
    async with httpx.AsyncClient(verify=tls_verify(), timeout=20) as client:
        r = await client.post(
            "https://api.openai.com/v1/embeddings",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"input": text, "model": model},
        )
    if r.status_code != 200:
        logger.warning("openai embed %s: %s", r.status_code, r.text[:200])
        return None
    return r.json()["data"][0]["embedding"]


async def _voyage_embed(text: str, voyage_key: str) -> list[float] | None:
    async with httpx.AsyncClient(verify=tls_verify(), timeout=20) as client:
        r = await client.post(
            "https://api.voyageai.com/v1/embeddings",
            headers={"Authorization": f"Bearer {voyage_key}", "Content-Type": "application/json"},
            json={"input": [text], "model": "voyage-3", "input_type": "query"},
        )
    if r.status_code != 200:
        return None
    return r.json()["data"][0]["embedding"]


def _resolve(ref: str | None) -> str | None:
    """Resolve a $NAME secret reference, or pass through a plain value / env."""
    if not ref:
        return None
    if ref.startswith("$"):
        try:
            return _resolve_secret_ref(ref[1:])
        except Exception as e:
            logger.warning("knowledge: failed to resolve %s: %s", ref, e)
            return None
    return ref


async def _search_qdrant(source: dict[str, Any], query: str, limit: int) -> dict[str, Any]:
    cfg = source.get("config", {}) or {}
    url = (cfg.get("url") or "").rstrip("/")
    collection = cfg.get("collection")
    if not url or not collection:
        return {"results": [], "error": "qdrant source missing url or collection"}

    # Guard the operator-supplied Qdrant URL against SSRF before any fetch.
    try:
        url = assert_safe_probe_url(url)
    except UnsafeProbeURL as e:
        return {"results": [], "error": f"qdrant url blocked: {e}"}

    vector_name = cfg.get("vector_name", "dense")
    # Default: OpenAI text-embedding-3-large (3072d). All Qdrant
    # collections are built with this; using anything else yields dimension
    # mismatches. Override to "voyage" only for legacy voyage-3 collections.
    embedder = cfg.get("embedder", "openai")
    api_key = _resolve(cfg.get("api_key_secret"))

    if embedder == "openai":
        openai_key = _resolve(cfg.get("openai_key_secret")) or _resolve("$OPENAI_API_KEY")
        if not openai_key:
            return {"results": [], "error": "OPENAI_API_KEY not configured"}
        model = cfg.get("embed_model", "text-embedding-3-large")
        vec = await _openai_embed(query, openai_key, model)
        if not vec:
            return {"results": [], "error": f"openai embedding failed (model={model})"}
    elif embedder == "voyage":
        voyage_key = _resolve(cfg.get("voyage_key_secret")) or _resolve("$VOYAGE_API_KEY")
        if not voyage_key:
            return {"results": [], "error": "VOYAGE_API_KEY not configured"}
        vec = await _voyage_embed(query, voyage_key)
        if not vec:
            return {"results": [], "error": "voyage embedding failed"}
    else:
        return {"results": [], "error": f"unsupported embedder {embedder!r}"}

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["api-key"] = api_key

    payload: dict[str, Any] = {"query": vec, "limit": limit, "with_payload": True}
    if vector_name:
        payload["using"] = vector_name

    try:
        async with httpx.AsyncClient(verify=tls_verify(), timeout=20) as client:
            r = await client.post(
                f"{url}/collections/{collection}/points/query",
                headers=headers,
                json=payload,
            )
    except Exception as e:
        return {"results": [], "error": f"qdrant request failed: {e}"}
    # Do not reflect the response body: the URL is operator-supplied, so echoing
    # the target's response would make this a read primitive. Status only.
    if r.status_code >= 400:
        return {"results": [], "error": f"qdrant HTTP {r.status_code}"}

    points = r.json().get("result", {}).get("points", []) or []
    text_key = cfg.get("text_payload_key", "text")
    return {
        "results": [
            {
                "id": p.get("id"),
                "score": p.get("score"),
                "text": (p.get("payload") or {}).get(text_key, ""),
                "payload": p.get("payload") or {},
            }
            for p in points
        ]
    }


DISPATCH: dict[str, Callable[[dict[str, Any], str, int], Awaitable[dict[str, Any]]]] = {
    "qdrant": _search_qdrant,
}


async def search_source(source: dict[str, Any], query: str, limit: int) -> dict[str, Any]:
    kind = source.get("kind", "")
    fn = DISPATCH.get(kind)
    if fn is None:
        return {"results": [], "error": f"no backend for kind {kind!r}"}
    return await fn(source, query, limit)


async def probe(source: dict[str, Any]) -> dict[str, Any]:
    """Cheap health check — tries a trivial search and reports reachability."""
    result = await search_source(source, "ping", 1)
    return {"ok": "error" not in result, "error": result.get("error")}
