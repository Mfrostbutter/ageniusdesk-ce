"""Feature switches: which surfaces this installation shows and serves.

A profile picks a default set; per-feature overrides adjust it. Core features
cannot be switched off. State lives in config.json under "features" and is
read through a small mtime cache so the HTTP gate stays cheap.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from backend import config as _config
from backend.config import load_config, save_config


@dataclass(frozen=True)
class Feature:
    id: str
    label: str
    description: str
    group: str
    views: tuple[str, ...] = ()
    settings_tabs: tuple[str, ...] = ()
    api_prefixes: tuple[str, ...] = ()
    api_allow: tuple[str, ...] = ()  # paths that stay open while the feature is off (ingest)
    widgets: tuple[str, ...] = ()
    core: bool = False
    requires: tuple[str, ...] = ()


CATALOG: tuple[Feature, ...] = (
    Feature("overview", "Overview", "Dashboards and widgets.", "core", views=("dashboard",), core=True),
    Feature("workflows", "Workflows", "Workflow list, detail and executions.", "core", views=("workflows",),
            core=True),
    Feature("errors", "Executions / Errors", "Failed executions, error grouping and the error handler.", "core",
            views=("errors",), settings_tabs=("error-handler",), core=True),
    Feature("instances", "Instances", "Connected n8n instances.", "core", views=("instances",),
            settings_tabs=("instances",), core=True),
    Feature("secrets", "Secrets", "Encrypted credential store.", "core", views=("secrets",),
            settings_tabs=("secrets",), core=True),
    Feature("admin", "Admin and settings", "Users, account, themes, modules, help.", "core",
            views=("admin", "settings"), settings_tabs=("account", "themes", "modules", "help", "features"),
            core=True),

    Feature("promote", "Promote", "Instance-to-instance workflow promotion with preflight.", "operate",
            views=("promote",), api_prefixes=("/api/promote",)),
    Feature("observe", "Observe", "Per-execution OpenTelemetry traces and silent-failure detection.", "operate",
            views=("observe",), api_prefixes=("/api/otel",), api_allow=("/api/otel/v1",)),
    Feature("fleet_health", "Fleet Health", "Workflow health across all instances.", "operate", views=("fleet",)),
    Feature("insights", "Insights", "Execution analytics, success rates and error trends.", "operate",
            views=("insights",), api_prefixes=("/api/insights",)),
    Feature("llm_cost", "LLM Cost", "Provider spend, quotas and burn.", "operate", views=("llm-cost",),
            api_prefixes=("/api/llm-cost",), api_allow=("/api/llm-cost/ingest", "/api/llm-cost/forwarder")),
    Feature("containers", "Containers", "Docker container management and n8n bundles.", "operate",
            views=("containers",), api_prefixes=("/api/containers",)),

    Feature("agents", "Agent Fleet", "LangGraph and PydanticAI agents, and the agent builder.", "build",
            views=("agent-fleet",), api_prefixes=("/api/agent-fleet",)),
    Feature("codelab", "Code Lab", "Code-node editor and workflow builder.", "build", views=("codelab",)),
    Feature("harness", "Harness", "Workspace files, sources, connectors, agent instructions and notes.", "build",
            views=("knowledge", "knowledge-connectors", "knowledge-instructions", "notes"),
            api_prefixes=("/api/notes", "/api/knowledge")),
    Feature("assistant", "AI assistant", "Chat, model settings and MCP servers.", "build",
            views=("assistant", "ai-settings", "mcp-servers"), settings_tabs=("assistant", "mcp")),

    Feature("backup", "Import and export", "Workflow import, export and scheduled backups.", "admin",
            views=("import", "backup"), api_prefixes=("/api/backups",)),
    Feature("music", "Your Vibe", "The music player.", "admin", views=("music",), api_prefixes=("/api/spotify",)),
)

BY_ID: dict[str, Feature] = {f.id: f for f in CATALOG}

_ALL = tuple(f.id for f in CATALOG)
_CORE = tuple(f.id for f in CATALOG if f.core)

PROFILES: dict[str, dict] = {
    "agency": {
        "label": "Agency",
        "description": "Many clients, many instances: health, traces, insights, spend and promotion. No build tooling.",
        "on": _CORE + ("promote", "observe", "fleet_health", "insights", "llm_cost", "backup"),
    },
    "msp": {
        "label": "MSP / hosting partner",
        "description": "Agency set plus container management.",
        "on": _CORE + ("promote", "observe", "fleet_health", "insights", "llm_cost", "containers", "backup"),
    },
    "builder": {
        "label": "Builder",
        "description": "One person, a few instances: workflows, Code Lab, agents, the harness. No fleet meters.",
        "on": _CORE + ("codelab", "agents", "harness", "assistant", "backup"),
    },
    "everything": {"label": "Everything", "description": "All features on.", "on": _ALL},
}

DEFAULT_PROFILE = "everything"

_cache: dict = {"mtime": None, "state": None}


def _raw_state() -> dict:
    try:
        mtime = os.stat(_config.CONFIG_FILE).st_mtime_ns  # module attribute: tests repoint it
    except FileNotFoundError:
        mtime = None
    if _cache["mtime"] == mtime and _cache["state"] is not None:
        return _cache["state"]
    state = (load_config().get("features") or {}) if mtime is not None else {}
    _cache.update(mtime=mtime, state=state)
    return state


def profile_id() -> Optional[str]:
    p = _raw_state().get("profile")
    return p if p in PROFILES else None


def overrides() -> dict[str, bool]:
    raw = _raw_state().get("overrides") or {}
    return {k: bool(v) for k, v in raw.items() if k in BY_ID}


def enabled_set() -> frozenset[str]:
    """Resolved feature ids: profile defaults, then overrides, then dependencies and core."""
    base = set(PROFILES[profile_id() or DEFAULT_PROFILE]["on"])
    for fid, on in overrides().items():
        (base.add if on else base.discard)(fid)
    base.update(_CORE)
    changed = True
    while changed:
        changed = False
        for fid in list(base):
            if any(req not in base for req in BY_ID[fid].requires):
                base.discard(fid)
                changed = True
    return frozenset(base)


def enabled(feature_id: str) -> bool:
    """Unknown ids are on, so a surface without a catalog entry never disappears."""
    if feature_id not in BY_ID:
        return True
    return feature_id in enabled_set()


def gate(path: str) -> Optional[str]:
    """The disabled feature whose API prefix owns `path`, or None. Longest prefix wins."""
    on = enabled_set()
    best: Optional[tuple[int, str]] = None
    for f in CATALOG:
        for allow in f.api_allow:
            if path == allow or path.startswith(allow + "/"):
                return None
        for prefix in f.api_prefixes:
            if path == prefix or path.startswith(prefix + "/"):
                if best is None or len(prefix) > best[0]:
                    best = (len(prefix), f.id)
    if best is None or best[1] in on:
        return None
    return best[1]


def set_state(profile: Optional[str] = None, override: Optional[dict[str, Optional[bool]]] = None,
              reset_overrides: bool = False) -> dict:
    """Persist a profile choice and/or per-feature overrides. None in `override` clears one."""
    config = load_config()
    state = dict(config.get("features") or {})
    if profile is not None:
        if profile not in PROFILES:
            raise ValueError(f"unknown profile {profile!r}")
        state["profile"] = profile
        if reset_overrides:
            state["overrides"] = {}
    if override:
        cur = dict(state.get("overrides") or {})
        for fid, val in override.items():
            f = BY_ID.get(fid)
            if f is None:
                raise ValueError(f"unknown feature {fid!r}")
            if f.core and val is False:
                raise ValueError(f"{fid} is a core feature and cannot be switched off")
            if val is None:
                cur.pop(fid, None)
            else:
                cur[fid] = bool(val)
        state["overrides"] = cur
    config["features"] = state
    save_config(config)
    _cache.update(mtime=None, state=None)
    return summary()


def summary() -> dict:
    on = enabled_set()
    pid = profile_id()
    ov = overrides()
    return {
        "profile": pid,
        "profiles": [{"id": k, **{kk: vv for kk, vv in v.items() if kk != "on"}, "on": sorted(v["on"])}
                     for k, v in PROFILES.items()],
        "features": [
            {
                "id": f.id, "label": f.label, "description": f.description, "group": f.group,
                "core": f.core, "requires": list(f.requires), "enabled": f.id in on,
                "override": ov.get(f.id), "views": list(f.views), "settings_tabs": list(f.settings_tabs),
                "widgets": list(f.widgets),
            }
            for f in CATALOG
        ],
    }


def client_map() -> dict:
    """What the frontend needs on boot: enabled ids and surface-to-feature maps."""
    on = enabled_set()
    return {
        "enabled": {f.id: f.id in on for f in CATALOG},
        "views": {v: f.id for f in CATALOG for v in f.views},
        "settings_tabs": {t: f.id for f in CATALOG for t in f.settings_tabs},
        "widgets": {w: f.id for f in CATALOG for w in f.widgets},
        "profile": profile_id(),
    }
