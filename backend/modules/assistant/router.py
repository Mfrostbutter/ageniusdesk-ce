"""AI Assistant API routes — chat, config, model listing, knowledge files."""

import os as _os
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel

from backend.auth_gate import require_role
from backend.config import decrypt_value, load_config, save_config
from backend.modules.assistant import providers
from backend.modules.assistant.baseline import loader as _baseline_loader
from backend.modules.assistant.baseline.schema import PutBaselineRequest

# Operator floor on the whole surface: chat spends tokens, /models and
# /test-creds reach out to operator-supplied URLs (SSRF-adjacent), and /config
# returns masked keys. Config-mutating routes raise the bar to admin below.
# require_role is a no-op on an open (AGD_DISABLE_LOGIN) install.
_ADMIN = Depends(require_role("admin"))

router = APIRouter(
    prefix="/api/assistant",
    tags=["assistant"],
    dependencies=[Depends(require_role("operator"))],
)

FILES_DIR = Path("data/assistant_files")
FILES_DIR.mkdir(parents=True, exist_ok=True)
MAX_TOTAL_BYTES = 20 * 1024 * 1024  # 20 MB
ALLOWED_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".json", ".jsonl", ".csv",
    ".py", ".js", ".ts", ".html", ".css", ".yaml", ".yml",
    ".xml", ".sh", ".env", ".toml", ".ini", ".conf",
}


class ChatRequest(BaseModel):
    messages: list[dict]
    context: str = ""
    model: str = ""  # Deprecated: legacy single-field override (model id only)
    override: dict | None = None  # Full {provider, model} override. Session-only.
    fallback: dict | None = None  # Optional {provider, model} fallback used if primary fails.
    surface: str = ""  # identifies the calling UI (e.g. "codelab")


# Per-area agents. Each surface (Code Lab / Error Triage / General Assistant)
# owns its provider, model, instructions, and optional fallback.
VALID_SURFACES = {"codelab", "triage", "assistant"}


class JobsRequest(BaseModel):
    # {surface: {provider, model, instructions, fallback_provider, fallback_model}}
    jobs: dict = {}


class SharedConfigRequest(BaseModel):
    ollama_url: str | None = None
    qdrant_url: str | None = None
    qdrant_collection: str | None = None
    custom_base_url: str | None = None  # API root for the "custom" OpenAI-compatible provider


class ConfigRequest(BaseModel):
    provider: str = "openrouter"
    api_key: str = ""
    model: str = "anthropic/claude-sonnet-4"
    ollama_url: str = "http://localhost:11434"
    qdrant_url: str = ""
    qdrant_collection: str = ""
    system_prompt: str = ""
    codelab_instructions: str | None = None  # None = leave unchanged (only the Code Lab save sends it)
    fallback_provider: str = ""
    fallback_model: str = ""
    fallback_api_key: str = ""


def _mask_key(raw: str) -> str:
    if not raw:
        return ""
    if raw.startswith("$"):
        return raw
    resolved = decrypt_value(raw)
    # Reveal only the last 4 (a recognized "which key is this" identifier) and
    # never the provider-prefixed head, to minimize the leaked secret material.
    return "..." + resolved[-4:] if len(resolved) > 8 else "configured"


@router.get("/config")
async def get_config():
    """Get assistant configuration (keys masked)."""
    config = load_config()
    ai = config.get("assistant", {})
    raw_key = ai.get("api_key", "")
    key_display = _mask_key(raw_key)

    provider = ai.get("provider", "openrouter")
    ollama_url = ai.get("ollama_url", "http://localhost:11434")
    # Ollama needs no API key, so a provider+url pair is enough to be "configured".
    # Mirrors the chat endpoint's own bypass at the same condition.
    configured = bool(raw_key) or (provider == "ollama" and bool(ollama_url))

    return {
        "configured": configured,
        # Per-job config (Code Lab / Error Triage / General Assistant). Each job
        # owns its provider, model, instructions, and optional fallback.
        "jobs": providers.build_jobs(ai),
        "job_labels": providers.JOB_LABELS,
        "key_status": providers.provider_key_status(),
        "instruction_defaults": {
            "codelab": providers.default_codelab_instructions(),
            "triage": providers.default_triage_instructions(),
            "assistant": providers.default_assistant_instructions(),
        },
        # Shared infra (not per-job): local Ollama endpoint, custom OpenAI-
        # compatible endpoint, RAG store.
        "ollama_url": ollama_url,
        "custom_base_url": ai.get("custom_base_url", ""),
        "qdrant_url": ai.get("qdrant_url", ""),
        "qdrant_collection": ai.get("qdrant_collection", ""),
        # Legacy fields kept for any older caller; the new UI ignores them.
        "provider": provider,
        "api_key_display": key_display,
        "model": ai.get("model", "anthropic/claude-sonnet-4"),
    }


@router.post("/jobs", dependencies=[_ADMIN])
async def save_jobs(req: JobsRequest):
    """Save the three per-area agents (Code Lab / Error Triage / General Assistant).

    Each entry is {provider, model, instructions, fallback_provider, fallback_model}.
    Provider keys are NOT stored here; they resolve from the Secrets store by
    provider at chat time. Saving jobs never touches the Harness or its AGENTS.md.
    """
    config = load_config()
    ai = config.get("assistant", {})
    existing = providers.build_jobs(ai)
    incoming = req.jobs or {}
    cleaned: dict = {}
    for surface in providers.JOB_SURFACES:
        base = dict(existing.get(surface) or {})
        sel = incoming.get(surface)
        if isinstance(sel, dict):
            if sel.get("provider"):
                base["provider"] = str(sel["provider"]).strip()
            if "model" in sel:
                base["model"] = str(sel.get("model") or "").strip()
            if "instructions" in sel:
                base["instructions"] = str(sel.get("instructions") or "")
            if "fallback_provider" in sel:
                base["fallback_provider"] = str(sel.get("fallback_provider") or "").strip()
            if "fallback_model" in sel:
                base["fallback_model"] = str(sel.get("fallback_model") or "").strip()
            # Optional explicit secret ref for this area's API key. Empty string
            # means "use the per-provider convention key" (the default).
            if "api_key_ref" in sel:
                base["api_key_ref"] = str(sel.get("api_key_ref") or "").strip()
        cleaned[surface] = base
    ai["jobs"] = cleaned
    config["assistant"] = ai
    save_config(config)
    return {"success": True, "jobs": cleaned}


@router.post("/shared", dependencies=[_ADMIN])
async def save_shared(req: SharedConfigRequest):
    """Save shared infra used by every job: local Ollama endpoint + RAG store."""
    config = load_config()
    ai = config.get("assistant", {})
    if req.ollama_url is not None:
        ai["ollama_url"] = req.ollama_url
    if req.custom_base_url is not None:
        ai["custom_base_url"] = req.custom_base_url.strip().rstrip("/")
    if req.qdrant_url is not None:
        ai["qdrant_url"] = req.qdrant_url
    if req.qdrant_collection is not None:
        ai["qdrant_collection"] = req.qdrant_collection
    config["assistant"] = ai
    save_config(config)
    return {"success": True}


@router.post("/config", dependencies=[_ADMIN])
async def save_assistant_config(req: ConfigRequest):
    """Save assistant configuration."""
    config = load_config()
    ai = config.get("assistant", {})

    ai["provider"] = req.provider
    ai["model"] = req.model
    ai["ollama_url"] = req.ollama_url
    ai["qdrant_url"] = req.qdrant_url
    ai["qdrant_collection"] = req.qdrant_collection
    ai["system_prompt"] = req.system_prompt
    if req.codelab_instructions is not None:
        ai["codelab_instructions"] = req.codelab_instructions
    ai["fallback_provider"] = req.fallback_provider
    ai["fallback_model"] = req.fallback_model

    if req.fallback_api_key:
        if req.fallback_api_key.startswith("$"):
            ai["fallback_api_key"] = req.fallback_api_key
        else:
            from backend.config import encrypt_value
            ai["fallback_api_key"] = encrypt_value(req.fallback_api_key)

    # Only update key if provided (non-empty)
    if req.api_key:
        # Store $VAR references as-is, encrypt raw keys
        if req.api_key.startswith("$"):
            ai["api_key"] = req.api_key
        else:
            from backend.config import encrypt_value
            ai["api_key"] = encrypt_value(req.api_key)

    config["assistant"] = ai
    save_config(config)
    return {"success": True}


@router.post("/chat")
async def chat(req: ChatRequest):
    """Send a message to the AI assistant.

    Request body supports two override paths:
      - `model`: legacy single-field override (model id only, uses saved provider+key).
      - `override`: full `{provider, model}` object. When `override.provider` is
        given, the backend resolves the provider-specific secret at request time
        and uses that key for this one request. The saved config is never
        mutated, overrides are session-only.
      - `fallback`: optional `{provider, model}` object. Used only if primary
        fails. Response includes `served_by` and (when fallback served)
        `primary_error`.
    """
    # Resolve the job for this surface (Code Lab / Error Triage / General
    # Assistant). Each job owns its provider, model, instructions, and fallback.
    job = providers.get_job_config(req.surface)

    # Model/provider: an explicit session override (the inline picker) wins for
    # on-the-fly experimentation; otherwise use the job's configured model. Both
    # resolve their provider key from the secrets store the same way.
    override = req.override or {
        "provider": job["provider"],
        "model": job["model"],
        "api_key_ref": job.get("api_key_ref", ""),
    }

    # Fallback: an explicit request wins; otherwise the job's own fallback.
    fallback = req.fallback or None
    if fallback is None and job.get("fallback_provider"):
        fallback = {"provider": job["fallback_provider"], "model": job.get("fallback_model", "")}

    result = await providers.chat(
        req.messages,
        req.context,
        model_override=req.model or "",
        override=override,
        fallback=fallback,
        surface=req.surface,
        instructions=job["instructions"],
    )
    if "error" in result:
        raise HTTPException(status_code=502, detail=result["error"])
    return result


@router.get("/models")
async def list_models(provider: str = "openrouter", ollama_url: str = "", api_key_ref: str = ""):
    """List available models for a provider.

    Strategy:
      - For `ollama`, always hits the live /api/tags endpoint.
      - For the OpenAI-compatible providers (anthropic / openai / openrouter and
        the registry: groq / deepseek / mistral / xai / together / custom), calls
        the live /models endpoint when a key is configured (resolved from the
        per-provider convention secret) and caches it for 5 minutes. Perplexity
        and the custom provider without a base URL serve a curated fallback list.
      - On any failure (missing key, auth error, network, timeout) we return
        the hardcoded fallback list. The client always sees SOME models.

    Response shape: `{"models": [...], "provider": "<p>", "source": "live"
    | "cached" | "fallback", "cached_at": <ts>?}`.
    """
    p = (provider or "").lower()
    if p == "ollama":
        url = ollama_url
        if not url:
            cfg = providers.get_assistant_config()
            url = cfg.get("ollama_url", "http://localhost:11434")
        models = await providers.list_ollama_models(url)
        source = "live" if models else "fallback"
        return {"models": models, "provider": "ollama", "source": source}

    if p in ("anthropic", "openai", "openrouter") or p in providers.OPENAI_COMPAT_PROVIDERS:
        result = await providers.list_provider_models(p, api_key_ref=api_key_ref)
        result["provider"] = p
        return result

    # Unknown provider — return empty list without crashing.
    return {"models": [], "provider": p, "source": "fallback"}


@router.post("/test")
async def test_connection():
    """Test the AI assistant connection (uses saved config)."""
    result = await providers.chat(
        [{"role": "user", "content": "Respond with just 'Connected!' if you can read this."}]
    )
    if "error" in result:
        return {"connected": False, "error": result["error"]}
    return {"connected": True, "model": result.get("model", ""), "response": result.get("response", "")}


class TestCredsRequest(BaseModel):
    provider: str
    api_key: str = ""
    api_key_ref: str = ""  # a $NAME secret to resolve server-side (preferred over api_key)
    model: str = ""
    ollama_url: str = ""


@router.post("/test-creds")
async def test_creds(req: TestCredsRequest):
    """Test AI provider creds without saving. Used by the setup wizard and the
    Models area key picker. A `$NAME` ref is resolved from the secrets store
    server-side so the plaintext key never round-trips through the browser."""
    import httpx
    p = req.provider
    # Resolve a chosen secret ref to its plaintext key (preferred path).
    if req.api_key_ref:
        ref = req.api_key_ref if req.api_key_ref.startswith("$") else f"${req.api_key_ref}"
        resolved = providers.decrypt_value(ref)
        if not resolved or resolved == ref[1:]:
            return {"ok": False, "error": f"Secret {ref} is empty or not found."}
        req.api_key = resolved
    elif not req.api_key and p in providers.PROVIDER_KEY_MAP:
        # "Use provider default key" — resolve the per-provider convention secret.
        req.api_key = providers._resolve_provider_key(p)

    # Never fire a request with an empty key: an empty "Bearer " header is itself
    # an httpx error ("Illegal header value"). Return a clean message instead.
    if p != "ollama" and not req.api_key:
        return {
            "ok": False,
            "error": "No API key set for this area. Pick a saved key above, "
                     "or add the provider's key in Secrets.",
        }

    try:
        if p == "openrouter":
            # OpenRouter's /v1/models is public (200 even with a bad key), so a
            # real connection test must hit the key-validation endpoint.
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get(
                    "https://openrouter.ai/api/v1/key",
                    headers={"Authorization": f"Bearer {req.api_key}"},
                )
                if r.status_code == 200:
                    return {"ok": True, "model": req.model or "anthropic/claude-sonnet-4"}
                if r.status_code in (401, 403):
                    return {"ok": False, "error": "Invalid or unauthorized API key"}
                return {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:200]}"}
        if p == "openai":
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get(
                    "https://api.openai.com/v1/models",
                    headers={"Authorization": f"Bearer {req.api_key}"},
                )
                if r.status_code == 200:
                    return {"ok": True, "model": req.model or "gpt-4o"}
                return {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:200]}"}
        if p == "anthropic":
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get(
                    "https://api.anthropic.com/v1/models",
                    headers={"x-api-key": req.api_key, "anthropic-version": "2023-06-01"},
                )
                if r.status_code == 200:
                    return {"ok": True, "model": req.model or "claude-sonnet-4-20250514"}
                return {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:200]}"}
        if p == "ollama":
            try:
                base = providers.assert_safe_probe_url(
                    req.ollama_url or req.api_key or "http://localhost:11434"
                )
            except providers.UnsafeProbeURL as e:
                return {"ok": False, "error": f"Ollama URL not allowed: {e}"}
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get(f"{base}/api/tags")
                if r.status_code == 200:
                    return {"ok": True, "model": req.model or "llama3"}
                # Don't reflect the fetched body: the target is operator-supplied.
                return {"ok": False, "error": f"HTTP {r.status_code} from Ollama at {base}"}
        # Perplexity / Groq / DeepSeek / Mistral / xAI / Together / custom — a
        # minimal chat-completions ping via the provider registry.
        return await providers.ping_provider(p, api_key=req.api_key, model=req.model)
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── Baseline (C3 Constitution) ────────────────────────────────────────────────


def _assert_constitution_enabled() -> None:
    """Raise HTTP 503 when AGD_CONSTITUTION_ENABLED=false."""
    if _os.environ.get("AGD_CONSTITUTION_ENABLED", "true").lower() in {"false", "0", "no"}:
        raise HTTPException(status_code=503, detail="C3 disabled by AGD_CONSTITUTION_ENABLED=false")


@router.get("/baseline")
async def get_baseline():
    """Return the current constitution.

    Response: {"version": int, "updated": ISO8601, "overrideable_sections": [str],
               "content": str, "size": int}
    """
    _assert_constitution_enabled()
    return await _baseline_loader.read()


@router.put("/baseline", dependencies=[_ADMIN])
async def put_baseline(req: PutBaselineRequest):
    """Replace the constitution body.

    Optimistic concurrency: ``expected_version`` must match the on-disk version.
    Returns 409 on mismatch, 413 when the body exceeds 64 KiB.
    """
    _assert_constitution_enabled()
    return await _baseline_loader.write(
        expected_version=req.expected_version,
        content=req.content,
        overrideable_sections=req.overrideable_sections,
    )


# ── Knowledge Files ────────────────────────────────────────────────────────────

def _total_files_size() -> int:
    return sum(f.stat().st_size for f in FILES_DIR.iterdir() if f.is_file())


@router.get("/files")
async def list_files():
    """List uploaded knowledge files."""
    files = []
    for f in sorted(FILES_DIR.iterdir()):
        if f.is_file():
            files.append({"name": f.name, "size": f.stat().st_size})
    total = sum(f["size"] for f in files)
    return {"files": files, "total_bytes": total, "max_bytes": MAX_TOTAL_BYTES}


@router.post("/files")
async def upload_file(file: UploadFile = File(...)):
    """Upload a knowledge file (text formats only, 20 MB total cap)."""
    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        allowed = ", ".join(sorted(ALLOWED_EXTENSIONS))
        raise HTTPException(
            status_code=400,
            detail=f"File type '{ext}' not allowed. Supported: {allowed}",
        )

    content = await file.read()

    # Check total size after upload
    current = _total_files_size()
    if current + len(content) > MAX_TOTAL_BYTES:
        remaining = MAX_TOTAL_BYTES - current
        raise HTTPException(
            status_code=400,
            detail=f"Storage limit reached. {remaining / 1024:.0f} KB remaining of 20 MB total."
        )

    # Sanitize filename — no path traversal
    safe_name = Path(file.filename).name.replace("..", "").replace("/", "_").replace("\\", "_")
    dest = FILES_DIR / safe_name
    dest.write_bytes(content)

    return {"success": True, "name": safe_name, "size": len(content)}


@router.delete("/files/{filename}")
async def delete_file(filename: str):
    """Delete a knowledge file."""
    # Prevent path traversal
    safe_name = Path(filename).name
    dest = FILES_DIR / safe_name
    if not dest.exists():
        raise HTTPException(status_code=404, detail="File not found")
    dest.unlink()
    return {"success": True}
