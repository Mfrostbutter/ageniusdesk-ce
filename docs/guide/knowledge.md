# The Harness (Knowledge)

The Harness is the workspace every AI agent in AgeniusDesk works within: a markdown notes vault on your disk, plus a registry of external knowledge sources, MCP connectors, and the agent instructions document. Most AI tools start cold every session; the Harness gives the in-app [AI assistant](ai-assistant.md) (and any MCP client you point at the dashboard) a persistent place to read and write context that compounds over time. The view lives in the sidebar as **Harness** and is rendered by `frontend/js/views/knowledge.js`.

The view has two surfaces:

- **Sources, Connectors & Instructions** (collapsed config drawer): register external search sources, toggle MCP connectors, edit the agent instructions.
- **Vault** (the primary surface): the markdown notes browser, mounted inline from `frontend/js/views/notes.js`.

---

## The vault (notes)

The vault is a folder of plain markdown files inside the container's data volume at `data/workspace/`. It is Obsidian-compatible: the same files open in Obsidian if you sync the folder (iCloud, Syncthing, etc.), and the dashboard reindexes on every save. Files are the source of truth; the search index is derived and rebuildable. See [Data Model](../architecture/data-model.md) for storage details.

Backend: `backend/modules/notes/` (`storage.py` filesystem layer, `index.py` FTS5 index, `parser.py` markdown parser, `router.py` HTTP API at `/api/notes/*`).

### Folder layout

On first run the vault is scaffolded with these folders (`storage.ensure_vault`). They are conventions, not enforced rules; you can create any path.

| Folder | What goes in it | Who writes it |
|---|---|---|
| `user/` | Your own notes: clients, runbooks, ideas | You |
| `agent/` | Scratchpads the AI writes for itself | The agents |
| `docs/` | Documentation | Both |
| `workflows/` | Saved n8n workflow JSON | Both |
| `research/` | Output from add-in modules | Add-ins |
| `shared/` | Canonical facts (company info, conventions) | Both |
| `sessions/` | Per-session logs | The agents |

`AGENTS.md` at the vault root is the agent instructions document (see [Instructions](#instructions-agentsmd)). A seed `README.md` is written once on first run.

### The three-pane browser

| Pane | Contents |
|---|---|
| Left | Search box, folder tree (`📁`/`📄`), tag list with counts |
| Middle | CodeMirror 6 markdown editor (loaded lazily from a CDN) with title bar (path, **Save**, **Archive**) |
| Right | **Backlinks** to the open note, and the note's own tags |

Top-right controls: **Reindex** (rebuild the search index from disk) and **+ New** (create a note).

### Markdown syntax

The parser (`parser.py`) supports an Obsidian-compatible subset:

```markdown
---
title: Incident runbook
tags: [ops, on-call]
---

# Incident runbook

See [[Other Note]] for context, or [[folder/Note|display text]].
Link to a heading with [[Note#heading]].
Inline tags work too: #ops #on-call
```

- **Frontmatter:** YAML between leading `---` fences. Flat `key: value` and `key: [a, b]` arrays only; nested YAML is dropped silently.
- **Wikilinks:** `[[Target]]`, `[[Target|Display]]`, `[[Target#heading]]`, `[[path/Target]]`. Targets match on basename (last path segment), like Obsidian's shortest-path default.
- **Tags:** inline `#tag-name` (and `#foo/bar`), plus a frontmatter `tags:` array. Tags are lowercased and de-duped. Tags inside fenced or inline code are ignored.
- **Title:** taken from frontmatter `title`, then the first `# H1`, then the filename.

### Search, tags, backlinks

- **Search** is full-text over title, body, and tags using SQLite FTS5 with BM25 ranking (`/api/notes/search?q=`). An empty query returns recent notes by modified time. Naive queries are tokenized into prefix matches automatically, so operator-like input does not crash the index.
- **Tag filter:** `/api/notes/search?tag=ops` returns notes carrying that tag. Click a tag pill in the left pane to filter.
- **Backlinks:** `/api/notes/{path}/backlinks` lists every note whose wikilink resolves to the current note's basename.

### Create a note

1. Click **+ New** in the Vault header.
2. Enter a path, for example `user/idea.md`. A `.md` extension is added if you omit it.
3. The note is created with an `# H1` title seeded from the filename, then opened in the editor.
4. Type. Edits autosave 1.5s after you stop typing (the status indicator shows `unsaved` then `saved`). Press Cmd/Ctrl+S to save immediately.

### Archive (soft delete)

Click **Archive** in the title bar. The file moves to `.archive/<timestamp>/<path>` and is removed from the search index. Nothing is hard-deleted; recover by moving the file back and reindexing. (`DELETE /api/notes/{path}` performs the archive.)

### Editing externally and reindexing

Because the vault is plain files, you can edit it from Obsidian or any editor while the dashboard runs. After external edits, click **Reindex** (`POST /api/notes/reindex`) to rebuild the FTS index from disk so search and backlinks pick up the changes. The dashboard reindexes automatically on its own saves.

### How the assistant reads and writes the vault

The [AI assistant](ai-assistant.md) gets the vault as a tool set (`backend/modules/assistant/workspace_tools.py`), so it can operate on the same files you see:

| Tool | Action |
|---|---|
| `workspace_list` | List files/folders (optionally under a prefix) |
| `workspace_read` | Read a file's full contents |
| `workspace_write` | Create or overwrite a file (creates parent folders) |
| `workspace_append` | Append to a file (good for scratchpads/logs) |
| `workspace_search` | Full-text search across the vault |
| `workspace_archive` | Soft-delete a file to the archive |

All tool paths are relative to the vault root and sandboxed (no `..`, no absolute paths). Ask the assistant about a note and it can read and update it directly. This is the loop the Harness is built around: agents save runbooks, workflows, and notes here, and the next session reads them back.

---

## Sources (external knowledge to search)

A source is an external store the dashboard can search and fan results back to agents. It is a registry row, not a copy of the data. Backend: `backend/modules/knowledge/` (`router.py` at `/api/knowledge/*`, `backends.py` per-kind search, `storage.py` the `knowledge_sources` table).

Only one source kind is implemented: **`qdrant`** (vector search against a Qdrant collection). The dialog rejects any other kind.

### Source fields

| Field | Maps to | Notes |
|---|---|---|
| Name | `name` | Stable identifier, lowercase with dots/dashes. Must be unique (409 on collision). |
| Kind | `kind` | `qdrant` only. |
| Description | `description` | The routing signal. One sentence describing what is inside; agents read this to decide when to query the source. |
| Qdrant URL | `config.url` | e.g. `http://localhost:6333`. |
| Collection | `config.collection` | Qdrant collection name. |
| Vector name | `config.vector_name` | Named vector (default `dense`); leave blank for unnamed vectors. |
| Text payload key | `config.text_payload_key` | Payload field to return as `text` (default `text`). |
| Qdrant API key secret | `config.api_key_secret` | Optional `$NAME` ref for the Qdrant `api-key` header. |
| Embedder | `config.embedder` | `openai` (default) or `voyage` (legacy). |
| Embed model | `config.embed_model` | Default `text-embedding-3-large`. |
| OpenAI key secret | `config.openai_key_secret` | Default `$OPENAI_API_KEY`. |
| Voyage key secret | `config.voyage_key_secret` | Legacy only. |
| Enabled | `enabled` | Disabled sources are skipped by search. |

Secret fields take `$NAME` references resolved from the [encrypted secrets store](secrets.md), so keys never sit in the registry in plaintext.

### Optional RAG / embedding behavior

Search is genuine vector RAG, but only against sources you register; there is no automatic ingestion or embedding of vault notes. At query time the dashboard embeds the query string with the source's embedder (OpenAI `text-embedding-3-large` by default, or legacy Voyage `voyage-3`), then runs a Qdrant `points/query` against the configured collection and returns the matched payloads. Embedding requires a key (`$OPENAI_API_KEY` or `$VOYAGE_API_KEY`); without it the source returns an error rather than results. Collections must be built with the same embedding model, or you get dimension mismatches.

### Add a source

1. Open the **Sources, Connectors & Instructions** drawer, then the **Sources** section.
2. Click **+ Add source**.
3. Fill **Name**, **Description** (write it as a routing hint), and the Qdrant connection fields.
4. Set the embedder and the key-secret references. For a current OpenAI-embedded collection, the defaults are correct.
5. Leave **Enabled** checked and click **Save** (`POST /api/knowledge/sources`).
6. Click **Test** on the row to probe reachability and auth (`POST /api/knowledge/sources/{id}/test`). The cell shows `reachable` or the error.

Edit and Delete act on the same row. Search across sources runs at `GET /api/knowledge/search?q=&sources=a,b`; omit `sources` to fan out across every enabled source. Each source runs concurrently with its own error isolation, so one bad source never kills the response.

---

## Connectors (MCP servers in the Harness)

Connectors are MCP servers (registered in [AI assistant](ai-assistant.md) settings) that you flag as available to the Harness. Toggling a connector on does not add a new server; it sets the `knowledge_enabled` flag so the server is surfaced in the Instructions document and agents know the tool exists.

Backend: `GET /api/knowledge/connectors` lists every MCP server with its `enabled` and `knowledge_enabled` flags; `PUT /api/knowledge/connectors/{id}` sets `knowledge_enabled`.

To enable a connector:

1. Open the **Connectors** section in the config drawer.
2. Each row shows the server name, URL, description, and its overall enabled/disabled state.
3. Click the **Off** / **In Harness** pill to toggle. The change saves immediately.

If no MCP servers are registered, the section links you to Assistant settings to add one.

---

## Instructions (AGENTS.md)

The Instructions panel edits the agent instructions document, an AGENTS.md-style set of house rules prepended to every agent's system prompt. In this build the panel binds to the constitution document at `data/workspace/AGENTS.md` (backend `backend/modules/assistant/baseline/loader.py`, routes `GET`/`PUT /api/assistant/baseline`), which is the same file shown at the vault root.

> **Note:** the panel header text references the legacy path `data/baseline/baseline.md`; the loader has since moved the document to `data/workspace/AGENTS.md` and migrates the old location on boot. Edits land in `AGENTS.md` either way.

The document carries YAML frontmatter (`version`, `updated`, `overrideable_sections`) followed by the markdown body. The body is what gets injected.

### Fields

| Control | Effect |
|---|---|
| Editor | The markdown body. Use `## H2` headings as override anchors. Max 64 KiB (413 if exceeded). |
| Overrideable sections | Comma-separated H2 slugs (e.g. `tone, tools`) that a per-agent override may replace; everything else stays fixed. |
| Save | Writes with optimistic concurrency. A 409 means another tab/session changed it; reload to merge. |

If `AGD_CONSTITUTION_ENABLED=false`, the panel shows a disabled card and no instructions are injected (`GET` returns 503).

### Edit the instructions

1. Open the **Instructions (AGENTS.md)** section in the config drawer.
2. Edit the body. Char/byte count updates live.
3. Optionally adjust **Overrideable sections**.
4. Click **Save** (or Cmd/Ctrl+S). The version increments on success.

You can also edit `AGENTS.md` directly in the Vault pane or via Obsidian; both write the same file.
