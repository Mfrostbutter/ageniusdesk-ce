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
import os
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend.auth_gate import require_trusted_request

from . import registry, runner, storage

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


@router.get("/agents")
async def list_agents():
    return {"agents": [a.card() for a in registry.all_agents()], "default": registry.DEFAULT_AGENT_ID}


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
