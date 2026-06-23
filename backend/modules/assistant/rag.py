"""Qdrant RAG module — optional vector search for AI assistant context enrichment."""

import logging

import httpx

from backend.config import decrypt_value, load_config

logger = logging.getLogger(__name__)


def _get_rag_config() -> dict:
    config = load_config()
    ai = config.get("assistant", {})
    return {
        "qdrant_url": decrypt_value(ai.get("qdrant_url", "")),
        "collection": ai.get("qdrant_collection", ""),
    }


def is_rag_enabled() -> bool:
    cfg = _get_rag_config()
    return bool(cfg["qdrant_url"] and cfg["collection"])


async def search(query: str, limit: int = 5) -> list[dict]:
    """Search Qdrant for relevant documents. Returns list of {text, score, metadata}."""
    cfg = _get_rag_config()
    if not cfg["qdrant_url"] or not cfg["collection"]:
        return []

    # Use Qdrant's scroll with text matching (no embedding needed — lightweight)
    # For full semantic search, users would need an embedding endpoint configured
    url = f"{cfg['qdrant_url'].rstrip('/')}/collections/{cfg['collection']}/points/scroll"

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json={
                "limit": limit,
                "with_payload": True,
                "with_vector": False,
                "filter": {
                    "should": [
                        {"key": "text", "match": {"text": query}},
                        {"key": "content", "match": {"text": query}},
                    ]
                },
            })
            if resp.status_code != 200:
                logger.warning("Qdrant search failed: HTTP %s", resp.status_code)
                return []

            data = resp.json()
            results = []
            for point in data.get("result", {}).get("points", []):
                payload = point.get("payload", {})
                text = payload.get("text") or payload.get("content") or payload.get("chunk", "")
                results.append({
                    "text": text[:500],
                    "metadata": {k: v for k, v in payload.items() if k not in ("text", "content", "chunk", "vector")},
                })
            return results
    except Exception as e:
        logger.warning("Qdrant search error: %s", e)
        return []


async def build_rag_context(query: str) -> str:
    """Search Qdrant and build a context string for the LLM."""
    if not is_rag_enabled():
        return ""

    results = await search(query, limit=5)
    if not results:
        return ""

    context = "## Relevant Documentation\n\n"
    for i, r in enumerate(results, 1):
        meta = r.get("metadata", {})
        source = meta.get("source", meta.get("platform", meta.get("title", "")))
        context += f"### Source {i}"
        if source:
            context += f" ({source})"
        context += f"\n{r['text']}\n\n"

    return context
