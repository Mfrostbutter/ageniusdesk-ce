# Review: Out-of-Process Backend Isolation Spec

Date: 2026-06-27
Reviewer: Codex (adversarial review pass)
Spec under review: `docs/specs/2026-06-27-out-of-process-backend-isolation.md`
Decision: Not approved yet (blocking issues below)

## Scope Reviewed

- Spec claims, threat model, enforcement matrix, and implementation plan.
- Current host implementation in `ageniusdesk-ce`.
- Current reference consumer implementation in `ageniusdesk-community-modules/modules/youtube-research`.
- Frontend bridge behavior relevant to websocket/event claims.

## Findings (Ordered by Severity)

### 1) CRITICAL - Module id path traversal enables destructive delete outside modules dir

Decision: Blocker. Must be fixed before shipping isolation work.

Why this is critical:
- `manifest.id` is not constrained to a safe slug.
- The value is used directly in filesystem paths.
- Uninstall and reinstall paths can recurse-delete outside `data/modules`.

Evidence:
- `backend/module_registry.py:99` (`id: str` with no validation)
- `backend/modules/modules/installer.py:373` (`final_dir = COMMUNITY_MODULES_DIR / manifest.id`)
- `backend/modules/modules/installer.py:375` (`shutil.rmtree(final_dir)`)
- `backend/modules/modules/installer.py:414` (`target = COMMUNITY_MODULES_DIR / module_id`)
- `backend/modules/modules/installer.py:418` (`shutil.rmtree(target)`)

Exploit class:
- Module id like `..` can resolve to parent directory behavior in recursive delete calls.

Required fix:
- Enforce strict id regex (for example `^[a-z0-9][a-z0-9-_]{1,63}$`) at manifest parse time and all write/uninstall entry points.
- Resolve and re-check targets with `relative_to(COMMUNITY_MODULES_DIR.resolve())` before any delete/move.

---

### 2) HIGH - `assistant.complete` design in spec currently points at a tool-enabled path

Decision: Blocker in spec design. Needs explicit non-tool execution path.

Why this matters:
- Spec says bridge should call existing `providers._dispatch_chat`.
- Current `_dispatch_chat` can expose tool calls, including mutating operations.
- That violates the intended narrow bridge contract for module completions.

Evidence:
- Spec reference: `docs/specs/2026-06-27-out-of-process-backend-isolation.md:260`
- Tool-enabled routing: `backend/modules/assistant/providers.py:460`
- Tool payload injection: `backend/modules/assistant/providers.py:783`
- Mutating tools include actions like trigger/import:
  - `backend/modules/assistant/tools.py:55` (`trigger_workflow`)
  - `backend/modules/assistant/tools.py:141` (`import_workflow`)

Required fix:
- Add a dedicated host-side `assistant.complete` executor that is tool-free by construction.
- Do not reuse `_dispatch_chat` unless it grows a hard `tools_disabled=True` enforcement path.

---

### 3) HIGH - Import-isolation plan has bootstrap contradiction

Decision: Blocker in spec architecture until clarified.

Why this matters:
- Spec proposes worker runtime under `backend/modules/_runtime/`.
- If worker bootstrap imports `backend` to start, the import ban guardrail is undermined at process start.
- This weakens the claim that worker-side `import backend.*` is blocked by path strategy.

Evidence:
- Spec reference: `docs/specs/2026-06-27-out-of-process-backend-isolation.md:461`
- Current package install includes backend package:
  - `Dockerfile:7` (`pip install '.[assistant]'`)
  - `pyproject.toml:72` (`include = ["backend*"]`)

Required fix:
- Move worker bootstrap entrypoint outside `backend` package namespace, or launch via path-exec script.
- Keep worker import graph independent from host package namespace.

---

### 4) MEDIUM - Spec claims existing v0.3 module websocket bridge that current frontend does not provide

Decision: Spec correction required (or implementation task must be added).

Why this matters:
- Spec states frontend community bridge already routes module-scoped websocket messages into iframe.
- Current frontend bridge only supports `fetch`, `notify`, `navigate`, `openInHarness`.
- The current youtube module frontend explicitly uses polling.

Evidence:
- Spec claim: `docs/specs/2026-06-27-out-of-process-backend-isolation.md:278`
- Bridge whitelist: `frontend/js/community-modules.js:75`
- No websocket bridge methods in same file.
- Polling statement: `ageniusdesk-community-modules/modules/youtube-research/static/module.js:8`

Required fix:
- Either change spec wording to "not yet present" or add explicit phase to implement host->iframe websocket event relay.

---

### 5) MEDIUM - Threat model text overstates current scanner severity for host imports

Decision: Documentation accuracy fix required.

Why this matters:
- Spec says obvious hostile host imports are "caught by scanner today (CRITICAL/HIGH findings)".
- Current scanner labels `backend.*` imports as `INFO`.

Evidence:
- Spec text: `docs/specs/2026-06-27-out-of-process-backend-isolation.md:55`
- Scanner behavior: `backend/modules/modules/scanner.py:246-249`

Required fix:
- Reword present-tense claim, or implement scanner severity change before making the claim.

---

### 6) MEDIUM - Notes bridge validation plan is incomplete for directory-level operations

Decision: Design gap; fix before implementation.

Why this matters:
- Spec says reuse `storage.resolve()` for notes path safety.
- `storage.resolve()` is file-oriented and appends `.md`.
- Bridge methods include folder and move operations that need directory-safe validators too.

Evidence:
- Spec references: `docs/specs/2026-06-27-out-of-process-backend-isolation.md:242` and `:244-247`
- `storage.resolve()` behavior: `backend/modules/notes/storage.py:93-95`

Required fix:
- Introduce explicit dir-safe and note-safe path validators in bridge layer.
- Apply declared `read_paths`/`write_paths` checks after canonicalization.

---

### 7) LOW - Versioning prose conflicts with current app version

Decision: Non-blocking, but should be corrected for clarity.

Evidence:
- Spec says "v0.3 (current minor)": `docs/specs/2026-06-27-out-of-process-backend-isolation.md:383`
- Current project version: `pyproject.toml:3` is `0.2.0`

Required fix:
- Update wording to avoid timeline ambiguity.

## Verified Accurate in the Spec

- Current in-process coupling in loader is correctly described:
  - `backend/modules/__init__.py:84-116`
  - `backend/main.py:376`
- The six youtube-research host couplings are correctly identified:
  - `router.py` auth dependency and websocket usage
  - `artifacts.py` direct vault/index usage and filesystem operations
  - `llm.py` host assistant config + secret resolution
  - `store.py` host DB coupling
- Packaging gotcha about `backend` being installed into site-packages is correct:
  - `Dockerfile:7`
  - `pyproject.toml:72`

## Final Disposition

- Current spec direction is strong and intentionally honest about guardrail vs containment.
- Approval is withheld due to 3 blockers (1 critical + 2 high) that materially affect safety and correctness.
- Once blockers are addressed, this is a solid candidate for implementation.
