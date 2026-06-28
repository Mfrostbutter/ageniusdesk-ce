---
title: n8n Skills (the harness skill library)
tags: [skills, n8n, meta]
---

# n8n Skills

This folder is a curated library of focused n8n skills. Each subfolder is one
skill: a `SKILL.md` entry point plus reference `.md` files it pulls in as needed.
The AgeniusDesk assistant (and any MCP client pointed at this dashboard) reads
these on demand to build and debug n8n workflows correctly the first time.

## How an agent should use this folder

1. When the task is about n8n (building or editing a workflow, configuring a
   node, writing an expression or Code node, wiring credentials, handling
   errors, debugging a failure), scan the table below and pick the relevant
   skill(s) by their "use when" description.
2. Read that skill's `SKILL.md` first. Load its reference files only as the work
   requires (each `SKILL.md` says which file covers what).
3. Pair the skills with this dashboard's **n8n-mcp** MCP server (Settings > MCP
   Servers): the skills tell you *how* to build; the MCP tools (`search_nodes`,
   `get_node`, `validate_workflow`, `n8n_create_workflow`, ...) let you *do* it
   against a live instance. `using-n8n-mcp-skills` is the router that ties the
   two together; start there if unsure.
4. In **Code Lab**, lead with `n8n-code-javascript` (or `n8n-code-python`) and
   `n8n-expression-syntax`.

## The skills

| Skill | Use when |
|---|---|
| [using-n8n-mcp-skills](using-n8n-mcp-skills/SKILL.md) | **Router.** Building, editing, validating, testing, or debugging any n8n workflow through the n8n-mcp server. Start here; it routes to the rest. |
| [n8n-mcp-tools-expert](n8n-mcp-tools-expert/SKILL.md) | Using the n8n-mcp tools effectively: searching nodes, validating configs, templates, managing workflows/credentials, auditing an instance. |
| [n8n-workflow-patterns](n8n-workflow-patterns/SKILL.md) | Designing workflow structure: webhook, API, database, AI, batch, and scheduled architectures. |
| [n8n-node-configuration](n8n-node-configuration/SKILL.md) | Configuring nodes: property dependencies, required fields, operation-aware patterns by node type. |
| [n8n-validation-expert](n8n-validation-expert/SKILL.md) | Interpreting validation errors and warnings, telling false positives from real fixes, the validation loop. |
| [n8n-expression-syntax](n8n-expression-syntax/SKILL.md) | Writing/fixing `{{ }}` expressions, `$json`/`$node` access, mapping data between nodes. |
| [n8n-code-javascript](n8n-code-javascript/SKILL.md) | Writing JavaScript in Code nodes: `$input`/`$json`, `$helpers`, DateTime, batching, per-item vs all-items. |
| [n8n-code-python](n8n-code-python/SKILL.md) | Writing Python in Code nodes: `_input`/`_json`, the standard library, and Python's limitations in n8n. |
| [n8n-code-tool](n8n-code-tool/SKILL.md) | The AI-agent-callable Custom Code Tool (`@n8n/n8n-nodes-langchain.toolCode`) — different contract from the Code node. |
| [n8n-error-handling](n8n-error-handling/SKILL.md) | Making failures loud, structured, and recoverable: error workflows, node error outputs, API/webhook response shapes. |
| [n8n-agents](n8n-agents/SKILL.md) | Building AI nodes (`@n8n/n8n-nodes-langchain.*`): agents, tools, memory, RAG, structured output, sub-workflow-as-tool. |
| [n8n-binary-and-data](n8n-binary-and-data/SKILL.md) | Files and binary data: images, PDFs, uploads/downloads, base64, multimodal input, file-as-tool. |
| [n8n-subworkflows](n8n-subworkflows/SKILL.md) | Reusable, composable sub-workflows: extracting shared logic, Execute Workflow, anything over ~10 nodes. |
| [n8n-multi-instance](n8n-multi-instance/SKILL.md) | When n8n-mcp targets more than one instance (prod vs staging, several teams/clients). |
| [n8n-self-hosting](n8n-self-hosting/SKILL.md) | Deploying production self-hosted n8n to a Linux VM: Docker Compose behind Caddy, single vs queue mode, day-2 ops. |

## Make it yours

These files are plain markdown in your harness vault. Edit them, add your own
house conventions, or drop in new skills as subfolders with their own `SKILL.md`.
They are seeded once on first run and never overwritten, so your edits stick.

## Attribution

Vendored from [czlonkowski/n8n-skills](https://github.com/czlonkowski/n8n-skills)
(MIT), "n8n skillset for Claude Code to build flawless n8n workflows." The MIT
license and upstream notices are kept alongside these files
(`LICENSE-n8n-skills.txt`, `NOTICES-n8n-skills.txt`).
