"""Discover operator-authored agents from the vault (`data/workspace/agents/`).

An agent the operator owns lives at `vault/agents/<id>/`:
  - `agent.json` : manifest (metadata only: name, framework, model, tools, hitl, ...)
  - `graph.py`   : a PURE factory exporting `build(llm, tools, checkpointer=None)`,
                   and OPTIONALLY `initial_state(task, target)` + `kickoff(error_id,
                   prompt)`. It imports only langgraph/langchain; the host injects the
                   model + the resolved tools, so the agent code never touches the host.

Discovery reads only the manifests (cheap JSON, boot-safe, no langgraph needed) and
builds an `AgentDef` per agent whose callables LAZILY import `graph.py` at run time.
So a vault agent shows in the catalog even when the langgraph extra is absent; a run
then surfaces the missing-dep error. Code Lab's "Register to Agent Fleet" writes
these folders; this module is what makes them show up + run.
"""

from __future__ import annotations

import importlib.util
import json
import logging
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field, ValidationError

from .registry import AgentDef

logger = logging.getLogger(__name__)

AGENTS_DIRNAME = "agents"

# (path, mtime) -> loaded module. Avoids re-exec across the build/state/kickoff
# calls in one run, and reloads automatically when Code Lab edits the file.
_MOD_CACHE: dict[tuple[str, float], Any] = {}


def _agents_dir() -> Path:
    from backend.modules.notes import storage as vault

    return vault.VAULT_DIR / AGENTS_DIRNAME


class AgentManifest(BaseModel):
    """Metadata for a vault agent. The graph.py owns behavior; this owns display +
    config (what the catalog shows and what the runtime injects)."""

    id: str = ""
    name: str
    framework: str = "langgraph"          # langgraph | pydantic-ai (pydantic-ai: later)
    entry: str = "graph.py"
    builder: str = "build"
    model: str = "claude-haiku-4-5"
    model_env: str = ""
    max_tokens: int = 2048
    max_tokens_env: str = ""
    tools: list[str] = Field(default_factory=list)
    hitl: bool = False
    badges: list[str] = Field(default_factory=list)
    tagline: str = ""
    description: str = ""
    run_hint: str = ""
    uses_errors: bool = True
    enabled: bool = True


def _load_module(path: Path, mod_name: str):
    """Load a vault graph.py as a module (cached by path+mtime)."""
    try:
        key = (str(path), path.stat().st_mtime)
    except OSError as e:
        raise ImportError(f"{path} not found: {e}") from e
    cached = _MOD_CACHE.get(key)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(mod_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _MOD_CACHE[key] = module
    return module


def _to_agentdef(agent_dir: Path, m: AgentManifest) -> AgentDef:
    """Build an AgentDef whose callables lazily import the agent's graph.py."""
    entry_path = agent_dir / m.entry
    mod_name = f"agentfleet_vault_{m.id}"

    def build(llm, checkpointer=None, **_kw):
        # Imports the agent's graph.py and calls its factory. The runner decides HOW
        # to execute what comes back (LangGraph driver vs PydanticAI adapter), per
        # the agent's framework; a PydanticAI factory ignores the llm/checkpointer.
        from .tools_local import resolve_tools

        mod = _load_module(entry_path, mod_name)
        factory = getattr(mod, m.builder, None)
        if not callable(factory):
            raise AttributeError(f"agent '{m.id}': {m.entry} has no callable '{m.builder}'.")
        return factory(llm, resolve_tools(m.tools), checkpointer=checkpointer)

    def initial_state(task: str, target: str) -> dict:
        # Prefer a graph-defined state (e.g. a fan-out agent seeding `findings`);
        # otherwise a plain MessagesState with the triage_target channel.
        try:
            fn = getattr(_load_module(entry_path, mod_name), "initial_state", None)
            if callable(fn):
                return fn(task, target)
        except Exception:  # noqa: BLE001 - fall back to the default state shape
            pass
        from langchain_core.messages import HumanMessage

        return {"messages": [HumanMessage(content=task)], "triage_target": target}

    def kickoff(error_id: Optional[int], prompt: str) -> str:
        try:
            fn = getattr(_load_module(entry_path, mod_name), "kickoff", None)
            if callable(fn):
                return fn(error_id, prompt)
        except Exception:  # noqa: BLE001 - fall back to the manifest-driven default
            pass
        if m.uses_errors:
            if error_id is not None:
                return f"Triage AgeniusDesk error id {error_id}."
            return prompt.strip() or "Triage the most recent error."
        return prompt.strip() or f"Run {m.name}."

    badges = tuple(m.badges) + (("vault",) if "vault" not in m.badges else ())
    return AgentDef(
        id=m.id,
        name=m.name,
        tagline=m.tagline or "Authored in the vault.",
        description=m.description,
        badges=badges,
        default_model=m.model,
        build=build,
        initial_state=initial_state,
        kickoff=kickoff,
        model_env=m.model_env,
        max_tokens=m.max_tokens,
        max_tokens_env=m.max_tokens_env,
        hitl=m.hitl,
        framework=m.framework,
        run_hint=m.run_hint,
        uses_errors=m.uses_errors,
    )


def discover() -> list[AgentDef]:
    """Scan the vault for agents and return their AgentDefs. Best-effort: a bad
    manifest is logged and skipped, never fatal. Reads manifests only (no code)."""
    out: list[AgentDef] = []
    base = _agents_dir()
    if not base.is_dir():
        return out
    for child in sorted(base.iterdir()):
        if not child.is_dir() or child.name.startswith((".", "_")):
            continue
        manifest_path = child / "agent.json"
        if not manifest_path.is_file():
            continue
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
            data.setdefault("id", child.name)
            manifest = AgentManifest(**data)
        except (json.JSONDecodeError, ValidationError, OSError) as e:
            logger.warning("agent-fleet: skipping vault agent %s: %s", child.name, e)
            continue
        if not manifest.enabled:
            continue
        if manifest.id != child.name:
            # The folder name is the identity (the install key); trust it over the file.
            manifest = manifest.model_copy(update={"id": child.name})
        try:
            out.append(_to_agentdef(child, manifest))
        except Exception as e:  # noqa: BLE001 - one bad agent must not break the catalog
            logger.warning("agent-fleet: could not register vault agent %s: %s", child.name, e)
    return out
