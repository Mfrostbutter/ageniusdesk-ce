# Import & Export

AgeniusDesk moves n8n workflows in and out of an instance as plain JSON. The **Import Workflows** view brings JSON into the active instance (single or bulk, file or paste, with optional rename and tagging). The **Export / Backup** view pulls workflows back out: scheduled snapshots of the whole fleet, individual selections, a full backup file, or an active-only backup, plus a drop zone to restore a backup file. The on-demand exports act on whichever instance is currently active; scheduled backups fan out across every connected instance. See [Workflows](workflows.md) for browsing and running what you import, and [Authentication & RBAC](../architecture/auth.md) for who can perform these actions.

---

## Import workflows

The Import view is split into an options card, an upload card, a paste card, and a session-only import history.

### Choose the target instance

If more than one n8n instance is configured, a **Import to** card appears at the top with one button per instance.

1. Click the instance button you want to import into. The active instance is highlighted.
2. Selecting a button activates that instance on the backend (`POST /api/n8n/instances/{id}/activate`), so every import below goes to it.

If only one instance is configured, this card is hidden and imports go to the active instance automatically.

### Set import options

The **Import options** card applies to every workflow imported during this session.

| Field | Behavior |
|---|---|
| Title override | Replaces the workflow's `name` from the JSON. Optional. Ignored on bulk uploads (it would clobber every file to the same name). |
| Tags | Comma-separated. Each tag is created in n8n if it does not already exist, then attached to the imported workflow. |

### Import a single workflow (file)

1. In the **Upload JSON** card, click the drop zone (or drag one `.json` file onto it).
2. The browser parses the file, then posts it to `POST /api/n8n/import` with your title override and tags.
3. On success a toast shows the final name and any tags applied, and a row is added to Import History.

### Import multiple workflows (bulk)

1. Select or drag more than one `.json` file at once.
2. Each file is imported in turn. Only files ending in `.json` are accepted from a drag-drop.
3. The title override is suppressed for bulk imports; each workflow keeps its own name from the JSON. Tags still apply to all of them.

### Import by pasting JSON

1. Paste raw n8n workflow JSON into the **Paste JSON** card. The expected shape is an object with `name`, `nodes`, and `connections`.
2. Click **Import**. Invalid JSON is rejected with a parse-error toast before anything is sent.
3. **Clear** empties the text area.

### Import History

Each import (success or failure) is logged in the **Import History** card for the current page session. A green `OK` or red `FAIL` pill shows the result, the message shows the final name and workflow ID (and any tag warning), and the source column shows the file name or `pasted`. History is in-memory only and resets on reload.

### Endpoint reference

`POST /api/n8n/import` accepts either a raw workflow object or the wrapped shape `{workflow, name_override?, tags?}`. The wrapped shape is detected when the body has a `workflow` key whose value is a dict containing `nodes`. The response includes `workflow_id`, `name`, `tags_applied`, and an optional `warning` if tags could not be attached.

---

## Export & backup

The **Export / Backup** view has four sections: a scheduled-backups card, a full-backup row, an individual-export checklist, and a restore drop zone.

### Scheduled backups

Unlike the on-demand exports below (which download to your browser and act on the active instance only), scheduled backups run on the server and snapshot **every connected instance** to disk on a schedule. They are **off by default**.

1. In the **Scheduled Backups** card, toggle **Enabled**.
2. Set **Every (hours)** (the interval, 1 to 720), **Keep (snapshots/instance)** (retention, 1 to 500), and optionally **Active workflows only**.
3. Click **Save**. The schedule takes effect immediately, no restart.
4. Click **Back up now** to run a snapshot on demand at any time.

Each run writes one JSON snapshot per instance under `data/backups/<instance_id>/<timestamp>.json` (the same envelope as a full backup), then prunes each instance's folder to the retention count, keeping the newest. A failing or unreachable instance is isolated: the rest still snapshot, and the card's status line shows the last run's result and the next scheduled run. Stored snapshots are listed per instance below the controls with a download link and size; snapshots from an instance you later remove are still listed (flagged "removed instance") so you can recover them.

The interval scheduler is dependency-free and in-process, so backups run only while the dashboard is running. The schedule persists in `config.json`, so it survives restarts.

**Endpoints** (operator role): `GET/PUT /api/backups/settings`, `GET /api/backups` (list), `POST /api/backups/run` (run now), `GET /api/backups/{instance_id}/{filename}` (download), `DELETE /api/backups/{instance_id}/{filename}`.

### Full backup

1. Click **All Workflows** to back up every workflow, or **Active Only** to back up just the workflows that are active in n8n.
2. The browser calls `GET /api/n8n/backup?active_only=<bool>`, which streams a single JSON file as a download.
3. The download filename comes from the server: `n8n-backup-<instance>-<timestamp>.json`.
4. A status line confirms the workflow count and filename.

The backup file is a single JSON object:

```json
{
  "backup_version": "1.0",
  "created_at": "2026-06-26T12:00:00+00:00",
  "instance": { "name": "prod", "url": "https://n8n.example.com" },
  "workflow_count": 42,
  "active_only": false,
  "workflows": [ /* full workflow objects */ ]
}
```

### Export selected workflows

The **Export Individual** card lists every workflow (up to 250) with a checkbox, an online/offline status dot, and an active/off pill.

1. Use **Select All** or **Select None** to toggle the checklist, or check workflows individually. All are checked by default.
2. Click **Export Selected**.
3. Each selected workflow is fetched one at a time via `GET /api/n8n/workflows/{id}/export`, then assembled client-side into one file named `n8n-export-<N>wf-<date>.json`.

The selected-export file uses the same `backup_version: "1.0"` envelope as a full backup, with `instance.name` set to `selected-export`.

### Restore from a backup file

Restore imports workflows from a backup file into the **active** instance.

1. In the **Restore from Backup** card, click the drop zone (or drag a single backup `.json` onto it).
2. The file is parsed and each workflow is sent to `POST /api/n8n/import` one at a time.
3. Restored workflows are imported as **inactive**; activate them from [Workflows](workflows.md) when ready.
4. Results show a count of imported vs failed, then a per-workflow `OK`/`FAIL` line with any error message.

The restorer accepts two shapes:

| File shape | Treated as |
|---|---|
| Object with a `workflows` array | Full backup or selected export; each entry is imported. |
| Object with a top-level `nodes` key | A single workflow file; imported on its own. |
| Anything else | Rejected with `Unrecognized backup format`. |

> Note: restore always targets the active instance. Switch the active instance first (Import view, or [Instances](instances.md)) if you need to restore elsewhere. Restore does not apply the Import view's title override or tags.
