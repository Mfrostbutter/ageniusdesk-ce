# Instance attribution for observed traces

How the OpenTelemetry receiver decides which configured AgeniusDesk instance an
incoming trace belongs to. This is load-bearing: cost enrichment, silent-failure
detection, and every per-instance metric key off the answer.

## The problem

n8n's native OTel export identifies its source only by an opaque resource hash
(`n8n.instance.id`, e.g. `895500a3…`), a container `host.name`, and
`service.name=n8n`. None of these match anything AgeniusDesk stores about an
instance (its display name and URL). The original matcher compared those
attributes against each instance's name/URL, never matched, and fell back to
**the active instance**.

Consequences of the fallback:

- Every observed trace was stamped with whatever instance was *active at ingest*,
  not its true source. In a multi-instance fleet, executions, error rates, and
  silent-failure counts were mis-attributed to whichever instance you were viewing.
- Enrichment broke. Cost and health enrichment fetch run-data from the n8n API;
  they used the active instance's client, which does not hold a foreign
  instance's execution, so they read empty and wrote **$0 cost / no health**.

## The mapping (two tiers + a safe bucket)

`instance_map.map_from_attrs` (pure, synchronous, runs in the ingest flatten
path) resolves a resource in this order:

1. **Deterministic stamp.** Every AgeniusDesk-provisioned n8n is booted with
   `OTEL_RESOURCE_ATTRIBUTES=agd.instance.name=<name>` (percent-encoded; see
   `docker_mgr/templates._otel_export_env`). The receiver matches that against
   the configured instance names. No probe, no ambiguity.
2. **Learned pin.** For external / legacy instances with no stamp, the opaque
   `n8n.instance.id` hash is looked up in `otel_instance_map`. A hash is learned
   once: `instance_map.resolve_hash` probes each configured instance's API for
   the trace's execution id and confirms the workflow id matches (execution ids
   are small integers that collide across instances, so the workflow id is the
   disambiguator). The hash→instance result is pinned; every later trace with
   that hash maps in O(1).
3. **Unknown bucket.** A resource that is neither stamped nor pinned is parked in
   a stable `unknown-<hash>` bucket. It is never attributed to the active
   instance. After insert, `instance_map.learn_unknowns` resolves the batch's
   unknown hashes and re-attributes their spans to the real instance.

A degenerate exporter with no instance id at all (not real n8n) maps to `""`
(unattributed), which downstream enrichment treats as the default/active
instance — the pre-mapping single-instance behavior.

## Enrichment follows attribution

Once a trace is attributed, cost and silent-failure enrichment fetch run-data
from **that** instance via `n8n_client.get_execution_raw_by_instance` (the active
instance is the fast path; any other configured instance is fetched with its own
credentials). An `unknown-<hash>` trace is skipped until the learn step
re-attributes it. This is what lets cost and silent-failure detection work across
the whole observed fleet rather than only the instance currently in focus.

## Backfill

The mapping only affects new ingests. Historical spans stored under the old
active-instance fallback are corrected by a one-time backfill: for each distinct
exporter hash, resolve it by probe, pin it, and re-attribute every span in its
traces to the resolved instance.
