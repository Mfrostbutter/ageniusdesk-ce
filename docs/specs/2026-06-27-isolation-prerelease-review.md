# Adversarial Pre-Release Review: AgeniusDesk 0.3 Community-Module Isolation

Date: 2026-06-28
Repos reviewed:
- `ageniusdesk-ce` @ `9110ee7`
- `ageniusdesk-community-modules` @ `b8a94b5`

Test run:
- `uv run pytest -q tests/test_host_bridge.py tests/test_module_runtime.py tests/test_module_security.py tests/test_assistant_completion.py tests/test_router_rbac.py tests/test_module_worker.py`
- Result: `170 passed`

## Findings

### HIGH: `notes.search` can leak out-of-scope snippets when the index contains symlink-aliased paths

**Evidence**
- `backend/modules/_runtime/bridge.py:236-242` filters search hits with `_under(path, grant.read_paths)` only (string-prefix check).
- `backend/modules/_runtime/bridge.py:114-128` + `131-143` show read/write paths are otherwise protected by resolved-path scoping (`_resolved_under`), but `notes.search` does not apply that resolved check.
- `backend/modules/notes/storage.py:95-103` keeps caller-provided relative path (`vp.rel`) while resolving symlinked absolute target (`vp.abs`).
- `backend/modules/notes/storage.py:149-156` indexes using `vp.rel`, so a symlink-aliased in-scope path can index out-of-scope content.

**Concrete repro**
I reproduced this locally:
1. Create vault symlink `research/evil -> ../user`.
2. Write via host storage to `research/evil/leaked.md` (lands on disk under `user/leaked.md`, indexed as `research/evil/leaked.md`).
3. Call bridge `notes.search` with token scoped to `read_paths=["research"]`.
4. Bridge returns snippet containing out-of-scope content (`TOPSECRET_zztag`) under `research/evil/leaked.md`.
5. `notes.read` for that same path correctly returns `403`.

So `search` can bypass effective scope while `read` is blocked.

**Required fix**
- In `notes_search`, re-validate each hit with resolved-path scoping (same model as `_note_rel_in_scope`): resolve returned path, require both declared-prefix and `_resolved_under`, and drop anything failing either check.
- Add regression test in `tests/test_host_bridge.py` that reproduces symlink-poisoned index entry and asserts `notes.search` does not return it.

---

### HIGH: Any authenticated viewer can disable isolation and manage modules (tier flip/install/uninstall)

**Evidence**
- `backend/modules/modules/router.py:16` mounts the entire modules router with `Depends(require_trusted_request)` only.
- Write endpoints on that router include isolation changes and code-management operations:
  - `POST /api/modules/isolation` at `router.py:93-104`
  - `POST /api/modules/install` at `router.py:169-193`
  - `DELETE /api/modules/{module_id}` at `router.py:195-213`
- `backend/auth_gate.py:106-116` (`require_trusted_request`) checks only that *some* identity exists; it does not enforce operator/admin role.

**Exploit path**
A low-privileged authenticated viewer can:
1. `POST /api/modules/isolation` with `{"mode":"in_process"}` (weakens boundary).
2. Install/uninstall community modules via the same router.

This is a direct policy bypass for isolation control and module execution surface.

**Required fix**
- Enforce `require_role("operator")` (or `admin`) on modules write endpoints at minimum (`/isolation` POST, `/install`, `/uninstall`; optionally `/discover` and `/inspect` too).
- Add RBAC regression tests (parallel to `tests/test_router_rbac.py`) proving viewers get `403` and operators are allowed.

---

### MEDIUM: Container orphan/volume cleanup is mode-dependent and can leave stale module runtime artifacts

**Evidence**
- Container orphan sweep runs only in container mode startup path:
  - `backend/modules/__init__.py:254-259`
- Subprocess orphan sweep is separate and mode-gated:
  - `backend/modules/__init__.py:303-310`
- Uninstall only attempts container teardown when current mode is `container`:
  - `backend/modules/modules/router.py:204-208`
- `stop_container_worker(..., remove_volume=True)` returns early when worker is not in `_workers`, skipping volume removal:
  - `backend/modules/_runtime/containers.py:397-403`

**Impact**
After crash/restart or mode transitions, stale labeled containers/volumes can survive outside the normal tracked-worker path. This is a lifecycle correctness gap and can retain module data longer than expected.

**Required fix**
- Make uninstall perform best-effort container/volume cleanup by module id regardless of current isolation mode.
- Ensure startup performs a best-effort container orphan sweep independent of selected mode (or document + enforce equivalent cleanup guarantees).
- Add tests (with mocked Docker client) for �untracked worker + remove_volume=True� and mode-transition teardown behavior.

---

### LOW: Spec enforcement matrix still contains a container import-isolation overclaim

**Evidence**
- `docs/specs/2026-06-27-out-of-process-backend-isolation.md:107` states container import isolation as enforced because host source is not in container.
- Same spec explicitly states host source is in image:
  - `...isolation.md:429-434`

**Required fix**
- Align matrix wording with implemented/documented reality: container tier currently relies on blocker + mount/env separation for this point, not host-source absence.

## What's Solid

- The `9110ee7` container confidentiality fix is correctly applied in code:
  - Mount now uses Docker volume `Subpath` for `modules/{id}` only (`backend/modules/_runtime/containers.py:269-280`).
  - No whole `/app/data` mount remains in worker container config.
- Worker/container hardening controls are materially present (`ReadonlyRootfs`, `Tmpfs`, `CapDrop`, `no-new-privileges`, pid/mem/cpu limits) in `containers.py:283-293`.
- Bridge/token model remains strong on implemented paths:
  - Per-spawn token mint/revoke (`bridge.py:51-76`).
  - Cookie-bearing bridge requests rejected (`bridge.py:159-163`).
  - Tool-free `assistant.complete` path still separated and tested.
- Proxy hygiene and streaming protections are in place and still passing tests (`test_module_runtime.py`).

## Test Coverage Assessment

- Strong coverage on subprocess worker sandbox/proxy/bridge paths (including token, cookie rejection, path traversal, proxy header stripping, request/response streaming, PID guard).
- Major gap remains for container tier:
  - No automated pytest coverage for container spawn/hardening/mount secrecy semantics.
  - No regression test specifically for the `9110ee7` secret-leak class (ensuring host secret files are unreachable in container tier).
- No RBAC tests currently cover modules management/isolation write endpoints.
- No bridge test currently covers symlink-poisoned index behavior for `notes.search`.

## Final Disposition

**NOT APPROVED** for 0.3 release as-is.

### Blocking findings
1. `notes.search` scope bypass via symlink-poisoned indexed paths (HIGH).
2. Modules management/isolation endpoints lack role floor; viewer can flip isolation/install/uninstall (HIGH).

If those are fixed and regression-tested, this should be ready for a focused re-check.

---

## Resolution (2026-06-28)

All four findings addressed. Full suite green; new regression tests added.

| Finding | Fix |
|---|---|
| HIGH: `notes.search` scope bypass via symlink-aliased index | `bridge._path_in_scope()` (non-raising resolve + `_under` + `_resolved_under`) now filters every search hit, so an index entry that resolves outside the read scope is dropped. Test `test_search_excludes_symlink_aliased_hits` reproduces the symlink-poisoned entry and asserts it is not returned. |
| HIGH: modules write endpoints lack a role floor | `require_role("operator")` added to `POST /api/modules/isolation`, `/discover`, `/inspect`, `/install`, and `DELETE /{module_id}`. Reads (`GET /api/modules`, `/isolation`) stay viewer-open. Tests `test_viewer_blocked_modules_writes_reads_open` + `test_operator_allowed_modules_isolation`. |
| MEDIUM: mode-gated container/volume cleanup | `uninstall` now tears down the container + volume regardless of current mode; `stop_container_worker` removes the labeled container + volume + revokes the grant even when the worker is untracked; `start_isolated_workers` sweeps container orphans before branching (covers container->subprocess). Test `test_stop_container_worker_untracked_cleans_up`. The container->in_process switch is documented as not auto-sweeping (re-enter an isolated mode or remove manually). |
| LOW: spec matrix container import overclaim | Matrix rows corrected: container `import backend.*` is enforced by the blocker + `/app` off the worker sys.path (host source ships in the image but is unimportable and secret-free); off-disk reads/`/proc`/uid rows reworded to match the v1 root worker + subpath mount; egress and syscalls marked Partial. |

**Disposition: blockers cleared.** Ready for the focused re-check.
