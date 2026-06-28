# Adversarial Review: Host Capability Bridge (Phases 3-5)

Status: COMPLETE.
Date: 2026-06-27
Reviewer: automated adversarial pass (GitHub Copilot)
Scope: commits `7e8ced9` (phase 3), `73f5766` (phase 4), `9fc0585` (phase 5) on `origin/main`
Spec: `docs/specs/2026-06-27-out-of-process-backend-isolation.md` Sections 5.5, 5.5a, 5.5b, 6
Threat model: the module author is hostile; the operator already clicked "install."

## Summary

The bridge is well-architected and the core security invariants hold: loopback-only bind, per-spawn tokens, cookie rejection, tool-free completion, host-resolved keys, default-off. The findings below are real but fixable; none require a redesign. **Disposition: not approved** pending the two HIGH findings (symlink escape, uninstall leaves worker live). The MEDIUM/LOW set can land in a follow-up.

---

## Findings

### HIGH-1: Symlink escape defeats notes path scoping

**Evidence:** `backend/modules/_runtime/bridge.py:106-108` (`_resolve_dir`), `backend/modules/notes/storage.py:100-104` (`resolve`), `bridge.py:117-122` (`_note_rel_in_scope`).

Both validators call `Path.resolve()` (which follows symlinks) and then check `relative_to(VAULT_DIR.resolve())`. This correctly blocks escapes **outside the vault**. But the scope check `_under(vp.rel, prefixes)` runs against the **unresolved** rel string, while the actual I/O uses the **resolved** path. A symlink inside the vault pointing to another vault subtree defeats the per-module scope:

1. A symlink `research/evil -> ../user` exists in the vault (placed by a prior in-process module run, an Obsidian sync, or a sibling module with broader access).
2. `notes.write` with `path="research/evil/secret.md"`:
   - `storage.resolve("research/evil/secret.md")` resolves to `VAULT_DIR/user/secret.md`.
   - `relative_to(VAULT_DIR.resolve())` succeeds (under vault).
   - `vp.rel` = `"research/evil/secret.md"` (the unresolved string).
   - `_under("research/evil/secret.md", ["research"])` returns True.
   - `storage.write` writes to `VAULT_DIR/user/secret.md` — **outside the declared write scope.**
3. `notes.read` through the same symlink reads `user/secret.md` — **outside the declared read scope.**
4. `notes.move(src="research/evil/secret.md", dst="research/copy.md")` reads `user/secret.md`, writes it to `research/copy.md`, then `storage.archive(src)` calls `shutil.move(vp.abs, archive_abs)` — **moving (deleting) the file from `user/` into the archive.** This is destructive: the file vanishes from its original location.

The module cannot create symlinks through the bridge (`make_folder` calls `mkdir`, not `symlink`), so this requires a pre-existing symlink. But the escape is real, enables read + write + destructive move across the vault, and is silent.

**Required fix:** after `resolve()`, check the resolved path is under the declared scope, not just under the vault. Concretely: resolve the path, then walk each parent from the resolved path back to the vault root and verify the first vault-relative segment is under a declared prefix. Alternatively (simpler, stricter): reject any path component that `os.path.islink` on the unresolved `VAULT_DIR / rel` intermediate segments, refusing to operate through symlinks at all. Add a regression test that creates a symlink inside the vault and asserts every notes operation through it is rejected.

---

### HIGH-2: Uninstall does not stop the worker or revoke the bridge token

**Evidence:** `backend/modules/modules/installer.py:432-449` (`uninstall`), `backend/modules/modules/router.py:145-151` (`uninstall_module`), `backend/modules/_runtime/supervisor.py:217-243` (`stop`), `backend/modules/_runtime/bridge.py:73-76` (`revoke_module` — defined, never called).

`uninstall()` does `shutil.rmtree(target)` and updates the lock file. It does **not**:
- Call `supervisor.get(module_id).stop()` — the worker subprocess keeps running with the module's code in memory.
- Call `bridge.revoke_module(module_id)` — the bridge token remains valid in `_grants`.
- Remove the proxy route registered by `register_proxy_route(app, module_id)` — FastAPI does not support route removal, so `/api/{module_id}/*` still forwards to the still-running worker.

Result: after the operator clicks "uninstall," the module is still fully operational (routes proxied, bridge token live) until the host process restarts. The operator's mental model ("I removed it") is false. A hostile module that detects uninstall (e.g., a background task polling its own route) can continue operating, including calling `assistant.complete` (spending the operator's LLM budget) and reading/writing the vault through its still-valid bridge token.

`revoke_module(module_id)` exists in `bridge.py:73` specifically for this case but is never called anywhere in the codebase.

**Required fix:** in `installer.uninstall()` (or in the router handler, before calling `installer.uninstall`), call `supervisor.get(module_id)` and if a worker is running, `worker.stop()` (which revokes the token). Also call `bridge.revoke_module(module_id)` as a belt-and-suspenders sweep. Document that the proxy route cannot be removed at runtime (FastAPI limitation) and that a restart fully clears it; the token revocation is the real control. Add a test: start a worker, uninstall, assert the bridge token is revoked (401 on next bridge call) and the worker process is dead.

---

### MEDIUM-1: Worker spawned before the bridge listener starts

**Evidence:** `backend/main.py:401` (`modules = register_modules(app)` — runs at import time), `backend/main.py:84-89` (lifespan calls `start_bridge()` — runs when the app starts serving).

`register_modules` runs at module import time and calls `supervisor.start_worker()` for each isolated community module. `start_bridge()` runs inside the lifespan, which executes **after** import time. So workers are spawned before the bridge listener is started.

A worker that calls the bridge at import time (module-level code, not a route handler) gets `ConnectionRefusedError`. The failure is safe (the host doesn't crash; the worker's import-time bridge call fails and may surface as a module load failure), but the ordering is incorrect: the spec says "start the loopback capability bridge before serving, so isolated-module workers can reach it."

In practice, most modules call the bridge from route handlers (invoked after the host is serving, when the lifespan has started the bridge), so this rarely triggers. But a module with import-time bridge calls would fail confusingly.

**Required fix:** move `start_bridge()` to before `register_modules(app)`, or make `register_modules` async and call it from the lifespan after `start_bridge`. Alternatively, have `start_worker` wait for the bridge to be listening before marking the worker healthy (add a bridge health check to `_wait_healthy`).

---

### MEDIUM-2: `_ensure_port()` TOCTOU race

**Evidence:** `backend/modules/_runtime/bridge.py:283-292`.

`_ensure_port()` opens a socket, binds to `127.0.0.1:0`, reads the assigned port, then **closes the socket**. The port is later re-bound by uvicorn in `start_bridge()`. Between the close and the re-bind, another process could grab the port. If that happens, uvicorn fails to bind and the bridge never starts (logged as an exception in the lifespan). Workers would then get `ConnectionRefused` on every bridge call.

Low impact (safe failure, no crash, no secret leak), but the port reservation is not durable.

**Required fix:** pass the bound socket itself to uvicorn (uvicorn.Config accepts a `fd` or the server can take a pre-bound socket), or bind once and keep the socket open until uvicorn takes it. Alternatively, retry on bind failure with a new port.

---

### MEDIUM-3: Scanner gap — literal `__import__("backend")` evades host-import HIGH

**Evidence:** `backend/modules/modules/scanner.py:178-182`.

The scanner flags `__import__` / `importlib.import_module` as CRITICAL dynamic-import **only when the module name argument is a non-literal** (`_const_str(first) is None`). A literal `__import__("backend")` passes the non-literal check (it IS a constant string) and is not flagged as host-import because the host-import check only runs in `visit_Import` / `visit_ImportFrom`, not in `visit_Call` for `__import__`.

A hostile module can write `__import__("backend").modules.notes.storage` and the scanner reports nothing (no import statement, no dynamic-import finding, no host-import finding). Under isolation, the worker's import blocker would still catch this at runtime, but the scanner — the operator's consent gate — misses it entirely.

**Required fix:** in `_check_call`, when `name in ("__import__", "importlib.import_module")` and the first arg is a literal string, resolve the root module and apply the same host-import check as `visit_Import`. Add a test: `code = '__import__("backend").config.decrypt_value("$X")'` should produce a HIGH host-import finding.

---

### LOW-1: No write size limit (disk-fill DoS)

**Evidence:** `backend/modules/_runtime/bridge.py:147` (`_WritePayload.content: str`), `backend/modules/notes/storage.py:131` (`vp.abs.write_text(content)`).

`notes.write` accepts arbitrarily large `content` with no per-call or per-module size cap. A hostile module can fill the disk by writing large notes repeatedly. The spec matrix acknowledges disk exhaustion is not enforced in v1, but a simple per-call size limit (e.g., reject content > 1 MB) would cheaply raise the bar.

**Required fix:** add a `max_length` validator on `_WritePayload.content` (pydantic `Field(max_length=...)`) or check `len(content)` in the handler. Document the limit in the spec.

---

### LOW-2: `list_folders` / `list_files` do not filter symlinks

**Evidence:** `backend/modules/_runtime/bridge.py:241, 251`.

Both list endpoints filter `not c.name.startswith(".")` but do not check `c.is_symlink()`. A symlink to a directory inside the listed directory appears in the output (because `c.is_dir()` follows symlinks). This leaks the symlink's name but not its target. Combined with HIGH-1, it lets a module discover symlink names to target.

**Required fix:** add `and not c.is_symlink()` to the filter, or resolve and re-check containment for each entry.

---

### LOW-3: Provider error body forwarded to the worker

**Evidence:** `backend/modules/assistant/completion.py:95` (`raise CompletionError(f"Provider HTTP {status}: {body[:300]}")`).

The provider's error response body (first 300 chars) is included in the `CompletionError` message, which the bridge returns to the worker as a 502 detail. Providers don't typically echo the API key in error bodies, but this is an information-forwarding path from the provider to the sandbox. If a misconfigured or malicious provider endpoint echoed the `Authorization` header in an error response, the key would leak to the worker.

**Required fix:** return a generic error message to the worker ("provider returned HTTP {status}") and log the full body host-side only. Or scrub any `Bearer ` token from the body before forwarding.

---

### LOW-4: `revoke_module` is dead code

**Evidence:** `backend/modules/_runtime/bridge.py:73-76`.

`revoke_module(module_id)` is defined but never called anywhere in the codebase. It exists for the uninstall path (HIGH-2) but was never wired up.

**Required fix:** call it from `installer.uninstall()` (see HIGH-2 fix).

---

## What's well done

- **Token entropy.** `secrets.token_urlsafe(32)` = 256 bits. No brute-force surface. (`bridge.py:60`)
- **Token revocation on replace and shutdown.** `start_worker` calls `existing.stop()` which calls `bridge.revoke(self.bridge_token)`. `stop_all` calls `worker.stop()` for each. (`supervisor.py:223, 283`)
- **Empty capabilities → all-deny.** `mint()` with `Capabilities()` or `None` produces `write_paths=[]`, `read_paths=[]`, `host_assistant=False`. Every bridge endpoint rejects. (`bridge.py:52-68`)
- **Loopback-only, never on public app.** `bridge_app` is a separate FastAPI served by its own `uvicorn.Server` on `127.0.0.1`. No `app.mount` or `app.include_router` for `bridge_app` on the main app. A request to `/api/_host/*` on the public app 404s. (`bridge.py:296`, grep confirms no mount)
- **Cookie rejection.** `_require_grant` rejects any request with a `cookie` header → 403. Blocks browser/CSRF. (`bridge.py:132-134`)
- **Bearer parsing.** Case-insensitive prefix, strips whitespace, empty token → 401. Robust against edge cases. (`bridge.py:135-138`)
- **Tool-free by construction.** `completion._openai_compat` / `_anthropic` / `_ollama` build payloads with no `tools` key. The executor never calls `_dispatch_chat` or fetches MCP tools. Comment at `completion.py:152`: "no tools key, ever." (`completion.py:140-156, 159-174, 177-196`)
- **No SSRF.** The caller supplies only `system`, `user`, `model`, `max_tokens`. The provider base URL comes from host config (`get_assistant_config`, `_custom_base_url`). For OpenAI/Anthropic/OpenRouter, URLs are hardcoded. The caller cannot influence the URL. (`completion.py:99-130`)
- **Host key never crosses to worker.** `_resolved_config()` resolves the key host-side; `_dispatch` uses it in the `Authorization` header; only text returns. (`completion.py:63-73, 99-130`)
- **`host.assistant` gate before provider call.** `assistant_complete` checks `grant.host_assistant` before importing `completion` or calling any provider. (`bridge.py:272-273`)
- **`max_tokens` clamped.** `max(1, min(int(payload.max_tokens or 8000), HARD_MAX_TOKENS))` where `HARD_MAX_TOKENS = 16000`. (`bridge.py:276`)
- **Default-off.** With `AGD_MODULE_ISOLATION` unset, `_isolation_mode()` returns `"in_process"`, no workers spawn, no bridge starts, no tokens mint. Behavior is unchanged. (`__init__.py:38-42`, `main.py:84-89`)
- **Segment-aware scope.** `_under("research-evil/x", ["research"])` returns False. Correct. (`bridge.py:86-92`)
- **Env allowlist.** `build_worker_env` starts empty, copies only allowlisted names, applies `injected` last. No host secret rides along. `is_secret_like` is a secondary guard. (`sandbox.py:60-80`)
- **Per-spawn token.** `bridge.mint()` called in `ModuleWorker.__init__`, per `start_worker`. Each spawn gets a new token. (`supervisor.py:62-63`)
- **Scanner host-import HIGH.** Correct: matches `backend` root only, not substrings. Catches `import backend`, `from backend.x import y`. Not over/under-broad. (`scanner.py:170-177`)
- **Scanner bridge cross-check.** Honest literal-string heuristic for `/api/_host/assistant/complete` and `/api/_host/notes/`. Declared → INFO; undeclared → HIGH. Limits (dynamic URLs, SDK wrapping) are documented. (`scanner.py:230-247`)

---

## Test coverage assessment

**`test_host_bridge.py`** — meaningful, not vacuous. Covers: token required, cookie rejection, write/read scoping, segment awareness, traversal rejection, read-includes-write, read-only path, folder ops, assistant capability gate, max_tokens clamp, end-to-end worker→bridge. Good breadth.

**`test_assistant_completion.py`** — meaningful. Covers: tool-free payload assertion, max_tokens clamp, key resolution from convention secret, retry on too-large, missing key error. The `assert "tools" not in sent` test is the load-bearing guarantee, well-placed.

**`test_module_security.py`** (scanner subset) — meaningful. Covers: host-import HIGH, bridge assistant declared vs undeclared, cross-module MEDIUM, parse errors. Good.

**Coverage gaps (all should be added):**
1. **Symlink escape** — no test creates a symlink in the vault and asserts notes operations through it are rejected. (HIGH-1)
2. **Token revocation** — no test revokes a token and asserts subsequent use returns 401. (`revoke` is tested indirectly via `stop()` in the end-to-end test, but not explicitly.)
3. **Cross-module token** — no test asserts module A's token cannot be used for module B's grant.
4. **Empty capabilities** — no test asserts a module with `Capabilities()` (all-false) gets 403 on every bridge endpoint.
5. **Uninstall revocation** — no test asserts that after uninstall, the worker is stopped and the bridge token is revoked. (HIGH-2)
6. **Write size limit** — no test for oversized content rejection. (LOW-1, because no limit exists)
7. **SSRF attempt on assistant.complete** — no test asserts the caller cannot set the provider URL. (Low value since there's no surface, but worth a negative test.)

---

## Disposition

**Not approved.** Two HIGH findings block:

1. **HIGH-1 (symlink escape)** — the core path-scoping contract is defeatable by a pre-existing symlink. The fix is localized (resolve-then-recheck against declared scope, or reject symlinks) and needs a regression test.
2. **HIGH-2 (uninstall leaves worker live)** — the operator's uninstall action does not stop the worker or revoke the bridge token. The fix is a few lines in `installer.uninstall()` plus a test.

The MEDIUM findings (listener ordering, port race, scanner `__import__` gap) should be addressed in the same pass but are not blocking. The LOW findings can land in a follow-up.

After HIGH-1 and HIGH-2 are fixed with tests, the bridge is approved for the next phase (re-porting a real module onto it).

---

## Resolution (2026-06-28)

All findings addressed in one pass. Full suite 221 pass (was 213); ruff clean on
every changed file.

| Finding | Fix |
|---|---|
| HIGH-1 symlink escape | `bridge._resolved_under()` scope-checks the RESOLVED on-disk location, not just the requested string; `_note_rel_in_scope`/`_dir_rel_in_scope` now require BOTH to be in scope. A symlink inside the vault that redirects out of the declared prefixes is refused (write/read/move/make-folder). Test `test_symlink_escape_blocked`. |
| HIGH-2 uninstall leaves worker live | `installer._teardown_isolated()` runs before `rmtree`: `supervisor.stop_worker()` (stops the subprocess, revokes its bridge token) + `bridge.revoke_module()`. The proxy route cannot be removed at runtime (FastAPI limit, documented); token revocation is the real control. Test `test_uninstall_stops_worker_and_revokes_token`. Wires `revoke_module` (closes LOW-4). |
| MEDIUM-1 worker before bridge | Worker spawn deferred from import-time registration to the lifespan, AFTER `start_bridge()`, via `start_isolated_workers()`. Spawns run in a thread executor so a worker that calls the bridge during startup is served by the free event loop instead of deadlocking the blocking health wait. |
| MEDIUM-2 port TOCTOU | `start_bridge()` adopts the port uvicorn actually bound (`_publish_bound_port`) and retries on a fresh port if the bind fails. Combined with MEDIUM-1, workers only read the port after the listener is up, so the reservation gap is closed. |
| MEDIUM-3 literal `__import__("backend")` | Scanner `_check_call` routes a literal dynamic import through `_note_import`, so it gets the same host-import HIGH (and network/subprocess) checks as a plain import statement. Test `test_scanner_literal_dynamic_import_backend_is_high`. |
| LOW-1 no write size limit | `MAX_NOTE_BYTES = 1_000_000` cap on `notes.write` (413 over limit). Test `test_write_size_limit`. |
| LOW-2 list does not filter symlinks | `list-folders`/`list-files` skip `is_symlink()` entries. Test `test_list_excludes_symlinks`. |
| LOW-3 provider error body forwarded | `completion.complete` raises a generic `Provider returned HTTP {status}`; the full body is logged host-side only. Test `test_provider_error_body_not_forwarded`. |
| LOW-4 `revoke_module` dead code | Wired by the HIGH-2 fix. |

Coverage gaps from the review's test assessment also added: token revocation
(`test_revoked_token_rejected`) and empty-capabilities all-deny
(`test_empty_caps_deny_every_endpoint`).

**Disposition: closed.** Bridge approved for phase 6 (re-port youtube-research).
