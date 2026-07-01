# Spec: Consented-secret tier (held credentials under isolation)

Status: SPEC / proposed. Not committed to a release.

Date: 2026-06-30

Related: `2026-06-27-out-of-process-backend-isolation.md` (the isolation tiers,
the allowlist env scrub this extends — §5.3), `2026-06-28-http-request-bridge.md`
(the host-mediated path this is the last-resort complement to),
`2026-06-28-agent-fleet-langgraph-spec.md` ("The credential fork" → Option B, the
first customer), `2026-06-30-support-ticketing-module.md` (IMAP poll / SMTP reply,
the second customer), `agd_module_worker/sandbox.py` (`build_worker_env`),
`backend/modules/_runtime/supervisor.py` (`_build_env`),
`backend/module_registry.py` (`Capabilities`).

## 1. Problem

Under the subprocess and container tiers the worker env is **allowlist-scrubbed**
(`build_worker_env`, `sandbox.py`): the worker gets `PATH`/locale/runtime vars plus
the injected `AGD_*` handshake, and **nothing** that looks like a credential. That
is correct for the common module — host actions go through the bridge and the key
never enters the worker.

But some modules need a credential **resident in the worker process**, and the
bridge cannot help, for one of two reasons:

1. **The credential feeds a native-wire protocol the host has no bridge for.** The
   `http.request` bridge brokers *HTTP*. IMAP, SMTP, Redis RESP, and the Postgres/
   MySQL wire are not HTTP, so the host cannot make the call on the module's behalf.
   A module that must `imaplib.login(user, password)` needs the password in-process.
2. **The credential feeds a library that must run in-worker for correctness.** Per
   the Agent Fleet investigation, LangSmith tracing + provider-native
   `llm.bind_tools()` only work when the keyed ChatModel runs in the same process as
   the graph; routing the LLM through `assistant.complete` loses the tool-loop and
   the traces.

Today such a module has exactly two options: run **`in_process`** (full host
access — defeats isolation) or **not ship**. This tier is the third path: inject
**only** the explicitly-declared, operator-consented secrets into an otherwise-
scrubbed worker. It is the Agent Fleet spec's deferred "Option B", generalized to a
shared host capability with more than one customer.

## 2. Where it sits relative to the other two host investments

This is **last resort by design.** Pick per-credential, cheapest containment first:

| Need | Mechanism | Credential location |
|---|---|---|
| Host can make the call (REST) | **`http.request` bridge** | Fully host-side; resolved per-call, never in the worker |
| Host can run the LLM completion (one-shot, tool-free) | **`assistant.complete` bridge** | Fully host-side; key never in the worker |
| Worker MUST hold the credential (native-wire / in-worker library) | **Consented-secret tier (this spec)** | **Resident in the worker for its process lifetime** |

If the bridge can do the job, use the bridge — it keeps the credential out of the
sandbox entirely. The consented-secret tier accepts a real credential into the
worker, so it carries the strongest consent and the loudest scanner finding.

## 3. Design principles

1. **Opt-in per secret, declared in the manifest, never inferred.** A module lists
   exactly which secrets it must hold; the host injects those and only those.
2. **Allowlist stays an allowlist.** Consented secrets are resolved host-side and
   added to the authoritative **`injected`** map — they do **not** widen the
   inherited-env allowlist (`_ENV_ALLOW`/`extra_allow`). A module still cannot name
   an arbitrary host env var and have it forwarded; it names a *secret_ref* the
   operator holds a value for, and only that resolved value is injected, under a
   worker env name the manifest declares.
3. **The consent grant is the source of truth, not the manifest.** The operator
   consents to a specific secret set at install; that set is stored as a structured
   **consent grant** (§5.1 — a new runtime artifact; the existing `module_installs`
   row is best-effort *audit*, not a grant). The supervisor injects from the stored
   grant, re-resolving values fresh at spawn — a manifest swapped on disk after
   install cannot silently widen the secret set, because injection reads the grant's
   `{env, secret_ref}` pairs, not the manifest's.
4. **Container-first.** A held credential belongs in a PID-namespaced container, not
   a same-uid subprocess where a sibling can read `/proc/<pid>/environ`. Default:
   `worker_secrets` requires the container tier; the subprocess tier injects only
   under an explicit operator override with a louder warning (§7).
5. **Loud by default.** Declaring `worker_secrets` is a HIGH scanner finding and a
   distinct, stronger consent block — this is the one place a credential enters the
   sandbox, and the operator must see it as exactly that.

## 4. Capability declaration

Extend `Capabilities` (`backend/module_registry.py`) with a new list, peer to the
existing `host`/`filesystem`/`network` blocks:

```json
"capabilities": {
  "worker_secrets": [
    {
      "env": "SUPPORT_IMAP_PASSWORD",
      "secret_ref": "SUPPORT_IMAP_PASSWORD",
      "reason": "Log in to the support mailbox over IMAP to poll new mail."
    },
    {
      "env": "LANGSMITH_API_KEY",
      "secret_ref": "LANGSMITH_API_KEY",
      "reason": "Emit LangSmith traces from inside the agent runtime."
    }
  ]
}
```

`WorkerSecret` fields:

| Field | Required | Meaning |
|---|---|---|
| `env` | yes | The env-var **name the worker code reads**. Validated `^[A-Z][A-Z0-9_]{0,63}$`; must NOT collide with a reserved handshake name (`AGD_*`), any `_ENV_ALLOW` name (`PATH`, `HOME`, …), or any name in this module's `capabilities.env` (below). |
| `secret_ref` | yes | The **key in the secret store** whose value is injected. A name only — never a value. Validated `^[A-Z][A-Z0-9_]{0,63}$` (store keys are UPPER_SNAKE by convention). Resolved host-side via the existing resolver (env first, then encrypted `secrets.json`), the same path `assistant.complete` uses. |
| `reason` | yes | Human string shown verbatim in the consent modal. An empty `reason` is a **scanner INFO note** in the report (surfaced, not blocking) — a load warning has no other home, and operators deserve to see a secret requested with no stated reason. |

Decoupling `env` from `secret_ref` is deliberate: the module declares "I need a
value at `LANGSMITH_API_KEY`"; the operator maps it to whichever store key holds
their key. The module never learns the store key name, only receives the value
under the name it asked for.

**Relationship to the two existing secret surfaces (decided, not open):**

- **`secrets_required`** (`list[SecretRequirement]`, checked at load by
  `check_secrets` → `missing_secrets` status if absent) means "this value must be
  **available host-side**" — it gates loading and, for a bridge module, is what
  `http.request`/`assistant.complete` resolve **per call**. The value never enters
  the worker.
- **`worker_secrets`** (this spec) means "this value must be **in the worker
  process**." Injected once at spawn, resident for the process lifetime.
- They are **disjoint by purpose** but a module MAY legitimately name the same
  `secret_ref` in both (e.g. a key it uses host-side for one path and in-worker for
  another). No conflict: each surface resolves the same store key independently.

**`env` vs `capabilities.env` (decided for v1).** `capabilities.env` is the existing
informational "env keys the module reads" list the scanner checks; `worker_secrets[].env`
is a name the host **injects**. A name may not appear in both — **parse-time
rejection** — and `worker_secrets` is authoritative (it is injected; `capabilities.env`
is only declared-read). This is a v1 validation rule, not an open question.

## 5. Host-side injection (the hook)

Two call sites, one helper, no change to `build_worker_env`'s allowlist logic.

In `supervisor.WorkerProc._build_env` (subprocess) and
`containers.ContainerWorker._injected_env` (container — today it returns ONLY the
handshake vars, so this is where the resolved pairs are appended):

1. Read the **consented** `worker_secrets` set from the module's stored **consent
   grant** (§5.1), NOT the live manifest — principle 3.
2. For each entry, resolve `secret_ref` host-side via `load_secrets()` (env → store
   precedence, unchanged). A ref with no value fails the spawn closed with a clear
   "missing required secret `{secret_ref}`" error rather than starting a worker that
   will half-work.
3. Add `{ws.env: resolved_value}` to the existing `injected` dict.

`build_worker_env` already applies `injected` **last and unconditionally**
(`out.update(injected)`), so the injected values bypass `is_secret_like` — which is
exactly right: these are the secrets the operator consented to. The inherited-env
allowlist is **untouched**, so no *other* secret can ride along. Net change to
`sandbox.py`: none; the tier is entirely a host-side resolve-and-inject plus the
manifest/grant/scanner/consent machinery around it.

Container tier: `_injected_env` appends the same resolved `{env: value}` pairs to
the container env (the container's bind is unpublished and otherwise scrubbed per
§5.8 of the isolation spec), confined to the container's PID namespace.

## 5.1 The consent grant store (new runtime artifact — the blocker)

Principle 3 needs a grant the supervisor can **read at spawn**. Today there is none:
`installer._record_install` writes a `module_installs` row that is explicitly
**best-effort audit** ("audit must never break install", wrapped in try/except) and
stores the entire `capabilities_json` blob, not a per-secret operator decision. The
supervisor has no read path back to it. So this tier must add the grant.

**Decision: upgrade the install record into a real grant (the reviewer's Option 1).**
Cleaner than "inject from live manifest + a consent flag," and it is the only shape
that satisfies "a manifest swap cannot widen the set" without also pinning and
re-checking a manifest SHA at every spawn.

- **Storage:** a dedicated, structured **consent grant** written **transactionally**
  at install (NOT best-effort — a failed grant write fails the install, unlike the
  audit row). Shape: `module_id → [{env, secret_ref}]`, the exact consented pairs.
  A sidecar table `module_secret_grants` (or a typed column on the install record
  read separately from the audit blob); it is **not** the `capabilities_json` audit
  JSON. Written only after the operator acknowledges the §8 consent block.
- **Read at spawn:** `_build_env`/`_injected_env` load the grant for the module and
  inject **only** its `{env, secret_ref}` pairs, re-resolving each `secret_ref` value
  fresh (§5). A manifest that now declares more/different `worker_secrets` than the
  grant records is **ignored** for injection; the module gets only what was
  consented. (A manifest that declares *fewer* is fine — the extra grant entries
  simply resolve to nothing new.)
- **Re-consent on change:** adding a `worker_secrets` entry, or changing an `env`/
  `secret_ref`, is a manifest change that must go back through inspect/scan/consent
  (the reinstall path already re-runs consent); the grant is rewritten from the new
  acknowledgement. There is no in-place widening.
- **Uninstall:** drop the module's grant rows alongside its data (the uninstall path
  already stops the worker + revokes the bridge token; the grant is removed there).

This is a **v1 build item**, not assumed-existing (§10). It is tracked in the
isolation spec's phase list as **phase 9** (`2026-06-27-out-of-process-backend-
isolation.md` §13), alongside the `module_secret_grants` table in that spec's
schema-changes section.

## 6. Resolution timing + rotation

- **Resolved at spawn**, not at install — so rotating the value in the store flows
  to the **next** worker start.
- The value is **resident in the worker env for the worker's process lifetime.**
  This is unavoidable for a held credential and is the defining cost of this tier:
  unlike `http.request` (resolved per call, never persisted in the worker), a
  worker-secret lives in worker memory/env until the process exits.
- **Rotation requires a worker restart** in v1 (re-inject on respawn). Live
  re-injection without restart is deferred (§9).
- **Log hygiene is a hard requirement, with a stated v1 mechanism:**
  - The supervisor **never logs the `injected` map** or resolved values (log the
    env *keys* only if anything, never values).
  - The worker bootstrap installs a **top-level exception handler that redacts**
    any `AGD_*` var and any `worker_secrets[].env` name (matched from the injected
    key set) before printing a traceback or env dump — so an unhandled crash cannot
    spill the credential to the per-module log.
  - Core dumps (which would contain the secret in worker memory) are addressed by
    the `RLIMIT_CORE` 0 / container default hardening, tracked in §11 — the
    redacting handler is the v1 control; disabling core dumps is the follow-up.

## 7. Tier policy (container-first) — and the tier is HOST-GLOBAL

**The isolation tier is a host-global setting, not per-module.** `_isolation_mode()`
resolves one mode (`in_process`/`subprocess`/`container`) for the whole host from
`AGD_MODULE_ISOLATION` / the persisted `module_isolation` config ("Global for now —
per-module opt-in is a later phase"). So the policy below applies to **every**
`worker_secrets` module on the host at once; it is not a per-module choice, and the
override is not a per-module switch. This framing matters for consent (below).

- **`in_process`** (host mode): the module's dual-mode `_host` facade reads the
  secret from the host directly (it already has host access). No injection; no change.
- **`container`** (host mode — the blessed mode for `worker_secrets`): every
  keyed module's secrets are injected into its own container env, confined to that
  container's PID namespace. A sibling module cannot read them; the host's own
  secrets are not in the container. This is the right home for held credentials.
- **`subprocess`** (host mode, same-uid): injection works identically, but a
  determined same-uid sibling can read a worker's `/proc/<pid>/environ` — the
  standing subprocess-tier caveat, now with real secrets present. **Policy:** under
  subprocess mode the host **refuses** to inject `worker_secrets` for **any** module
  unless the operator sets a **host-global** override
  (`AGD_ALLOW_WORKER_SECRETS_SUBPROCESS=true`). Flipping it on is a **host-wide**
  decision: it enables weaker (uncontained-vs-same-uid) injection for **every** keyed
  module on the host, not one.

**Consequence for consent (do not mislead the operator):** because the tier and the
override are host-global, the subprocess-override consent must be a **host-level**
acknowledgement that **names every module whose secrets become uncontained**, not a
per-module "allow this module's IMAP password" toggle. An operator enabling the
override for one keyed module is actually opening the weaker posture for all of them,
and the consent text must say exactly that. Per-module tier selection is only
possible if/when the host gains per-module isolation (an isolation-spec follow-up);
until then, this tier inherits the host-global model and is honest about it.

This keeps the honest-claim discipline of the isolation spec: the default (container)
makes the contained choice; the weaker choice is reachable, host-wide, and loud.

## 8. Consent + scanner

- **Scanner** (`scanner.py`): a non-empty `worker_secrets` emits a dedicated **HIGH**
  finding **per secret** — "this module receives `{secret_ref}` as `{env}` inside
  its sandbox process" — distinct from the network/filesystem/`assistant.complete`
  findings. Mirrors the existing undeclared-capability machinery, but here the mere
  declaration is HIGH because a credential crosses into the worker.
- **Consent modal**: a separate, visually-stronger block (the same treatment as the
  `http.request` mutating-endpoint consent) listing each injected secret and its
  `reason`, acknowledged separately from the rest of the grant: "This module will
  hold these credentials inside its process: …". The per-module install consent
  covers *which secrets this module holds*; the **subprocess-override** consent is a
  separate, **host-level** acknowledgement (§7) that names every keyed module whose
  secrets become uncontained-vs-same-uid — it is not part of one module's install.
- **`security.md`** gains a row in the enforcement matrix: a module with
  `worker_secrets` holds those **named** credentials inside its worker; everything
  else stays contained (no host DB, no other secrets, no host imports, fault
  isolation). Under the container tier the credential is confined to the container;
  under subprocess same-uid it is not — hence the container-first policy.

## 9. Customers (why this is shared, not one-off)

- **Agent Fleet (Option B)** — LLM key + LangSmith key + n8n API key in-worker. This
  tier is exactly what its spec deferred; it **unblocks** agent-fleet's migration to
  the sandboxed path (chosen Option A / `in_process`-only precisely because this tier
  did not exist). To be clear: this tier *enables* that migration, it does not
  *perform* it — agent-fleet must then be re-pointed at `worker_secrets` + a baked-in
  `langgraph` image extra as its own follow-on.
- **Support / ticketing** — IMAP poll (mailbox password) and the SMTP reply fallback
  (SMTP password). The webhook-intake v1 needs none of this; the IMAP fast-follow
  rides this tier.
- **Redis / queue monitor, Postgres/MySQL viewer** — if ever built as community,
  their AUTH/DB passwords are held credentials with no HTTP bridge. (The candidates
  doc still rates the DB viewer better as a built-in; this tier does not change that
  verdict, only removes the *can't-inject-the-credential* blocker.)
- Any keyed native-wire module.

Most modules need **neither** this tier nor a held credential — they stay on the
bridge. This exists for the minority that genuinely cannot.

## 10. Phasing

- **v1:** manifest `worker_secrets` schema + name validation (incl. the
  `capabilities.env` collision rejection); **the consent grant store (§5.1) — a new
  transactional artifact, not the audit row**; resolve-at-spawn injection in both
  `supervisor._build_env` and `containers._injected_env`; scanner HIGH per secret
  (+ empty-`reason` INFO); the host-level consent block; **host-global**
  container-first policy with the **host-global** subprocess override; log hygiene
  (redacting bootstrap handler); `security.md` matrix row.
- **Deferred:** live re-injection on rotation without a worker restart (needs a
  re-inject/respawn signal); a per-secret audit trail of when a worker last received
  a given secret; per-module secret-scoping UI beyond the install consent.

## 11. Open questions

- **Whether to forbid the subprocess override entirely.** §7 makes it a host-global,
  off-by-default toggle. Some orgs may want it hard-disabled (container-or-nothing for
  keyed modules). Ship the toggle, or ship container-only? (Recommend: ship the
  toggle, default off, loud.)
- **Same `secret_ref` across modules.** Two modules consenting to the same ref each
  get their own injected copy — fine, but worth stating in docs so it is not read as
  a shared handle.
- **Crash-dump / core-dump exposure** of the worker's held secret on a hard crash.
  The redacting bootstrap handler (§6) covers logged tracebacks; disabling core dumps
  (`RLIMIT_CORE` 0 / container default) is the deeper control — v1 or a follow-up?

## 12. Testing

- **Manifest:** `worker_secrets` with a reserved `AGD_*` or `_ENV_ALLOW` `env` name
  rejected; a non-`^[A-Z][A-Z0-9_]{0,63}$` `env` or `secret_ref` rejected; an `env`
  that also appears in this module's `capabilities.env` rejected at parse
  (collision rule, §4); `secret_ref` carrying a value-looking string is still treated
  as a name (no value ever in the manifest); an empty `reason` yields a scanner INFO,
  not a load failure.
- **Grant store (§5.1):** installing writes the consent grant transactionally (a
  forced grant-write failure fails the install, unlike the best-effort audit row);
  the grant records the `{env, secret_ref}` pairs, not the raw capabilities blob;
  uninstall drops the grant.
- **Env builder:** a consented worker-secret appears under its declared `env` name in
  the worker env with the resolved value; a secret present in the **manifest** but
  **not in the grant** is NOT injected, and a manifest edited on disk to add a
  `worker_secrets` entry after install does NOT widen injection (grant is source of
  truth); the rest of the allowlist is unchanged (assert none of `{SECRET_KEY,
  *_TOKEN, QDRANT_*}` leak, and that a **sibling module's** `AGD_BRIDGE_TOKEN`/proxy
  secret never appears — only this worker's own handshake vars do); `is_secret_like`
  does not strip the injected value (it is in `injected`, applied last).
- **Missing secret:** a consented `secret_ref` with no value fails the spawn closed
  with a clear error, no half-started worker.
- **Rotation:** changing the store value and respawning injects the new value;
  without respawn the old value persists (documented v1 behavior).
- **Tier policy:** subprocess injection is refused without the override and allowed
  (with the louder consent recorded) when the override is set; container injection
  places the value in the container env and not in any published port.
- **Scanner:** non-empty `worker_secrets` → HIGH per secret; empty/absent → none.
- **Log hygiene:** spawn writes neither the resolved value nor the `injected` map to
  the module log or the host log; a forced worker crash does not emit env.
- `uv run pytest`; lint touched files `uvx ruff check` (line-length 120).
