# Workflows

The **Workflows** view lists every workflow on the active n8n instance, lets you activate or deactivate them, trigger them on demand, and inspect recent execution history per workflow. It always reflects the [active instance](instances.md); switch instances and the list reloads against the new one.

The view is a two-pane layout: the workflow list on the left, and a detail panel on the right that fills in when you select a workflow.

## Listing and searching

When the view loads it fetches up to 250 workflows from the active instance. Rows are sorted active-first, then alphabetically. Each row shows:

| Element | Meaning |
|---|---|
| Status dot | Green when the workflow is active, gray when inactive. |
| Name | The workflow name. |
| ARCHIVED pill | Shown (with the row dimmed) when the workflow is archived in n8n. |
| Trigger pill | The detected trigger type: `webhook`, `schedule`, `manual`, `error`, or `unknown`. |

Controls in the header:

- **Search workflows...** filters the list by name (matched server-side, case-insensitive).
- **Active only** toggle. When on, only active workflows are listed. It defaults on for direct/sidebar navigation, and off when you arrive from a Dashboard card that asked for "all" or when deep-linking to a specific workflow.

## The detail panel

Click a workflow to load its detail panel, which fetches the full workflow plus its 15 most recent executions. The panel shows the name and ID, status chips (Active/Inactive, trigger type, node count, and an "Archived in n8n" chip when applicable), an action row, and a **Recent Executions** table.

## Activate and deactivate

The **Activate** / **Deactivate** button toggles the workflow's active state on n8n. Activating a workflow is what makes its trigger live (a schedule starts firing, a webhook registers, and so on). The list and detail panel refresh after the toggle.

## Trigger on demand

AgeniusDesk fires a workflow through a dashboard-owned webhook node named `__dashboard_trigger`, not by editing your real trigger. How the buttons behave depends on the workflow's trigger type:

- **Non-webhook workflows** (schedule, manual, error, unknown). If no dashboard trigger exists yet, click **Enable Dashboard Trigger**. This adds a `__dashboard_trigger` webhook node, wires it in parallel with your existing trigger, and (if the workflow is active) briefly deactivates and reactivates it so the new webhook registers. Once enabled, a **Trigger** button appears; click it to fire the workflow on demand. The workflow must be active for Trigger to work. **Remove Dashboard Trigger** deletes the injected node.
- **Webhook workflows.** AgeniusDesk will not inject a trigger. n8n cannot register two webhook triggers for the same workflow, and webhook workflows usually expect a specific payload the dashboard cannot supply. To fire one, call its existing webhook URL directly. If an orphaned `__dashboard_trigger` is detected on such a workflow, a **Remove Orphaned Dashboard Trigger** button lets you clean it up.

## Execution history

The **Recent Executions** table lists each execution's ID, status (success / error / running), mode, and start time. Clicking a row opens that execution in n8n in a new tab (when an n8n base URL is available).

For a failed execution, an **Ask AI** button appears on the row (when the AI assistant is configured). It pulls the execution's error detail, sends it to the assistant, and shows an inline analysis with suggested fixes that you can copy.

For the full failure picture across all workflows, including the error webhook feed and grouped/aggregated failures, see [Executions & Errors](errors.md).

## Importing workflows

**Import Workflow** uploads one or more `.json` workflow files into the active instance. Each file is posted to the importer, which strips read-only fields n8n rejects on create and reports per-file success or failure. This is the quick path; for round-trip backup and bulk export/import, see [Import & Export](import-export.md).

## Deleting workflows

- **Delete** (in the detail panel) hard-deletes the selected workflow from n8n after a type-to-confirm dialog. This is irreversible and execution history is lost.
- **Delete archived (N)** appears in the header when the loaded list contains archived workflows. It scans the full workflow list on the server (which may find more than are visible) and hard-deletes every archived one. Partial failures are reported and do not abort the run.
