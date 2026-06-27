# Review Pass 5 (Final Confirmation): Out-of-Process Backend Isolation (Phases 1-2)

Date: 2026-06-27
Reviewer: Codex
Scope:
- Local unpushed commits: `26a2b3c`, `a0b8434`, `64761f0`, `2ce60c8`
- Prior review: `docs/specs/2026-06-27-out-of-process-backend-isolation-review-4.md`
- Spec: `docs/specs/2026-06-27-out-of-process-backend-isolation.md`

## Per-Item Status

1. Pass-4 blocker #1 (orphan-sweep PID identity exact-token guard): **OPEN (partially closed)**
- Closed parts:
  - Guard now uses tokenized argv and exact adjacent pair matching: `_pid_is_our_worker` requires `argv[i] == "--agd-module"` and `argv[i+1] == module_id` (`backend/modules/_runtime/supervisor.py:320-333`).
  - Substring/prefix false matches are fixed. New tests assert `trivial` and `trivialmodx` do not match (`tests/test_module_runtime.py:134-136`).
  - Repro on current code confirms old-substring behavior is gone (`old_prefix=True`, `new_prefix=False`) with real worker argv.
- Remaining blocker:
  - The "cannot verify argv => skip kill" guarantee is not airtight for parse-failure cases. On `shlex.split(... )` `ValueError`, code falls back to `s.split()` (`backend/modules/_runtime/supervisor.py:312-315`) instead of treating argv as unreadable.
  - This means ambiguous/malformed command lines can still be interpreted as valid marker/id tokens and pass `_pid_is_our_worker`, so sweep may kill when verification was not reliable.
  - Concrete repro against current code: simulated malformed fallback output `python main.py --agd-module trivialmod "` yields `_process_argv -> ['python','main.py','--agd-module','trivialmod','"']` and `_pid_is_our_worker(...) -> True`.
- Required fix:
  - On `shlex.ValueError`, return `None` (skip kill) rather than `s.split()`.
  - Add a regression test that mocks malformed fallback output and asserts `_pid_is_our_worker(...) is False`.

2. Follow-up: proxy strips `clear-site-data`, `www-authenticate`, `set-cookie2`: **PARTIALLY CLOSED**
- `_STRIP_RESPONSE` now includes all requested headers (`backend/modules/_runtime/proxy.py:36-39`).
- Test asserts `set-cookie`, `clear-site-data`, and `www-authenticate` are removed (`tests/test_module_runtime.py:108-117`).
- Gap: no assertion for `set-cookie2` specifically.
- Required fix:
  - Extend the fixture/response test to emit and assert stripping of `set-cookie2` (or document why not testable in this stack).

3. Follow-up: spec 5.3 matches phase-2 env behavior (`forward_env=[]`): **CLOSED**
- Spec now states phase 2 forwards no module-declared `capabilities.env` (`docs/specs/2026-06-27-out-of-process-backend-isolation.md:253-258`).
- Code matches: loader starts worker with `forward_env=[]` (`backend/modules/__init__.py:169-173`).

4. Regression sweep (exact-token change side effects): **NO NEW REGRESSION FOUND in covered paths**
- Adjacent-pair scan is bounds-safe (`range(len(argv)-1)`) and handles `argv=None` safely (`backend/modules/_runtime/supervisor.py:329-333`).
- Module ids cannot contain spaces/shell metacharacters under enforced slug validation (`backend/module_registry.py:56`, `:68-76`, `:157-169`).
- Default-off behavior unchanged: subprocess paths remain mode-gated (`backend/modules/__init__.py:212-218`), and `stop_all()` no-ops with no workers (`backend/modules/_runtime/supervisor.py:257-259`).
- Runtime tests pass on this branch: `uv run pytest -q tests/test_module_runtime.py` (10 passed).

## New Findings (Pass-5)

### 1) MEDIUM - Parse-error fallback reopens orphan-kill verification risk (blocking)

Evidence:
- On fallback tokenization failure, `_process_argv` uses `s.split()` instead of returning unverified (`backend/modules/_runtime/supervisor.py:312-315`).
- `_pid_is_our_worker` trusts the resulting token list for kill eligibility (`backend/modules/_runtime/supervisor.py:332`).

Why this matters:
- The design intent is "cannot verify => skip kill." Parse-failure paths are inherently ambiguous; permissive fallback can produce false positives.

Required fix:
- Replace the `ValueError` fallback with `return None` and add tests for malformed fallback output.

### 2) LOW - `set-cookie2` is stripped in code but not asserted in tests (non-blocking)

Evidence:
- Code strips `set-cookie2` (`backend/modules/_runtime/proxy.py:38`).
- Test does not assert it (`tests/test_module_runtime.py:113-117`).

Required fix:
- Add explicit `set-cookie2` regression assertion.

## Final Disposition

**Not approved.**

Blocking set:
1. Parse-error fallback (`shlex.ValueError` -> `s.split()`) means PID identity verification is still not strict in all "cannot verify" cases.
