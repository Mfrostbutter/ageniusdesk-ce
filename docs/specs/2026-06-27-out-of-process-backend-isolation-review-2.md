# Review Pass 2: Out-of-Process Backend Isolation

Date: 2026-06-27
Reviewer: Codex
Scope:
- Landed fix commit `2865246` (module-id traversal hardening)
- Revised spec `docs/specs/2026-06-27-out-of-process-backend-isolation.md`

## Findings (Ordered by Severity)

### 1) HIGH - Landed module-id validation is not fully safe on Windows canonical paths

Decision: **Blocking**

Evidence:
- `backend/module_registry.py:54-63` allows ids matching `^[a-z0-9][a-z0-9._-]{0,63}$` with only a `".."` substring ban.
- This still allows `a.` and Windows device names like `nul`, `con`, `aux`, etc.
- Installer path enforcement is containment-only:
  - `backend/modules/modules/installer.py:69-85`
  - `backend/modules/modules/installer.py:393-399`
  - `backend/modules/modules/installer.py:434-438`

Why this is a real gap:
- On Windows, trailing-dot names canonicalize/alias (`a.` maps to `a`), so two distinct ids can target the same directory. That can overwrite/delete sibling module data inside `data/modules` during install/uninstall.
- Windows device names are also accepted by current regex and can produce non-filesystem behavior (install/uninstall errors and undefined targeting semantics).

Current tests miss this class:
- `tests/test_module_security.py:47-49` unsafe set does not include trailing-dot aliases or Windows reserved names.

Required fix:
- Tighten id policy to a cross-platform canonical slug (recommended: disallow `.` entirely, or at minimum disallow trailing `.` and reserved device names).
- Add explicit normalization/canonicalization guard before filesystem operations (Windows-aware).
- Add regression tests for `a.`, `nul`, `con`, `aux`, `prn`, `com1`..`com9`, `lpt1`..`lpt9`.

---

### 2) MEDIUM - Revised spec is still internally inconsistent on `broadcast` scope/timing

Decision: **Non-blocking for code fix, blocking for spec approval as implementation guide**

Evidence:
- Says `broadcast` is optional/future and not needed in v1:
  - `docs/specs/2026-06-27-out-of-process-backend-isolation.md:343-359`
- But still states youtube-research should replace broadcast call:
  - `docs/specs/2026-06-27-out-of-process-backend-isolation.md:436`
- Still says youtube-research needs host WS via broadcast bridge in proxy section:
  - `docs/specs/2026-06-27-out-of-process-backend-isolation.md:208-209`
- Phase plan still includes broadcast namespace as part of main flow:
  - `docs/specs/2026-06-27-out-of-process-backend-isolation.md:577`

Required fix:
- Make one v1 truth and apply it everywhere.
- If v1 is polling-only for youtube-research, remove all “replace with bridge.broadcast” instructions from re-port section and proxy section.
- Keep `broadcast` in an explicit later phase only.

---

### 3) LOW - Spec/code mismatch in id regex details and wording drift

Decision: **Non-blocking**

Evidence:
- Spec prerequisite regex says `{1,63}`:
  - `docs/specs/2026-06-27-out-of-process-backend-isolation.md:170`
- Code uses `{0,63}` (1-char ids allowed):
  - `backend/module_registry.py:54`
- Tests confirm 1-char ids are intentionally accepted:
  - `tests/test_module_security.py:48-49`, `:68-69`

Required fix:
- Align spec text with actual enforced rule (or change code+tests if 2-char min is desired).

## Verification Notes (What Is Closed Correctly)

- Pass-1 finding #1 (CRITICAL traversal delete) is substantially fixed for classic traversal:
  - Manifest id validator is active (`field_validator` on `ModuleManifest.id`) in `backend/module_registry.py:145-157`.
  - Install/uninstall destructive paths now use `_safe_community_dir` (`installer.py:393`, `:434`).
  - Targeted tests are meaningful and pass (`62 passed` in `tests/test_module_security.py`).
- Coverage check on id-derived destructive entry points:
  - `install()` final dir delete+move: guarded.
  - `uninstall()` target delete: guarded.
  - Router delete path delegates to guarded uninstall (`backend/modules/modules/router.py:144-152`).
- Pydantic v2 validator wiring is correct (`field_validator` import+usage), and built-ins currently do not regress (existing built-in ids are lowercase slug-safe).

## Pass-1 Closure Check on Revised Spec

- #2 (`assistant.complete` must be dedicated tool-free, not `_dispatch_chat`): **Closed in text** (`spec:325-334`), with clear prohibition on `tools` payload.
- #3 (bootstrap outside `backend` package): **Closed in text** (`spec:271-277`, `:547-550`, `:568-571`).
- #4 (broadcast overclaim): **Not fully closed** due internal contradictions (Finding #2).
- #5 (scanner severity wording): **Closed in text** (`spec:66-71`).
- #6 (dir-safe notes validators): **Closed in text** (`spec:309-316`).
- #7 (version prose): **Closed in text** (`spec:7-8`, `:463-469`).
- #1 prerequisite documented + code fix landed: **Partially closed** (classic traversal fixed; Windows canonical-name gap remains, Finding #1).

## Final Disposition

**Not approved**.

Blocking set:
1. HIGH: Windows canonical-path id gap in landed fix (`a.` aliases and reserved names).

Once that is fixed, this pass can move to approved for the code fix portion; spec then needs the broadcast consistency cleanup to be implementation-safe.
