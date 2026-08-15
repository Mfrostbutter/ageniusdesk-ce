# Feature QA pass, 2026-08-14

Manual walkthrough of every shipped view against the v0.5.0 code, run on the local dogfood instance (`agd-otel-dashboard-1`, http://localhost:3066).

Purpose: confirm each feature actually works end to end, log what does not, fix it.

## Status legend

- `OPEN` logged, not yet diagnosed
- `DIAG` root cause identified, fix not written
- `FIXED` fix in the working tree
- `SHIPPED` fix committed
- `WONTFIX` intentional behavior, closed with a note

## Bugs

### QA-001 Observe reports fleet-wide span volume against an instance-scoped view

- **Status:** FIXED
- **View:** Observe
- **Repro:** Open Observe with an active instance that has never exported a span, while another registered instance has spans on file.
- **Expected:** The view says this instance is not exporting and how to wire it.
- **Actual:** Header reads `Receiver on` + `30 spans`, body reads `No traces yet. Run a workflow on the active instance.` Every metric reads zero. Running workflows (one every 15 min) changes nothing, and the view gives no hint why.
- **Root cause:** `/api/otel/status` called `count_spans()` with no filter (fleet-wide), while `/api/otel/traces` and `/api/otel/metrics` scope to `get_active_instance_id()`. The two disagreed, and the header's number implied ingest was working. Compounding it, `setupHtml()` rendered only when the receiver was *off* (`if (!status.enabled)`), so the receiver-on-but-instance-unwired case, the one an operator actually hits, got no setup guidance at all.
- **Fix:** `count_spans(instance_id="")` takes an optional filter; `/status` returns `instance_span_count` and `instance_name` next to the fleet total. The badge now counts the active instance and turns amber at zero, with a separate `+N other` pill for spans held for other instances. When the receiver is on and the active instance has zero spans, the body renders the setup panel addressed to that instance by name, with the concrete endpoint filled in from `window.location.origin` instead of a `<this-host>:<port>` placeholder.
- **Note:** The zeros themselves were environmental. The `Test` instance is URL-connected, so it was never auto-wired for OTel export, exactly the v0.5.0 upgrade note. The bug is that the UI could not say so.

### QA-002 mcp 2.0 silently unmounts the built-in MCP server on any fresh build

- **Status:** FIXED
- **View:** n/a (startup / packaging)
- **Repro:** Build the Docker image from a clean cache today.
- **Expected:** `dashboard_mcp` registers and mounts at `/api/mcp-dashboard`.
- **Actual:** `Failed to load module dashboard_mcp: No module named 'mcp.server.fastmcp'` and `Dashboard MCP mount failed`, as WARNING lines. 19 modules register instead of 20. The app otherwise starts normally, so the built-in MCP server is simply gone with no user-visible signal.
- **Root cause:** `pyproject.toml` pinned `mcp>=1.2.0` with no ceiling. `mcp` 2.0.0 removed `mcp.server.fastmcp` (the API moved to `mcp.server.mcpserver`), which is the import in `backend/modules/dashboard_mcp/server.py`. `Dockerfile:15` installs with `pip install ".[${AGD_EXTRAS}]"`, which ignores `uv.lock`, so every build resolves the newest `mcp` rather than the locked one. Caught because a rebuild of the dogfood instance dropped from 20 modules to 19; the image built 2026-08-13 predated the 2.0 release.
- **Fix:** Pinned `mcp>=1.2.0,<2.0.0` with a comment naming the removed module, and relocked. Rebuild confirmed: 20 modules, server mounted.
- **Follow-up:** Two open items, both logged below as QA-003 and QA-004.

### QA-003 Docker builds ignore uv.lock

- **Status:** FIXED
- **View:** n/a (packaging)
- **Repro:** Inspect the old `Dockerfile:15`.
- **Expected:** A build reproduces the locked dependency set.
- **Actual:** `pip install ".[${AGD_EXTRAS}]"` resolved fresh against PyPI. `uv.lock` was committed but never consulted by the image build, so the deployed artifact drifted from the tested one and an upstream major release landed straight in a user's build. This is the root enabler of QA-002; the version ceiling fixed that instance, not the class.
- **Fix:** The build now copies `uv.lock`, runs `uv export --frozen --no-dev --no-emit-project` with one `--extra` per entry in `AGD_EXTRAS`, and installs the result with `pip install --require-hashes`. `--no-emit-project` preserves the existing deps-only property (the `backend` package stays out of site-packages so a sandboxed module worker can exclude it from `sys.path`), so nothing else about the image changes. uv is pinned at `ghcr.io/astral-sh/uv:0.11.8` rather than `:latest`, since pulling an unpinned toolchain to fix unpinned dependencies would be self-defeating.
- **Verified:** Default (`assistant`) image builds and boots with 20 modules. The `assistant,langgraph` export resolves too, and the comma-to-flags shell loop was tested in isolation.

### QA-004 Dashboard MCP server is tied to the mcp 1.x API

- **Status:** FIXED (ceiling stays, by design)
- **View:** n/a
- **Finding that changed the plan:** Porting to 2.x-only would have broken the Agent Fleet. `pydantic-ai` (langgraph extra) pulls `fastmcp-slim`, which declares `mcp<2.0,>=1.24.0`. Requiring `mcp>=2.0` makes `AGD_EXTRAS="assistant,langgraph"` unresolvable. So the `<2.0.0` ceiling is the correct constraint today, not a stopgap, and it lifts when pydantic-ai moves, not when we do.
- **Fix:** `server.py` now supports both SDK lines. It tries `mcp.server.mcpserver.MCPServer` and falls back to `mcp.server.fastmcp.FastMCP`, with the transport options (`streamable_http_path`, `transport_security`) passed on the constructor for 1.x and on `streamable_http_app()` for 2.x, which is where 2.0 moved them. The tool decorator and `TransportSecuritySettings` are identical across both, so the shim is confined to construction and mount.
- **Verified:** Forced both versions into the built image against the working tree. 17 tools registered and a Starlette app built under mcp 1.29.0 and 2.0.0 alike.

### QA-005 A built-in module failing to load is only a WARNING

- **Status:** FIXED
- **View:** n/a (startup)
- **Actual:** The module loader treated a built-in failing to import the same as a community module failing: one WARNING line, startup continues. QA-002 was invisible for exactly that reason. A community module failing soft is correct; a shipped built-in silently disappearing is not.
- **Fix:** A built-in failure now logs at ERROR with the traceback and states that its features are unavailable. `main.py` additionally re-states any failed built-ins immediately after the "Registered N modules" roster, since that roster line is the one an operator actually reads and the original failure can be hundreds of lines earlier. The registry already recorded `status="failed"`, so the Modules view data was never the gap; the logging was.

### QA-006 A rejected OTLP export is invisible on the dashboard

- **Status:** OPEN
- **View:** Observe
- **Repro:** Point an n8n instance at the receiver with a bearer that does not match `AGD_OTEL_TOKEN`. Run workflows.
- **Expected:** The dashboard shows that exports are arriving and being refused.
- **Actual:** The receiver answers `401 Invalid or missing OTel token` and moves on, logging nothing. n8n logs `OTLPExporterError: Unauthorized` into its own container logs, where nobody is looking. Observe shows an ordinary empty state. The two systems each know something is wrong and neither says so anywhere the operator will see.
- **Impact:** This is how the 2026-08-13 → 08-14 gap went unnoticed for ~28 hours. Every trace in that window is unrecoverable; the tokens on the two sides had drifted apart.
- **Fix:** Not written. A rejected-ingest counter (401s and parse failures, per source IP, over a rolling window) surfaced on Observe next to `Receiver on` would have made this a ten-second diagnosis. Pairs with the trace-backfill work: the counter is the alarm, backfill is the cure.
- **Related:** [Trace backfill spec](../specs/2026-08-14-trace-backfill-from-execution-history.md)
- **Update 2026-08-15:** the cure shipped. Backfill Phase 1 is built and rebuilt this exact window: 69 executions, 294 spans, enrichment included, idempotent on re-run. The counter (the alarm) is still the open half of this item.

### Environment fix (not a code defect)

The `Test` instance was wired for OTel export all along; the dashboard and the n8n container simply held different tokens. Rotated to a fresh token on both sides (`.local/otel.override.yml` and `/opt/n8n-dev/docker-compose.yml`, the latter backed up first) and restarted both. Verified: token hashes match, zero 401s since, and live traces are landing and correctly attributed to the active instance.

## Test coverage

`tests/test_qa_pass_2026_08_14.py` locks all five in: instance-scoped span counts and the `/status` contract, the setup-panel branch in the Observe view, the mcp ceiling and its agreement with the lockfile, the dual-SDK shim, `dashboard_mcp` being registered at all (so a future dependency drift fails the suite instead of shipping), the Dockerfile's locked-install properties, and the ERROR-level built-in failure path. Full suite: 487 passed.

<!--
Template:

### QA-001 Short title

- **Status:** OPEN
- **View:** Overview
- **Repro:** what was clicked
- **Expected:** what should happen
- **Actual:** what happened
- **Root cause:**
- **Fix:**
-->

## Views covered

| View | Walked | Result |
| --- | --- | --- |
| Overview | no | |
| Workflows | no | |
| Executions | no | |
| Errors | no | |
| Promote | no | |
| Observe | yes | QA-001 fixed; instance still needs OTel export wired to see real traces |
| Insights | no | |
| Fleet Health | no | |
| Assistant | no | |
| Code Lab | no | |
| Agent Fleet | no | |
| Knowledge | no | |
| Notes | no | |
| Containers | no | |
| Modules | no | |
| Secrets | no | |
| Settings / Admin | no | |
| Backups / Export | no | |
| Themes | no | |
| Player | no | |
