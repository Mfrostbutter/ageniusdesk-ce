"""FastAPI routes for the managed Agent Fleet.

  GET    /api/agent-fleet/agents             catalog of registered agents (cards)
  GET    /api/agent-fleet/agents/{id}/graph  static topology (nodes + edges) for the live view
  POST   /api/agent-fleet/triage             kick a run {agent_id?, error_id?, prompt?} -> {run_id}
  POST   /api/agent-fleet/runs/{id}/resume   resume a HITL run parked at an interrupt
  GET    /api/agent-fleet/runs               list past runs (no big blobs); ?agent_id= filters
  GET    /api/agent-fleet/runs/{id}          full run detail (events log + result markdown)
  DELETE /api/agent-fleet/runs/{id}          remove a run row

Fire-and-forget: POST returns immediately, the runner persists progress into the
run's event log, and the frontend polls the run detail. The route is agent-agnostic:
it resolves an AgentDef by id and the runner dispatches to that agent's graph.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend.auth_gate import require_trusted_request

from . import registry, runner, storage

_AGENT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")[:64]


router = APIRouter(
    prefix="/api/agent-fleet",
    tags=["agent-fleet"],
    dependencies=[Depends(require_trusted_request)],
)


class TriageRequest(BaseModel):
    agent_id: str = Field(default="", description="Which managed agent to run. Blank = default (ops-triage).")
    error_id: Optional[int] = Field(default=None, description="A specific AgeniusDesk error id to triage.")
    prompt: str = Field(default="", description="Free-form request. Blank triages the most recent error.")


class ResumeRequest(BaseModel):
    action: str = Field(default="approve", description="Human verdict: approve | edit | reject.")
    edited: str = Field(default="", description="The edited fix text, when action=edit.")
    mode: str = Field(default="dry_run", description="For write agents: dry_run | live.")
    choice: int | None = Field(default=None, description="1-based pick when the gate offers choices.")


class RegisterAgentRequest(BaseModel):
    name: str = Field(..., description="Display name; the id is derived from it unless `id` is set.")
    id: str = Field(default="", description="Optional explicit slug id (else derived from name).")
    framework: str = Field(default="langgraph", description="langgraph | pydantic-ai")
    code: str = Field(..., description="The agent's graph.py source (pure factory: build(llm, tools, checkpointer)).")
    model: str = Field(default="claude-haiku-4-5")
    model_env: str = Field(default="")
    max_tokens: int = Field(default=2048)
    tools: list[str] = Field(default_factory=list)
    hitl: bool = Field(default=False)
    badges: list[str] = Field(default_factory=list)
    tagline: str = Field(default="")
    description: str = Field(default="")
    run_hint: str = Field(default="")
    uses_errors: bool = Field(default=True)


@router.get("/agents")
async def list_agents():
    return {"agents": [a.card() for a in registry.all_agents()], "default": registry.DEFAULT_AGENT_ID}


@router.get("/tools")
async def list_tools():
    """The tool catalog a vault agent can declare in its manifest (for the builder).
    Empty when the langgraph extra is absent (the @tool objects can't be imported)."""
    try:
        from . import tools_local

        return {"tools": tools_local.tool_catalog()}
    except Exception:  # noqa: BLE001 - extra not installed: nothing to offer
        return {"tools": []}


@router.post("/agents")
async def register_agent(req: RegisterAgentRequest):
    """Register (create or update) an operator-authored agent in the vault.

    Writes vault/agents/<id>/{agent.json, graph.py}; the registry re-discovers vault
    agents on every read, so it appears in the catalog immediately, no restart. This
    is the target of Code Lab's "Register to Agent Fleet"."""
    from . import vault_agents

    agent_id = (req.id or _slugify(req.name)).strip()
    if not _AGENT_ID_RE.match(agent_id):
        raise HTTPException(status_code=400, detail="Invalid agent id: use a lowercase slug, e.g. 'my-agent'.")
    if agent_id in registry.builtin_ids():
        raise HTTPException(status_code=409, detail=f"'{agent_id}' is a built-in agent; choose a different name.")
    # Validate the code parses before writing a broken agent.
    try:
        compile(req.code, f"<{agent_id}/graph.py>", "exec")
    except SyntaxError as e:
        raise HTTPException(status_code=400, detail=f"graph.py has a syntax error: {e}")
    try:
        manifest = vault_agents.AgentManifest(
            id=agent_id, name=req.name, framework=req.framework, model=req.model,
            model_env=req.model_env, max_tokens=req.max_tokens, tools=req.tools, hitl=req.hitl,
            badges=req.badges, tagline=req.tagline, description=req.description,
            run_hint=req.run_hint, uses_errors=req.uses_errors, enabled=True,
        )
    except Exception as e:  # noqa: BLE001 - surface validation errors to the UI
        raise HTTPException(status_code=400, detail=f"Invalid manifest: {e}")

    agent_dir = vault_agents._agents_dir() / agent_id
    try:
        agent_dir.mkdir(parents=True, exist_ok=True)
        (agent_dir / "graph.py").write_text(req.code, encoding="utf-8")
        (agent_dir / "agent.json").write_text(
            json.dumps(manifest.model_dump(), indent=2, ensure_ascii=False), encoding="utf-8"
        )
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Could not write agent files: {e}")
    return {"ok": True, "id": agent_id, "name": req.name}


@router.delete("/agents/{agent_id}")
async def delete_agent(agent_id: str):
    """Remove an operator-authored (vault) agent. Built-ins cannot be deleted."""
    from . import vault_agents

    if agent_id in registry.builtin_ids():
        raise HTTPException(status_code=400, detail="Built-in agents cannot be deleted.")
    if not _AGENT_ID_RE.match(agent_id):
        raise HTTPException(status_code=400, detail="Invalid agent id.")
    if runner.is_live():
        raise HTTPException(status_code=409, detail="A run is in progress.")
    agent_dir = vault_agents._agents_dir() / agent_id
    if not agent_dir.is_dir():
        raise HTTPException(status_code=404, detail="Agent not found.")
    shutil.rmtree(agent_dir, ignore_errors=True)
    return {"ok": True, "id": agent_id}


@router.get("/agents/{agent_id}/graph")
async def agent_graph(agent_id: str):
    """Static topology (nodes + edges) for the in-app graph visualization.

    Builds the agent's graph with a throwaway, never-invoked model (no key, no
    network) purely to read its shape via compiled.get_graph(). The frontend lights
    up nodes from the run's persisted event log."""
    agent = registry.get_agent(agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail=f"Unknown agent '{agent_id}'.")
    try:
        from langchain_anthropic import ChatAnthropic

        llm = ChatAnthropic(model=agent.default_model or "claude-haiku-4-5", api_key="topology-only")
        try:
            compiled = (agent.build(llm, None, reviewer_llm=llm)
                        if agent.reviewer_provider else agent.build(llm, None))
        except TypeError:
            compiled = agent.build(llm, None)
        g = compiled.get_graph()
        nodes = list(g.nodes.keys())
        edges = [{"source": e.source, "target": e.target,
                  "conditional": bool(getattr(e, "conditional", False))} for e in g.edges]
        return {"nodes": nodes, "edges": edges}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Could not build topology: {type(e).__name__}: {e}")


@router.post("/triage")
async def start_triage(req: TriageRequest):
    live = runner.is_live()
    if live:
        raise HTTPException(status_code=409, detail=f"A run is already in progress ({live}).")

    agent_id = req.agent_id or registry.DEFAULT_AGENT_ID
    agent = registry.get_agent(agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail=f"Unknown agent '{agent_id}'.")

    model = os.environ.get(agent.model_env, agent.default_model) if agent.model_env else agent.default_model
    target = str(req.error_id) if req.error_id is not None else (req.prompt.strip() or "latest")
    run = await storage.create_run(agent_id, target, req.prompt.strip(), model)
    # Fire-and-forget; the runner persists progress into the run's event log.
    asyncio.create_task(runner.run(run["id"], agent_id, req.error_id, req.prompt))
    return {"run_id": run["id"], "run": run}


@router.post("/runs/{run_id}/resume")
async def resume_run(run_id: str, req: ResumeRequest):
    """Resume a HITL run parked at a human-approval interrupt."""
    if not runner.is_paused(run_id):
        raise HTTPException(status_code=409, detail="Run is not awaiting approval.")
    if runner.is_live():
        raise HTTPException(status_code=409, detail="Another run is in progress.")
    decision = {"action": req.action, "edited": req.edited, "mode": req.mode, "choice": req.choice}
    asyncio.create_task(runner.resume(run_id, decision))
    return {"ok": True}


@router.get("/runs")
async def list_runs(limit: int = 100, agent_id: str = ""):
    return {
        "runs": await storage.list_runs(limit=limit, agent_id=agent_id),
        "live_run_id": runner.is_live(),
    }


@router.get("/runs/{run_id}")
async def get_run(run_id: str):
    run = await storage.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found.")
    return run


@router.delete("/runs/{run_id}")
async def delete_run(run_id: str):
    if runner.is_live() == run_id:
        raise HTTPException(status_code=409, detail="Run is in progress.")
    ok = await storage.delete_run(run_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Run not found.")
    return {"ok": True}
