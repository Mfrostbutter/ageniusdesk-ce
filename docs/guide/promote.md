# Workflow Promotion

Promotion moves a workflow from one registered n8n instance to another: dev to staging, staging to production. It is the open-source answer to n8n Enterprise environments.

The hard part is never the workflow JSON. It is the credentials. A workflow that runs fine on dev references credential ids that do not exist on prod, and n8n binds a node credential by **both** id and name, so a workflow imported with a half-correct mapping activates and then fails at run time on the first node that needs the missing credential. Promotion is built around that problem: a preflight tells you what will break before anything is written, and activation is refused rather than allowed to fail silently.

You need the **operator** role. The whole `/api/promote` surface is gated there.

## The flow

Open the **Promote** view from the sidebar.

1. **Pick a source instance** and click **Load workflows**. The source is probed first, so an unreachable or mis-keyed instance fails in about four seconds with a real error instead of showing you an empty workflow picker.
2. **Select the workflows** you want to move.
3. **Pick a target instance.**
4. **Run Preflight.** Nothing is written yet.
5. **Resolve the credential mapping** the preflight reports, using auto-provision or by naming target credentials yourself.
6. **Promote.**

## What preflight tells you

Preflight reads each selected workflow and reports, per workflow:

- Every credential the workflow's nodes bind, by type and name.
- Whether the target instance already ships a credential of that type.
- Duplicate-name collisions, so you know when promoting will land next to a workflow of the same name on the target rather than replacing it.

Preflight is bound to the exact source, target, and selection you ran it against. Changing the target or the workflow selection **invalidates it**, and you have to run it again. This is deliberate: it stops you preflighting against staging and then firing the promote at production.

## Credential mapping

Every source credential needs a target credential id, or the promoted workflow will not run. You have three options per row.

**Auto-provision.** AgeniusDesk resolves the gap for you. It first looks for a credential it has already mirrored onto the target and reuses that. If there is none, it creates one from a matching entry in your [Secrets store](secrets.md), through the same credential-mirror path the Secrets view uses.

Two guarantees are worth knowing:

- **Ambiguity is never guessed.** If the target already has more than one mirror of a credential type, auto-provision does not pick one. It surfaces the row as ambiguous and waits for you to choose. Silently binding two distinct source credentials to one arbitrary target credential is exactly the failure that is impossible to debug later.
- **Provisioning is idempotent by reuse, not by recreate.** An already-mirrored secret returns the existing credential id and issues no write to n8n. It does not delete and recreate, which would mint a new id and orphan any workflow you promoted earlier that is still pointing at the old one, possibly while active.

Auto-provision runs through the same guardrails as the manual mirror route: the SSRF floor on the target URL, the per-secret instance scope, and the URL-repoint check. A secret scoped to dev cannot be pushed to a prod target.

**Name a target credential yourself.** Type the exact target credential name into the row. AgeniusDesk can only see credentials it created on the target; n8n's API does not expose credentials made by hand in the n8n UI, so for those you supply the name.

**Leave the row blank.** The workflow imports with that credential unlinked. Useful when you intend to wire it in n8n afterwards. It will not activate (see below).

## Activation guarding

Tick **Activate on target** and promotion will try to activate each imported workflow. It refuses when any mapped credential has no name on the target, because n8n needs both the id and the name to bind a node credential, and importing something that will fail on its first run is worse than not importing it.

When n8n itself rejects an activation, you get n8n's own node-by-node explanation of which credential is missing, not a bare `HTTP 400`.

## Options

| Option | What it does |
|---|---|
| **Activate on target** | Activate each workflow after import, subject to the guard above. Off by default. |
| **Name suffix** | Append a string to each promoted workflow's name (for example ` (from dev)`). Useful when promoting into an instance that already holds a workflow of the same name. |
| **Dry run** | Run the whole promotion path and report what would happen without writing to the target. |

## API

All endpoints require the operator role.

| Endpoint | Purpose |
|---|---|
| `GET /api/promote/workflows/{instance_id}` | List the workflows on an instance (probes liveness first). |
| `POST /api/promote/preflight` | Report credentials, target coverage, and duplicate names for a selection. |
| `POST /api/promote/auto-provision` | Reuse or create target credentials from the Secrets store. Accepts `secret_choices` to resolve ambiguous rows. |
| `POST /api/promote/run` | Perform the promotion. Takes `cred_map`, `cred_names`, `activate`, `name_suffix`, and `dry_run`. |

## Related

- [Secrets](secrets.md) for the store auto-provision draws from, and how `$NAME` references work.
- [n8n Instances](instances.md) for registering the source and target.
- [Import & Export](import-export.md) for moving a workflow to or from a file rather than another instance.
