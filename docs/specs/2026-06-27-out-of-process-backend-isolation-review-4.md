# Review Pass 4: Out-of-Process Backend Isolation (Phases 1 + 2)

Date: 2026-06-27
Reviewer: Codex
Scope:
- Local unpushed commits: `26a2b3c` (phase 1), `a0b8434` (phase 2), `64761f0` (pass-3 fixes)
- Spec: `docs/specs/2026-06-27-out-of-process-backend-isolation.md`
- Prior review: `docs/specs/2026-06-27-out-of-process-backend-isolation-review-3.md`

## Per-Finding Status (Pass-3 -> Pass-4)

| # | Pass-3 finding | Status | Evidence (file:line) | Required fix |
|---|---|---|---|---|
| 1 | Orphan-sweep PID-reuse safety | **Partially-closed** | Worker spawn now tags argv with marker+id (`backend/modules/_runtime/supervisor.py:40-43`, `:123-125`); orphan sweep now verifies before kill and skips when unverifiable (`:307-318`, `:333-347`); cmdline fallback exists for `/proc`, `ps`, PowerShell (`:284-304`). Remaining issue: verification is substring-based (`WORKER_MARKER in cmd and str(module_id) in cmd`, `:318`), which can false-positive (`module_id="trivial"` matches `--agd-module trivialmod`). Test coverage does not catch this (`tests/test_module_runtime.py:122-129`). | Parse command line into exact argv tokens and require exact `--agd-module` token followed by exact module id. Keep "cannot verify -> skip" behavior. Add regression tests for substring false-positive and cmdline-unreadable skip. |
| 2 | `declared_env` secret leak path | **Confirmed-closed** | Loader no longer sources from manifest declared env and passes empty host-controlled list (`backend/modules/__init__.py:169-173`); supervisor threads that through as `forward_env` (`backend/modules/_runtime/supervisor.py:95-108`, `:110-117`, `:243-250`); worker env helper renamed to `extra_allow` with host-controlled contract (`agd_module_worker/sandbox.py:58-74`, `:80-85`). `capabilities.env` is no longer used on worker spawn path. | None. |
| 3 | Worker `Set-Cookie` response leak | **Confirmed-closed** | `set-cookie` added to response strip list (`backend/modules/_runtime/proxy.py:31-35`); filtering is case-insensitive (`:42-43`); regression test asserts cookie header/cookie jar are clean (`tests/test_module_runtime.py:106-113`). | None. |
| 4 | Request-body buffering instead of streaming | **Confirmed-closed** | Proxy now forwards `content=request.stream()` (`backend/modules/_runtime/proxy.py:57-60`); no `await request.body()` remains in proxy; existing GET routes still pass via runtime tests (`tests/test_module_runtime.py:71-93`), and POST body forwarding is validated (`:115-119`). | None. |
| 5 | Watchdog/backoff spec overclaim | **Confirmed-closed (doc-only)** | Spec now clearly marks restart/backoff as not yet implemented (`docs/specs/2026-06-27-out-of-process-backend-isolation.md:398-404`), attack #10 reflects same (`:556-559`), and testing section marks it pending (`:635-636`). Code still has 502-on-dead with no watchdog (consistent with doc) (`backend/modules/_runtime/proxy.py:47-49`). | None for pass-3 closure. Implementation still required before "isolated modules ship" per spec. |
| 6 | Default-off side effects in `in_process` mode | **Confirmed-closed** | `stop_all()` now early-returns with no workers (`backend/modules/_runtime/supervisor.py:256-258`); orphan sweep is only invoked when isolation mode is subprocess (`backend/modules/__init__.py:212-218`); subprocess worker start path is mode-gated (`:117-119`, `:160-174`); regression test validates no `data/run` side effect (`tests/test_module_runtime.py:131-140`). | None. |
| 7 | Coverage gaps around security/lifecycle assertions | **Partially-closed** | New meaningful tests added for response cookie stripping (`tests/test_module_runtime.py:106-113`), request-body forwarding (`:115-119`), PID guard (`:122-129`), default-mode no side effects (`:131-140`). Remaining gap: PID guard test does not assert exact-token matching and misses substring false-positive path tied to finding #1. | Add tests that fail on substring matches (`trivial` vs `trivialmod`) and tests that enforce "cannot verify cmdline -> skip kill" behavior. |
| 8 | Proxy-secret compare not constant-time | **Confirmed-closed** | Worker now uses `hmac.compare_digest` (`agd_module_worker/main.py:24`, `:59-61`). | None. |
| 9 | Spec status block stale | **Confirmed-closed** | Header now states phases 1-2 landed and pending items explicitly (`docs/specs/2026-06-27-out-of-process-backend-isolation.md:3`). | None. |

## Regression Sweep on the Fixes

- `request.stream()` forwarding did not introduce method/path regressions in covered routes: GET no-body requests and POST body forwarding both pass (`tests/test_module_runtime.py:71-93`, `:115-119`). The proxy reads the request stream once and does not double-read (`backend/modules/_runtime/proxy.py:57-60`).
- Extra argv marker does not break worker startup parsing: `main.py` only special-cases `--selfcheck`; otherwise it runs normally and ignores extra args (`agd_module_worker/main.py:111-114`).
- PID identity check is still weak due substring matching (`backend/modules/_runtime/supervisor.py:318`), so this fix is not airtight yet.
- Default-off still appears clean: `start_worker`/`sweep_orphans` are only reached in subprocess mode (`backend/modules/__init__.py:117-119`, `:212-218`), and `stop_all` no-op prevents shutdown writes when no workers were started (`backend/modules/_runtime/supervisor.py:256-258`).
- `Set-Cookie` is stripped; additional auth-sensitive response headers (`www-authenticate`, `clear-site-data`) are still forwarded today (`backend/modules/_runtime/proxy.py:34`). This is a hardening consideration, not a pass-3 reopen by itself.

## New Findings (Introduced/Exposed in Pass-4)

### 1) MEDIUM - Spec drift on env forwarding contract (code and spec disagree)

Evidence:
- Spec still says module-declared `capabilities.env` entries are forwarded if non-secret (`docs/specs/2026-06-27-out-of-process-backend-isolation.md:252-253`).
- Current code intentionally does **not** source worker env from module declarations and passes `forward_env=[]` (`backend/modules/__init__.py:169-173`).

Why this matters:
- The spec is currently describing a weaker and different contract than implemented code, which creates review and operator confusion around expected env exposure.

Required fix:
- Update Section 5.3 (and any dependent prose in Sections 11/14) to match implemented behavior: no module-declared env forwarding in phase 2.

### 2) LOW - Response-header hardening gap beyond `Set-Cookie`

Evidence:
- Response strip list now blocks `set-cookie` but still forwards other sensitive browser-facing headers (`backend/modules/_runtime/proxy.py:34`).

Why this matters:
- Untrusted module responses can still influence host-origin browser behavior via headers like `Clear-Site-Data` or `WWW-Authenticate`.

Required fix:
- Decide and document a tighter response-header policy (allowlist preferred), and add tests for blocked sensitive headers.

## Final Disposition

**Not approved.**

Blocking set:
1. Pass-3 finding #1 remains only partially closed: orphan PID identity proof is still substring-based (`backend/modules/_runtime/supervisor.py:318`), so kill safety is not yet strict.

Non-blocking follow-ups:
1. Close the remaining PID-guard test gaps (finding #7 partial).
2. Align spec Section 5.3 with current env behavior.
3. Consider broader response-header hardening policy.
