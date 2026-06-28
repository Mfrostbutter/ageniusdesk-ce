# Code Lab

Code Lab is an in-browser editor for building n8n Code-node logic and whole workflows. It pairs a Monaco editor (the same engine VS Code uses) with an AI Code Assistant that can write, explain, and fix code, draft full workflow JSON from a description, and assemble structured agent system prompts. Finished work can be pushed straight into a new workflow on the active n8n instance.

Source: `frontend/js/views/codelab.js`. AI calls go to `POST /api/assistant/chat` with `surface: "codelab"` (`backend/modules/assistant/router.py`). For provider/model/key configuration, see [AI Assistant & Models](ai-assistant.md). For storing provider keys, see [Secrets](secrets.md).

## Layout

The view has two panels.

| Panel | Contents |
|---|---|
| Editor (left) | Mode toggle, template/language pickers, Prompt Builder button, Format/Copy/Send buttons, the Monaco editor, and an OUTPUT log at the bottom |
| AI sidebar (right) | Provider/model picker (with optional fallback), Test connection, a chat message log, quick-action buttons, and a chat input |

The editor uses a dark theme with n8n-aware autocomplete: typing `$` surfaces n8n globals (`$input`, `$json`, `$node`, `$env`, `$now`, and others) plus snippets for common patterns (transform items, filter items, fetch).

## Three modes

A toggle at the top left switches the whole view between three modes.

| Mode | Button | What the editor holds | Send button |
|---|---|---|---|
| Code Node | "Code Node" | A JavaScript/TypeScript/Python snippet for one n8n Code node | "Send to n8n" |
| Workflow Builder | "Workflow Builder" | A complete n8n workflow as JSON | "Import to n8n" |
| Agent Builder | "Agent Builder" | A LangGraph or PydanticAI agent (Python) | "Register to Agent Fleet" |

Switching modes swaps the editor language and, if the current content does not match the new mode, loads a starter (a code template in Code Node mode, a one-node manual-trigger scaffold in Workflow Builder mode, an agent scaffold in Agent Builder mode). Content that already matches is preserved. The template + language pickers show in Code Node mode; a framework + starter picker shows in Agent Builder mode.

## Code Node mode walkthrough

1. Confirm the mode toggle shows "Code Node" highlighted.
2. Pick a starting point from the Template dropdown:

   | Template | Produces |
   |---|---|
   | Blank | Minimal `return $input.all();` stub |
   | Transform Items | Map over items and add/modify fields |
   | Filter Items | Keep only matching items |
   | HTTP Request | `fetch` call with auth header from `$env` |
   | Aggregate Data | Combine all items into one summary item |
   | Split into Batches | Chunk items into fixed-size groups |
   | Webhook Response | Format a single response object |

3. Pick the language from the Language dropdown: JavaScript, TypeScript, or Python. This sets Monaco's syntax mode for the editor.
4. Write or edit code. Autocomplete suggests n8n globals and snippets as you type.
5. Use the toolbar buttons as needed:
   - **Format** runs Monaco's document formatter.
   - **Copy** copies the editor contents to the clipboard.
6. Use the AI sidebar to write, explain, or fix code (see [Using the AI Code Assistant](#using-the-ai-code-assistant)).
7. Click **Send to n8n** to create a new workflow on the active instance.

### Send to n8n (Code Node mode)

Send to n8n posts to `POST /api/n8n/import` and builds a two-node workflow:

- A **Manual Trigger** node, wired to
- A **Code** node (`n8n-nodes-base.code`, typeVersion 2) whose `jsCode` parameter is set to the editor contents.

The new workflow is named `Code Lab — <timestamp>`. On success a toast shows the new workflow ID and the OUTPUT log records it. The workflow targets whichever n8n instance is active in the dashboard.

> Note: the Code node is always created with the editor text in the `jsCode` field regardless of the Language picker. The picker controls editor syntax highlighting, not the n8n node language. For a Python Code node, set the node's language inside n8n after import.

## Workflow Builder mode walkthrough

Workflow Builder generates a complete, importable workflow JSON from a plain-language description.

1. Click **Workflow Builder** in the mode toggle. The editor switches to JSON and loads a one-node scaffold if it was holding code.
2. In the AI input (placeholder "Describe the workflow to build…"), describe the workflow. Or click a quick-action prompt:
   - Webhook → Slack
   - Daily API → Sheet
   - Form → Email
3. The assistant is instructed to return a single ```json``` block containing top-level `name`, `nodes`, `connections`, and `settings`. When it does, the JSON is extracted and loaded straight into the editor, and a toast confirms generation. If the model replies with prose instead of JSON (often because it needs more detail), the reply is shown in the chat so you can refine the request.
4. Review and edit the generated JSON in the editor.
5. Click **Import to n8n**. The editor contents are parsed and validated client-side:
   - Must be valid JSON.
   - Must contain a non-empty `nodes` array.
   - Missing `name`, `connections`, or `settings` are filled with defaults before import.
6. On success a toast shows the imported workflow ID; the OUTPUT log records it.

> Tip: the built-in [`n8n-mcp`](https://github.com/czlonkowski/n8n-mcp) server by czlonkowski (MIT) gives the assistant deep n8n node knowledge plus workflow validation and create/update tools, which improves the accuracy of generated node types and parameters. It auto-installs when Docker is available (Settings → MCP Servers → n8n Intelligence). See [MCP server tools](ai-assistant.md#mcp-server-tools).

## Agent Builder mode

Agent Builder mode builds a LangGraph or PydanticAI agent that runs in the [Agent Fleet](agent-fleet.md).

1. Click **Agent Builder** in the mode toggle. The editor switches to Python and loads an agent scaffold.
2. Pick a **framework** (LangGraph or PydanticAI) and a **starter** (ReAct, human-in-the-loop, parallel fan-out, or blank). Changing either reloads the scaffold.
3. Write the agent. A LangGraph agent is a pure factory — `build(llm, tools, checkpointer=None)` returning a compiled graph — importing only langgraph/langchain; AgeniusDesk injects the model and the tools you select. The AI sidebar is agent-aware (quick actions: Explain, Add a tool, Make it HITL, Fix).
4. Click **Register to Agent Fleet**. Name it, pick its model, select the tools it may call, and toggle human-in-the-loop. It is written to your vault under `agents/<id>/` (a pure `graph.py` factory + an `agent.json` manifest) and appears in the Agent Fleet immediately, no restart.

Agent Builder mode needs the agent dependency extra installed (`AGD_EXTRAS="assistant,langgraph"`); without it the Register button still saves the files, but running needs the extra. Full details, including running + monitoring, are in [Agent Fleet](agent-fleet.md).

## Using the AI Code Assistant

The right sidebar is a chat scoped to the editor. Every message sends the current editor contents as context, so the assistant always sees what you are working on.

1. Type a question in the chat input and click **Ask**, or
2. Click a quick action. The quick actions change with the mode:

   | Code Node quick actions | Workflow Builder quick actions |
   |---|---|
   | Explain, Fix Bugs, Optimize, Add Error Handling, Fix n8n Syntax | Webhook → Slack, Daily API → Sheet, Form → Email |

3. In Code Node mode, if the reply contains a code block, an **Apply Code to Editor** button appears under the message. Clicking it replaces the editor contents with that code.
4. In Workflow Builder mode, a returned JSON block is loaded into the editor automatically (no Apply button).

If a response was served by the configured fallback model instead of the primary, an inline notice shows which provider/model answered and why the primary failed.

## Per-session provider/model override

The sidebar has its own Provider and Model dropdowns. These default to the Code Lab area's saved provider and model (set in [AI Assistant & Models](ai-assistant.md)), read from `GET /api/assistant/config`.

- Changing the dropdowns overrides the model for **this browser session only**. The override is held in `sessionStorage` and sent with each chat request as `override`; it never mutates the saved configuration.
- A hint under the picker shows whether the model list is live, cached, or a default fallback list.
- **Test connection** validates the selected provider's stored key (via `POST /api/admin/assistant/test`) and reports success or the error.
- **+ Fallback** reveals a second card where you can pick a provider/model used only if the primary call fails (5xx / 429 / timeout). It has its own Test connection button.
- If you save a new Code Lab default in the Models area while Code Lab is open, the sidebar picker updates live and the session override is cleared.
- If a selected provider has no stored key, the chat returns a "not configured" message and the picker reverts to the saved default.

Provider keys are resolved server-side from the Secrets store by reference (`$ANTHROPIC_KEY` / `$OPEN_AI_KEY` / `$OPEN_ROUTER_KEY`). Plaintext keys never reach the browser. See [Secrets](secrets.md).

## Prompt Builder

The Prompt Builder assembles a structured agent system prompt that you can drop into a Code node string or paste into an n8n AI Agent node. Open it with the **Prompt Builder** button in the editor toolbar.

It is a modal with seven sections:

| Section | Purpose |
|---|---|
| Identity & Role | Who the agent is |
| Objective | The single primary goal |
| Capabilities & Tools | What it can do and which tools/data it can reach |
| Workflow / Process | The step-by-step process to follow |
| Rules & Constraints | Hard rules and guardrails |
| Output Format | How responses should be structured |
| Examples | Optional few-shot examples |

### Authoring a prompt

1. Click **Prompt Builder**.
2. Fill the sections by hand, or use the AI draft:
   - Type a one-line description in the field at the top (e.g. "triages inbound support email").
   - Click **Draft with AI** (or press Enter). The configured Code Lab model is asked to return a JSON object keyed by the seven section names; each returned section is written into its textarea. A status line reports how many sections were filled.
   - The draft uses the same provider/model/fallback as the sidebar picker.
3. Review and edit the textareas.
4. Output the prompt:
   - **Copy prompt** composes the filled sections into a single markdown document (each section becomes a `# Section` heading) and copies it to the clipboard.
   - **Push to Code Lab** loads the composed prompt into the Monaco editor (switching it to markdown) so you can wrap it in code or keep editing.
   - **Clear all** empties every field.
   - **Close** (or Escape) dismisses the modal.

Only sections with content are included in the composed output.

## Output log

The OUTPUT panel below the editor records import results (workflow IDs, names). Clear it with the **Clear** button in its header.

## Related

- [Agent Fleet](agent-fleet.md) - run + monitor the LangGraph / PydanticAI agents you build in Agent Builder mode
- [AI Assistant & Models](ai-assistant.md) - configure the provider, model, instructions, and fallback for the Code Lab area
- [Secrets](secrets.md) - store the provider API keys Code Lab resolves by reference
- [The Harness](knowledge.md) - shared house rules prepended to every assistant area
- Architecture: [../architecture/](../architecture/)
