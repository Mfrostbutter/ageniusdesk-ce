# Spec: Community Module Security (Scan + Consent) and the YouTube Research Module

Status: Draft
Date: 2026-06-26
Owner: Michael Frostbutter
Scope: AgeniusDesk Community Edition
Release gate: no (target: next release after 0.1.0)
Decision on record: ship scan + consent now; out-of-process isolation is roadmap, not this release.

## 1. Goal

Make installing a community module a deliberate, informed act instead of a blind
`import`. Before any third-party module is registered, the operator sees what the
module declares it needs, what a static scan actually found in its code, and any
gap between the two, then explicitly consents. The YouTube research module is the
first real community module and the forcing function we design this against.

Two deliverables, coupled on purpose:

1. A capability-declaration + static-scan + install-consent pipeline for community
   modules.
2. The YouTube research module, packaged as the first installable community module.

## 2. Non-goals (this spec)

- **Out-of-process isolation / sandboxing.** This is the only real security
  boundary and it is explicitly deferred to a later release (see Section 9). Until
  then, community modules run in-process with full Python access, and the
  scan/consent flow is defense-in-depth, not containment.
- A hosted module registry / marketplace backend. Install stays repo-based
  (GitHub tarball, already implemented).
- Re-scanning or runtime monitoring of an already-installed module.
- Bundling GPU compute or whisper. v1 is captions-API only. A whisper fallback is
  deferred to a later version (see Section 9), and nothing GPU ever ships by
  default.

## 3. Honest framing (do not skip)

A static scanner over Python that executes in-process is a heuristic, not a
boundary. A determined author bypasses pattern matching trivially
(`getattr(__import__('os'), 'system')`, base64-then-exec, a payload fetched at
runtime, dynamic import). The value of this work is:

- catching low-effort or accidental danger,
- forcing an explicit informed-consent moment,
- recording a tamper-evident audit trail of what was approved.

Every piece of user-facing copy and every doc must say this plainly. We do not
ship a "scanned and safe" badge. The phrasing is "heuristic review, not a
sandbox. Only install modules you trust." Overstating the guarantee is worse than
omitting it.

## 4. Current state (analysis)

What exists today:

- `backend/modules/modules/installer.py`: downloads a GitHub tarball pinned to a
  tag/SHA, records the resolved commit, sha256s the archive, and hardens
  extraction (rejects symlinks, special files, and path traversal; uses the
  tarfile `data` filter on 3.12+). Its own header states: "No sandboxing; modules
  run in-process with full Python access." There is no review of what the code
  does.
- `backend/module_registry.py`: `ModuleManifest` (id, name, version,
  min_app_version, description, author, author_url, repo, license, routes_prefix,
  python_entry, `secrets_required`, frontend decl, builtin, homepage). `RegistryEntry`
  carries `installed_sha`, `status`, `error`, `missing_secrets`. `version_tuple` /
  `is_compatible` gate on `min_app_version`. No capability surface, no scan.
- `backend/modules/__init__.py` `register_modules`: built-ins first, then community
  from `data/modules/{id}/`; a community module that fails to import is recorded
  `status=failed` rather than crashing boot.
- `backend/modules/modules/router.py`: the install/list/remove HTTP surface.
- `docs/architecture/security.md`: lists in-process community modules as an
  explicitly accepted risk.

Gap: nothing inspects module code or intent before registration, and the operator
gets no consent moment.

Source to port for the module itself: the upstream AgeniusDesk research module
(`router.py`, `artifacts.py`, `llm.py`) plus the optional whisper and captions
sidecars. It was stripped from CE on the way to open-source.

## 5. Design

### 5.1 Capability manifest

Extend `ModuleManifest` with a declared `capabilities` block. Declaration is the
module author asserting intent; the scanner (5.2) checks code against it.

```jsonc
"capabilities": {
  "network": { "enabled": true, "hosts": ["*.youtube.com", "api.openai.com"] },
  "filesystem": { "write_paths": ["research-output"] },   // relative to data/
  "subprocess": false,
  "env": ["WHISPER_URL", "CAPTIONS_URL"]
}
```

- `network.hosts` is an allowlist of hostnames/domains (glob allowed). Empty +
  `enabled:true` means "any host" and is itself a HIGH finding.
- `filesystem.write_paths` are paths under `data/` the module writes. Anything
  outside is a finding.
- `subprocess` declares whether the module spawns child processes.
- Secret reads continue to be declared via the existing `secrets_required`; the
  scanner cross-checks against it.
- Backward compatibility: a manifest with no `capabilities` block is treated as
  "declares nothing," so any detected capability becomes an undeclared finding.

### 5.2 AST static scanner

New module `backend/modules/modules/scanner.py`. Parses every `.py` file in the
extracted module with `ast`, walks the tree, and emits severity-ranked findings.
It never imports or executes module code.

Risk categories:

| Severity | Detect |
|---|---|
| CRITICAL | `eval` / `exec` / `compile`; `os.system`; dynamic `__import__` / `importlib.import_module` with a non-literal arg; `ctypes`; `pickle` / `marshal` loads feeding exec; base64/hex decode whose result flows into exec/import |
| HIGH | `subprocess` / `Popen` when `subprocess` not declared; raw `socket`; network libs (`httpx`, `requests`, `urllib`, `aiohttp`) when `network` not declared, or to a host not in the allowlist (best-effort literal extraction); `open(..., 'w'/'a'/'x')` outside declared `write_paths`; reads of `os.environ[...]` keys not declared; direct reads of `data/secrets.json`, `data/.secret_key`, `data/config.json` |
| MEDIUM | file reads outside the module directory; importing another community module; large obfuscated string literals; dynamic `getattr`/`setattr` on imported modules |
| INFO | declared capability with no matching code usage (over-declaration) |

Output: a `ScanReport` (list of findings with `severity`, `category`, `file`,
`line`, `detail`, and a `declared_vs_detected` diff). The diff is the headline: a
module that uses the network without declaring it, or writes outside its declared
paths, surfaces as HIGH "undeclared capability."

Limitations are documented in the report itself (cannot follow obfuscation or
runtime-fetched code).

### 5.3 Two-phase install + consent

Split the current one-shot install into inspect then install.

- `POST /api/modules/inspect` `{ repo, ref }` -> downloads, validates the tarball
  (existing hardening), parses the manifest, runs the scanner, and returns
  `{ manifest, capabilities, scan_report, resolved_sha }` **without registering**.
  Extraction goes to a staging dir that is discarded if install is not confirmed.
- `POST /api/modules/install` `{ repo, ref, resolved_sha, consent }` -> requires
  the `resolved_sha` from inspect to still match (guards against a swapped tag),
  records the consent, then extracts to `data/modules/{id}` and registers.

Consent gating:

- CRITICAL findings: require a typed confirmation (operator types the module id)
  to proceed. Never silently block; it is the operator's box, but the friction is
  proportional to risk.
- HIGH findings: require an explicit checkbox acknowledging undeclared/elevated
  capabilities.
- The operator can always proceed after consent. We inform, we do not forbid.

Frontend: a consent modal in the module manager (`frontend/js/views/settings-modules.js`)
that renders the capability list, the severity-colored scan findings, the
declared-vs-detected diff, and the honest-framing copy, with Approve / Cancel.

### 5.4 Provenance and audit record (light)

- Keep the existing sha pinning and resolved-commit recording.
- Optional manifest `signature`: if present, verify against the author's public
  key and show "signed by <author>" vs "unsigned." Key distribution is out of
  scope for this release; verification is best-effort and additive.
- Persist an audit record per install: new SQLite table `module_installs`
  (`module_id`, `repo`, `resolved_sha`, `capabilities_json`, `scan_summary`,
  `approved_by`, `approved_at`). This makes "what did we agree to, and when" a
  query, not a memory.

## 6. The YouTube research module (first community module)

Distributed as its own GitHub repo (proposed: `ageniusdesk-module-youtube-research`),
installed through the flow above.

- **Port from** the upstream AgeniusDesk research module (`router.py`,
  `artifacts.py`, `llm.py`), adapting to the CE module manifest + capability model.
- **Compute (captions-only in v1, lightweight by design).** The captions API is
  the only transcription path in v1. It has been the reliable path in practice and
  needs no GPU and no sidecar. A whisper fallback (for videos without captions) is
  deferred to a later version; see Section 9. Nothing GPU or sidecar-related ships
  in v1.
- **Manifest capabilities** (drives the scanner test):
  - `network.hosts`: YouTube + the captions API host + the configured LLM provider
    host
  - `filesystem.write_paths`: `workspace/research` (the Harness vault research
    tree; see 6.1)
  - `subprocess`: false (captions and the optional sidecar are reached over HTTP,
    not spawned)
  - `secrets_required`: the LLM API key (+ captions key if the chosen captions
    provider requires one)
- **Why it is the right first module.** It exercises network, filesystem writes,
  external secrets, and an external compute dependency, which is exactly the
  surface the scanner and consent flow must handle. We build the module and the
  security pipeline against each other.

### 6.1 Intake, classify, and auto-file workflow

The module does not just produce a breakdown; it files it. The research vault is
the existing Harness notes vault (`data/workspace/`), which already has a
`research/` folder, so breakdowns become first-class searchable notes (FTS, tags,
backlinks) instead of loose files.

Flow:

1. **Intake to Inbox.** A submitted video's breakdown is written first to
   `research/inbox/` (the unsorted drop). Nothing is lost even if classification
   fails.
2. **Classify + tag.** The breakdown model returns structured output alongside the
   summary: a single best-fit `topic` and a list of `tags`. Classification is
   **constrained to the existing topic folders** (the model is given the current
   `research/` subfolders as the candidate set), not free-form, to stop the vault
   fragmenting into dozens of near-duplicate topics.
3. **Auto-file.** The note is moved from `research/inbox/` to `research/<topic>/`
   and the tags are written into the note frontmatter. If the model has no
   confident fit, the note stays in `research/inbox/` for manual filing rather
   than inventing a topic. (Open question: allow the model to propose a *new*
   topic above a confidence threshold, or always require manual creation.)
4. **Write path.** Filing reuses the existing workspace write tools
   (`write_note` / move / tag) rather than raw filesystem writes, so everything
   is indexed and the capability surface is a single declared vault path.

Capability impact: `filesystem.write_paths` is `workspace/research` (the vault
research tree), and the module reads the existing topic folders to build the
candidate set.

### 6.2 Scaffolded starter taxonomy

On first run the module seeds a small, generic topic set so classification has
targets out of the box (all under `research/`, operator-editable, just folders):

- `inbox` (unsorted intake; never a classification target)
- `ai-and-llms`
- `automation-and-n8n`
- `business-and-marketing`
- `engineering-and-devtools`
- `productivity`
- `misc` (catch-all when a fit is weak but not zero)

The taxonomy is a starting scaffold, not a fixed schema: the operator adds or
removes folders and the classifier adapts because it reads the live folder list
as its candidate set.

## 7. Data and schema changes

- `ModuleManifest`: add `capabilities` (optional, defaults to "declares nothing").
- New: `backend/modules/modules/scanner.py` (`ScanReport`, `Finding`, severities).
- New SQLite table `module_installs` (audit), migrated idempotently in
  `backend/database.py:_migrate()`.
- `RegistryEntry`: optionally carry the approved capability set + scan summary for
  display in the module manager.

## 8. API and UI changes

- `POST /api/modules/inspect` (new): dry-run, returns manifest + capabilities +
  scan report + resolved_sha.
- `POST /api/modules/install` (changed): requires `resolved_sha` + `consent`.
- Module manager view: consent modal, per-installed-module capability/scan summary,
  honest-framing copy throughout.
- Docs: update `docs/architecture/modules.md` (capability schema + install flow)
  and `docs/architecture/security.md` (accepted risk becomes "accepted now,
  isolation planned"; document the scanner's stated limits).

## 9. Roadmap (explicitly deferred)

Out-of-process isolation is the only real boundary and is the headline security
item for a later release: run community modules in a subprocess with dropped
privileges and a restricted network/filesystem behind an RPC contract, instead of
in-process import. This spec's scan/consent layer is the bridge until then.

Whisper transcription fallback for the YouTube module is also deferred. v1 is
captions-only because captions has been the reliable path in practice; the whisper
hook (a documented compose snippet, never a bundled dependency) is added only when
there is a demonstrated need for videos without captions.

## 10. Implementation phases

1. Capability manifest schema + validation (`module_registry.py`).
2. AST scanner (`scanner.py`) + fixtures.
3. Two-phase inspect/install + consent (backend `router.py`, `installer.py`) and
   the `module_installs` audit table.
4. Consent modal + module-manager surfacing (`settings-modules.js`).
5. Optional signature verification + provenance display.
6. YouTube research module: port, manifest, captions-only transcription, the
   intake/classify/auto-file workflow (6.1), and the scaffolded taxonomy (6.2), in
   its own repo.
7. Tests + docs.

## 11. Testing

- Scanner unit tests against fixtures: a benign module (no findings), an
  obfuscated-exec module (CRITICAL), an undeclared-network module (HIGH diff), an
  out-of-dir write (HIGH), an over-declared capability (INFO).
- Install-flow tests: inspect returns a report without registering; install
  rejects a sha mismatch; consent is recorded; CRITICAL requires typed
  confirmation.
- Capability-diff tests: declared-vs-detected reconciliation.
- YouTube module: a smoke test of the captions path (the only transcription path
  in v1) end to end; and a classify/file test asserting a breakdown lands in
  `research/inbox/` then moves to the chosen `research/<topic>/` with tags, and
  stays in inbox when no confident topic fits.
- Run with `uv run pytest`; lint touched files with `uvx ruff check`.

## 12. Open questions

- Signature infrastructure: where author keys come from and how they are trusted
  (defer, but decide the manifest field shape now).
- Do we ever hard-block, or always allow-with-consent? Current stance: always
  allow after proportional friction.
- Whether the capability `network.hosts` allowlist should be enforced at runtime
  later (it is declaration-only in this release).
- YouTube classifier: may it propose a *new* topic folder above a confidence
  threshold, or always fall back to `inbox` for manual creation (6.1, step 3)?
