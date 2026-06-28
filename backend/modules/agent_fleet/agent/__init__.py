"""Agent graphs for the Agent Fleet module.

Each graph is a PURE factory: it builds a compiled LangGraph from an injected
model (and toolset / effects), with no host imports and no module-scope side
effects. That is what lets `langgraph dev` / Studio import a graph without
dragging in the dashboard, and what keeps the package import-safe at boot even
when the optional langgraph extra is absent.
"""
