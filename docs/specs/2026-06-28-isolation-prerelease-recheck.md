# Adversarial Pre-Release Re-Check: AgeniusDesk 0.3 (focused, post-fix)

Date: 2026-06-28
Repos reviewed:
- `ageniusdesk-ce` @ `64b8673`
- `ageniusdesk-community-modules` @ `b8a94b5`

This is the focused re-check requested after the first pre-release review
(`2026-06-27-isolation-prerelease-review.md`, NOT APPROVED) was resolved in
`d1fc03a`. Two feature commits landed after that resolution and are the new
attack surface here:
- `37bc828` Fleet Health cross-instance Errors tab
- `64b8673` shared error-item component (one renderer for Overview / Errors /
  Fleet Health)

Scope: (a) confirm the four prior findings remain fixed at `64b8673`, and (b)
adversarially review the new code (fleet health, error-handler auto-install,
shared error item, the errors router).

Test baseline before fixes: `uv run pytest -q` -> `237 passed`.

## Part A: the four prior findings still hold at 64b8673

| Prior finding | Status at 64b8673 | Evidence |
|---|---|---|
| HIGH `notes.search` symlink scope bypass | Still fixed | `bridge._path_in_scope()` (resolve + `_under` + `_resolved_under`) filters every search hit (`bridge.py:131-143`, `:250-256`). |
| HIGH modules write endpoints lack a role floor | Still fixed | `require_role("operator")` on `/isolation` POST, `/discover`, `/inspect`, `/install`, `DELETE /{id}` (`modules/router.py:93,143,154,169,195`). |
| MEDIUM mode-gated container/volume cleanup | Still fixed, hardened | `stop_container_worker` now tears down the labeled container + revokes the grant even when the worker is untracked (`containers.py:394-420`); `start_isolated_workers` sweeps container orphans before branching on mode (`__init__.py:251-264`); uninstall tears down regardless of mode (`modules/router.py:201-210`). |
| LOW spec matrix container import overclaim | Still fixed | matrix wording corrected in the spec (`...isolation.md:107-112`). |

No regressions in the prior fixes.

## Part B: new findings in the post-resolution feature code

### HIGH: stored XSS via the shared error-item renderer (attribute/JS-context escaping)

**Evidence**
- `frontend/js/components/error-item.js` (new in `64b8673`) was extracted from
  `views/errors.js` but shipped with a WEAKER `jsStr()`:
  - extracted component: `jsStr` escaped only `\` and `'`.
  - source `views/errors.js:386-391`: `jsStr` also escapes `<`, `&`, and `"`.
- The component writes ids into HTML-attribute / inline-JS contexts with that
  weak escaper and with `esc()` (which does not escape `"`):
  - `onclick="window.__nav('workflows',{selectId:'${jsStr(e.workflow_id)}'})"`
  - `onclick="window.__deleteExecution('${jsStr(e.execution_id)}', this)"`
  - `href="${n8nBase}/workflow/${esc(e.workflow_id)}/executions/${esc(e.execution_id)}"`
- `e.workflow_id` / `e.execution_id` are attacker-controlled: `ErrorPayload`
  (`errors/router.py:21-27`) is an unvalidated string model, and
  `POST /api/errors/webhook` is allow-listed past the login gate
  (`main.py:174-177`, `_legacy_webhook_ok`), open by default (no
  `AGD_WEBHOOK_TOKEN`). `collector.store_error` persists the values verbatim.
- The same component renders on Overview (`dashboard.js`), the Errors view
  (`errors.js` flat mode), and Fleet Health > Errors (`fleet-health.js`), so the
  payload fires on three surfaces.
- No CSP by default (`security_headers` sets CSP only when `AGD_CSP` is set,
  `main.py:285-286`), so inline event handlers execute.

**Concrete repro (verified against the committed pre-fix component)**
`workflow_id = '"><img src=x onerror=alert(1)>'` posted to the open webhook
renders a literal `<img ... onerror=...>` in the operator's authenticated
dashboard (zero-click) the next time they view any error surface. A DOM-shim
harness run against `HEAD:error-item.js` confirmed the literal `<img` breakout;
the same harness against the source `views/errors.js` renderer did not break out.

**Required fix**
- Bring the shared component's escapers up to the source's strength: `jsStr`
  escapes the JS string delimiters AND `<`/`&`/`"`; ids in the `href` use
  `encodeURIComponent` (URL-correct and attribute-safe).
- Add a behavioral regression test that renders a hostile error and asserts no
  attribute/JS breakout.

### MEDIUM: error-store endpoints that mutate n8n lack the operator role floor

**Evidence**
- `errors/router.py` mounts no router-level role gate. n8n-mutating / destructive
  endpoints were reachable by any logged-in identity (viewer):
  - `POST /clear-group` (optional n8n execution purge),
  - `DELETE /{execution_id}` (`purge_n8n=True` by default),
  - `DELETE ""` (optional n8n purge),
  - `POST /sync` (pulls n8n executions, writes the local store),
  - `POST /install-handler` (imports + activates a workflow into n8n) used only
    `require_trusted_request` (any identity).
- The equivalent n8n writes on `/api/n8n/*` require `require_role("operator")`
  (`n8n_proxy/router.py:29`). This is the same class as the prior modules HIGH:
  a low-privileged viewer performing operator-class mutations.

**Required fix**
- Gate the n8n-mutating / destructive error endpoints at `operator`; keep reads
  (`GET ""`, `/grouped`, `/handler-status`, `/handler-template`) and the machine
  webhook open. RBAC regression tests parallel to `test_router_rbac.py`.

### MEDIUM: auto-installed error handler silently fails when a webhook token is set

**Evidence**
- `backend/n8n_workflows/global-error-handler.json` POSTs to
  `/api/errors/webhook` with no token header.
- That route is token-gated by `_legacy_webhook_ok` whenever `AGD_WEBHOOK_TOKEN`
  is set (`main.py:199-204`). With a token configured, every error POST returns
  401 and is swallowed by the node's `continueOnFail: true`, while the connect
  toast reports the handler installed successfully. The headline "errors flow on
  connect" feature silently breaks under a reasonable hardening config.

**Required fix**
- Have the handler template send `x-agd-webhook-token` from
  `$env.AGD_WEBHOOK_TOKEN` (no-op when unset; correct when the operator sets the
  token on both sides) and document the cross-config in the handler note.

### Reviewed and acceptable (no action)
- **SSRF in fleet health / auto-install.** `_instance_health`, `fleet_health`,
  `install_handler_into`, `handler_dashboard_url` build URLs from operator-set,
  decrypted instance config and operator-set settings, not from request input.
  `fleet_health` fan-out is exception-safe (`_instance_health` never raises);
  `exec_limit` clamped 1..250.
- **error_message / workflow_name rendering.** Rendered through `esc()` (text)
  and `attr()` (data-attributes); both safe even though attacker-controlled.
- **Open webhook itself.** Documented trusted-LAN / machine-ingest posture,
  consistent with `messages/webhook` and OTel ingest; the bug was the rendering,
  not the open ingest.

## Final disposition (pre-fix)

**NOT APPROVED** as of `64b8673`. Blocking finding: the stored XSS (HIGH).

---

## Resolution (2026-06-28)

All three findings fixed; full suite green with new regression tests
(`uv run pytest -q` -> `241 passed`). Ruff clean on changed files (the 2 E501s in
`n8n_proxy/router.py` are pre-existing in untouched lines).

| Finding | Fix |
|---|---|
| HIGH stored XSS in the shared error item | `error-item.js` `jsStr` now escapes `\ ' \n \r < & "` (matching `views/errors.js`); `n8nExecUrl` ids use `encodeURIComponent`; the instance-badge color uses `attr()`. The same latent `esc()`-in-href / `esc()`-in-style spots in `views/errors.js` `renderGroupItem` were hardened for parity. Regression test `tests/test_error_item_xss.py` runs `tests/js/error_item_xss_check.mjs` (DOM shim, hostile payload) and asserts no `<img>`/`<script>` breakout. Verified the harness fails against the pre-fix component. |
| MEDIUM error endpoints lack a role floor | `require_role("operator")` added to `POST /clear-group`, `DELETE /{execution_id}`, `DELETE ""`, `POST /sync`, and `POST /install-handler`; reads + the webhook stay open. Tests `test_viewer_blocked_errors_mutations_reads_open` + `test_operator_allowed_errors_clear`. |
| MEDIUM handler vs. webhook token | `global-error-handler.json` now sends `x-agd-webhook-token` from `$env.AGD_WEBHOOK_TOKEN` (HTTP node `sendHeaders` + `headerParameters`); note updated. Test `test_handler_template_carries_webhook_token_header`. |

**Disposition: blockers cleared.** Ready for the 0.3 tag decision.
