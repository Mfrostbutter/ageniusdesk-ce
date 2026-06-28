"""Code-defined registry of the LangGraph agents the fleet manages.

The headline frame: AgeniusDesk operates LangGraph agents the way it operates n8n
instances. This registry is the catalog. Each agent is an `AgentDef`: identity +
metadata + a pure graph factory + how it turns a kickoff into graph state. The
catalog UI lists these; `runner.py` dispatches a run to the right graph by
`agent_id`. Adding an agent is adding one `AgentDef` here and nothing else: the
runner, router, storage, and frontend are all agent-agnostic.

Boot discipline (mirrors runner.py): heavy langchain/langgraph imports happen
INSIDE the builder/state functions, never at module scope. The package must import
cleanly at app boot even when the optional langgraph extra is absent, because
module auto-discovery imports the package on the way to `router`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional

# ── Agent definition ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class AgentDef:
    """One managed LangGraph agent.

    build: (llm, checkpointer) -> compiled graph. Pure factory; imports its graph
        + tools lazily so this module stays boot-safe.
    initial_state: (task, target) -> the dict fed to graph.astream.
    kickoff: (error_id, prompt) -> the human task string the run starts from.
    hitl: True when the graph interrupts for human approval (needs a checkpointer
        + a thread_id + a resume path).
    """

    id: str
    name: str
    tagline: str
    description: str
    badges: tuple[str, ...]
    default_model: str
    build: Callable[..., Any]
    initial_state: Callable[[str, str], dict]
    kickoff: Callable[[Optional[int], str], str]
    model_env: str = ""
    max_tokens: int = 2048
    max_tokens_env: str = ""
    hitl: bool = False
    # Cross-provider reviewer hook. Unused by the v1 fleet (all single-provider),
    # kept so the agent-agnostic runner can build a second model for a future agent
    # without a runner change. Empty = single-provider (the default).
    reviewer_provider: str = ""  # "" | "openai" | "anthropic"
    reviewer_model: str = ""
    reviewer_model_env: str = ""
    # Free-text hint shown in the run composer (what to type / pick).
    run_hint: str = ""
    # True for agents that operate on an AgeniusDesk error (the composer shows the
    # error picker and runs default to "most recent error").
    uses_errors: bool = True

    def card(self) -> dict[str, Any]:
        """The agent-agnostic payload the catalog UI renders."""
        return {
            "id": self.id,
            "name": self.name,
            "tagline": self.tagline,
            "description": self.description,
            "badges": list(self.badges),
            "model": self.default_model,
            "reviewer_provider": self.reviewer_provider,
            "reviewer_model": self.reviewer_model,
            "hitl": self.hitl,
            "run_hint": self.run_hint,
            "uses_errors": self.uses_errors,
        }


# ── Shared kickoff/state helpers ─────────────────────────────────────────────


def _kickoff_triage(error_id: Optional[int], prompt: str) -> str:
    """Human task for the tool-loop agents (ops-triage, fix-proposer)."""
    if error_id is not None:
        return f"Triage AgeniusDesk error id {error_id}."
    if prompt.strip():
        return prompt.strip()
    return "Triage the most recent error."


def _messages_state(task: str, target: str) -> dict:
    """MessagesState kickoff with the explicit triage_target channel."""
    from langchain_core.messages import HumanMessage

    return {"messages": [HumanMessage(content=task)], "triage_target": target}


def _kickoff_health(error_id: Optional[int], prompt: str) -> str:
    """The fleet sweep ignores a specific error; it reports on the whole fleet."""
    return prompt.strip() or "Produce a current fleet health report."


def _health_state(task: str, target: str) -> dict:
    """Health state seeds the additive `findings` channel empty."""
    from langchain_core.messages import HumanMessage

    return {"messages": [HumanMessage(content=task)], "triage_target": target, "findings": []}


# ── ops-triage (ReAct tool loop) ─────────────────────────────────────────────


def _build_ops_triage(llm, checkpointer=None):
    from .agent.graph import build_graph
    from .tools_local import TOOLS

    return build_graph(llm, TOOLS, checkpointer=checkpointer)


OPS_TRIAGE = AgentDef(
    id="ops-triage",
    name="Ops Triage",
    tagline="Investigates a live n8n failure and writes a root-cause triage.",
    description=(
        "A classic ReAct tool-loop: the agent pulls recent errors, inspects the "
        "failing workflow and its execution payload, checks whether the failure "
        "recurs, then writes a structured triage. Read-only over the n8n fleet."
    ),
    badges=("tool-loop", "read-only", "ReAct"),
    # Read-only diagnosis is well within Haiku's range; route it to the cheap model.
    # Override via OPS_TRIAGE_MODEL.
    default_model="claude-haiku-4-5",
    model_env="OPS_TRIAGE_MODEL",
    max_tokens_env="OPS_TRIAGE_MAX_TOKENS",
    build=_build_ops_triage,
    initial_state=_messages_state,
    kickoff=_kickoff_triage,
    run_hint="Pick an error, or describe what to triage. Blank = most recent error.",
)


# ── fix-proposer (human-in-the-loop) ─────────────────────────────────────────


def _build_fix_proposer(llm, checkpointer=None):
    from .agent.graph_hitl import build_fix_graph
    from .tools_local import TOOLS

    return build_fix_graph(llm, TOOLS, checkpointer=checkpointer)


FIX_PROPOSER = AgentDef(
    id="fix-proposer",
    name="Fix Proposer",
    tagline="Triages a failure, drafts one concrete fix, and pauses for your approval.",
    description=(
        "Investigates like Ops Triage, then proposes a single minimal, reversible n8n "
        "change and HALTS on a LangGraph interrupt() backed by a checkpointer. You "
        "approve, edit, or reject in the UI; the graph resumes from exactly where it "
        "paused and reflects your decision into the outcome."
    ),
    badges=("human-in-the-loop", "checkpointer", "interrupt"),
    # Drafting the fix is correctness-sensitive; keep it on Sonnet.
    default_model="claude-sonnet-4-6",
    model_env="FIX_PROPOSER_MODEL",
    max_tokens_env="OPS_TRIAGE_MAX_TOKENS",
    build=_build_fix_proposer,
    initial_state=_messages_state,
    kickoff=_kickoff_triage,
    hitl=True,
    run_hint="Pick an error to propose a fix for. Blank = most recent error.",
)


# ── health-reporter (parallel fan-out) ───────────────────────────────────────


def _build_health_reporter(llm, checkpointer=None):
    from .agent.graph_health import build_health_graph
    from .tools_local import TOOLS

    return build_health_graph(llm, TOOLS, checkpointer=checkpointer)


HEALTH_REPORTER = AgentDef(
    id="health-reporter",
    name="Health Reporter",
    tagline="Sweeps the fleet through three lenses in parallel, then synthesizes a report.",
    description=(
        "A parallel fan-out / fan-in graph: a plan node dispatches three diagnostic "
        "lenses (recent failures, fleet-health metrics, error-execution volume) that "
        "run concurrently in one LangGraph superstep, then a synthesize node reconciles "
        "them into a single fleet health report. Shows real parallelism + a reduce."
    ),
    badges=("parallel", "fan-out", "map-reduce"),
    # Lens summaries + synthesis are easy work; route to Haiku. Override via
    # HEALTH_REPORTER_MODEL.
    default_model="claude-haiku-4-5",
    model_env="HEALTH_REPORTER_MODEL",
    max_tokens_env="OPS_TRIAGE_MAX_TOKENS",
    build=_build_health_reporter,
    initial_state=_health_state,
    kickoff=_kickoff_health,
    run_hint="Run a full fleet sweep. Input is ignored; this reports on the whole fleet.",
)


# ── Registry ─────────────────────────────────────────────────────────────────

# Insertion order = catalog display order.
_REGISTRY: dict[str, AgentDef] = {
    a.id: a
    for a in (
        OPS_TRIAGE,
        FIX_PROPOSER,
        HEALTH_REPORTER,
    )
}

DEFAULT_AGENT_ID = OPS_TRIAGE.id


def _vault_agents() -> list[AgentDef]:
    """Operator-authored agents discovered from the vault. Lazy import avoids a
    circular dependency (vault_agents imports AgentDef from here) and keeps boot
    light. Never raises: discovery is best-effort."""
    try:
        from . import vault_agents

        return vault_agents.discover()
    except Exception:  # noqa: BLE001 - a discovery failure must not hide the built-ins
        return []


def all_agents() -> list[AgentDef]:
    """Built-in agents first, then vault agents (skipping any id that collides with a
    built-in). Re-read each call so a newly registered vault agent shows immediately."""
    builtins = list(_REGISTRY.values())
    builtin_ids = {a.id for a in builtins}
    extra = [a for a in _vault_agents() if a.id not in builtin_ids]
    return builtins + extra


def get_agent(agent_id: str) -> Optional[AgentDef]:
    if agent_id in _REGISTRY:
        return _REGISTRY[agent_id]
    for a in _vault_agents():
        if a.id == agent_id:
            return a
    return None


def builtin_ids() -> set[str]:
    """Ids of the built-in (code) agents, which a vault agent may not shadow."""
    return set(_REGISTRY)


def register(agent: AgentDef) -> None:
    """Add (or replace) an agent. Lets a future agent's graph + def live together."""
    _REGISTRY[agent.id] = agent
