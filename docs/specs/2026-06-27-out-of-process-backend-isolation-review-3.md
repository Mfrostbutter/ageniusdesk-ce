# Review Pass 3: Out-of-Process Backend Isolation (Phases 1 + 2)

Date: 2026-06-27
Reviewer: Codex
Scope:
- Local unpushed commits: `26a2b3c` (phase 1), `a0b8434` (phase 2)
- Spec: `docs/specs/2026-06-27-out-of-process-backend-isolation.md`
- Reference consumer: `ageniusdesk-community-modules/modules/youtube-research`

## Findings (Ordered by Severity)

### 1) HIGH - Orphan sweep can kill unrelated processes via PID reuse

Decision: **Blocking**

Evidence:
- `backend/modules/_runtime/supervisor.py:277-299` reads PID values from `workers.json` and sends `SIGTERM`/`taskkill` directly.
- `backend/modules/_runtime/supervisor.py:280-281` acknowledges PID reuse risk, but no identity verification is performed before kill.
- PID file stores only `{pid, uds, port}` (`backend/modules/_runtime/supervisor.py:264-273`), no command-line/boot marker to prove ownership.

Why this is a real gap:
- After host restart, a stale PID can belong to an unrelated process under the same user. Current sweep may terminate that process.

Required fix:
- Persist worker identity metadata (at minimum: spawn timestamp + expected command line/module id; ideally Linux boot id/starttime).
- Before killing, verify process identity matches the recorded worker process.
- If identity cannot be proven, skip kill and log.

---

### 2) HIGH - `declared_env` path can leak host secrets with non-matching names

Decision: **Blocking**

Evidence:
- Declared env values are forwarded from host env when present: `agd_module_worker/sandbox.py:75-79`.
- Blocking is heuristic-only (`is_secret_like`), based on limited substrings/suffixes: `agd_module_worker/sandbox.py:43-55`.
- Declared env list is taken directly from manifest capabilities and passed to worker spawn: `backend/modules/__init__.py:169-172`, `backend/modules/_runtime/supervisor.py:91-104`.

Why this is a real gap:
- Sensitive names not matching heuristics (for example `DATABASE_URL`, `GITHUB_PAT`, provider-specific names) can be forwarded into worker env if declared.
- This contradicts the claimed posture that host secrets cannot leak via env in subprocess mode.

Required fix:
- Replace heuristic gate with an explicit host-side allowlist of forwardable declared env names (or prefix-based public env contract).
- Keep heuristic checks as defense in depth, not the primary control.
- Add tests proving non-heuristic secret names are blocked.

---

### 3) HIGH - Proxy forwards worker `Set-Cookie` headers to the browser

Decision: **Blocking**

Evidence:
- Response header strip list excludes hop-by-hop, `content-length`, `content-encoding`, but not `set-cookie`: `backend/modules/_runtime/proxy.py:31`, `:39-40`.
- All remaining response headers are forwarded to client unchanged: `backend/modules/_runtime/proxy.py:62-67`.

Why this is a real gap:
- An untrusted community module can set/clear cookies on the host origin.
- This enables session/csrf cookie poisoning or forced logout behavior, and violates boundary hygiene between host auth surface and worker responses.

Required fix:
- Strip `Set-Cookie` (and consider stripping other auth-sensitive response headers) from worker responses by default.
- Add regression tests asserting cookie headers from workers never reach clients.

---

### 4) MEDIUM - Request body is fully buffered; spec requires streaming request/response path

Decision: **Blocking for spec conformance**

Evidence:
- Proxy reads full request body into memory: `backend/modules/_runtime/proxy.py:54`.
- Spec requires streaming both directions and no full buffering: `docs/specs/2026-06-27-out-of-process-backend-isolation.md:221-223`.

Why this is a real gap:
- Large uploads are buffered in host memory before forwarding.
- This weakens the DoS/performance posture and diverges from the stated phase-2 contract.

Required fix:
- Stream request body to worker (`request.stream()` -> async byte stream) instead of `await request.body()`.
- Add tests that assert request streaming behavior and memory-safe forwarding.

---

### 5) MEDIUM - Crash-restart watchdog/backoff is still not implemented, despite spec claims

Decision: **Blocking for spec honesty / lifecycle completeness**

Evidence:
- Dead worker immediately returns 502; no restart path: `backend/modules/_runtime/proxy.py:45-47`.
- Supervisor has no crash watcher/backoff/circuit-breaker logic; `restarts` exists but is unused: `backend/modules/_runtime/supervisor.py:73`, `:238-258`.
- Spec states crash restart with capped backoff + circuit breaker: `docs/specs/2026-06-27-out-of-process-backend-isolation.md:398-399`, and lists crash-loop DoS as mitigated: `:551`.

Why this is a real gap:
- Current behavior is fail-dead-until-manual-action, not supervised recovery.
- Spec overstates delivered mitigation.

Required fix:
- Implement worker crash monitoring with bounded exponential backoff and breaker state.
- Or explicitly downgrade spec language from "mitigated" to "not yet implemented" for current phase.

---

### 6) MEDIUM - Default-off mode is not byte-for-byte unchanged due unconditional shutdown side effect

Decision: **Blocking for strict default-off guarantee as written in prompt**

Evidence:
- App shutdown unconditionally imports runtime supervisor and calls `stop_all()`: `backend/main.py:105-106`.
- `stop_all()` always writes pidfile via `_save_pidfile()`: `backend/modules/_runtime/supervisor.py:251-258`, `:264-273`.
- Isolation default is `in_process`: `backend/modules/__init__.py:42-49`.

Why this is a real gap:
- Even with default `in_process`, shutdown path can create `data/run/workers.json` and runtime side effects.
- This is not byte-for-byte unchanged behavior.

Required fix:
- Gate lifespan shutdown `stop_all()` behind `AGD_MODULE_ISOLATION == "subprocess"`, or make `stop_all()`/`_save_pidfile()` no-op when subsystem was never activated.
- Add a test asserting no runtime files/side effects in default mode.

---

### 7) MEDIUM - Test coverage misses key security/lifecycle assertions

Decision: **Non-blocking alone, but contributes to blocking risk above**

Evidence:
- Default-mode test only checks env default string, not side effects (`no spawn/proxy/orphan sweep/pidfile`): `tests/test_module_runtime.py:102-106`.
- Header hygiene test checks request-side cookie/auth stripping, but no response-side `Set-Cookie` stripping assertion: `tests/test_module_runtime.py:59-75`.
- No test for orphan PID identity safety (`sweep_orphans`), despite kill behavior in supervisor.
- No automated UDS-permission test (`chmod 600`) or Linux-specific sweep semantics.

Required fix:
- Add tests for: response cookie stripping, default-off no side effects, orphan PID identity checks, and UDS permission behavior on Linux CI.

---

### 8) LOW - Proxy secret compare in worker is not constant-time

Decision: **Non-blocking**

Evidence:
- Secret check uses plain string inequality: `agd_module_worker/main.py:58`.

Why this is a gap:
- On local transports this is low risk, but constant-time compare is standard for secret equality checks.

Required fix:
- Use `hmac.compare_digest()` for proxy-secret comparison.

---

### 9) LOW - Spec status text is stale relative to local branch state

Decision: **Non-blocking doc issue**

Evidence:
- Spec header says isolation runtime "not yet built": `docs/specs/2026-06-27-out-of-process-backend-isolation.md:3`.
- Local branch contains phase-1/2 runtime implementation (`26a2b3c`, `a0b8434`) and corresponding tests (`tests/test_module_worker.py`, `tests/test_module_runtime.py`).

Required fix:
- Update spec status block to reflect implemented phases vs pending phases, so reviewers are not evaluating against stale state.

## Cross-check Notes

- Pass-1 critical prerequisite (module-id traversal hardening) remains closed in current code:
  - Validator + reserved names: `backend/module_registry.py:56-76`, `:157-169`
  - Installer containment gate on install/uninstall: `backend/modules/modules/installer.py:69-85`, `:393-399`, `:434-438`
  - Security tests include traversal and Windows-reserved coverage: `tests/test_module_security.py:47-63`, `:76-96`
- Reference consumer coupling claims in spec match current `youtube-research` code:
  - `backend` imports for auth/websocket/db/assistant/notes remain present (for now), consistent with "needs re-port":
    - `.../youtube-research/router.py:29,43,57-59`
    - `.../youtube-research/store.py:18,41,75,89,97,109,121,133`
    - `.../youtube-research/llm.py:41,44,48-50,52`
    - `.../youtube-research/artifacts.py:29-30,39,201,218`
  - Frontend polling (no module WS dependency) is present: `.../youtube-research/static/module.js:8,20,234-241`

## Final Disposition

**Not approved**.

Blocking set:
1. PID-reuse unsafe orphan sweep kill path.
2. `declared_env` secret leakage via heuristic-only filtering.
3. Worker response `Set-Cookie` leakage through reverse proxy.
4. Request buffering + missing watchdog/backoff where spec currently claims those guarantees.
5. Default-off mode not behaviorally unchanged due unconditional runtime shutdown side effects.
