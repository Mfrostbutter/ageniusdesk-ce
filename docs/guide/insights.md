# Insights

The Insights view is an execution analytics roll-up for the active n8n instance. It derives everything from execution data (pulled from n8n's executions API plus the local error log) and presents success rates, an execution timeline, and the busiest and most error-prone workflows. There are no charts library dependencies; the timeline is a stacked bar strip rendered inline.

Related: [Executions & Errors](errors.md) is the live feed that the local error log here is built from. Data shapes are in [../architecture/data-model.md](../architecture/data-model.md).

## How it is computed

The whole view loads in one round-trip from `GET /api/insights?range=<range>`. The aggregator pulls executions for the time window from n8n, merges in counts from the local error log, and shapes them into the summary, timeline, and top lists. It does not read any pre-computed table; the numbers reflect actual executions in the selected window.

The result is cached server-side for 5 minutes per `(instance_id, range)`. The card header shows whether the data is `fresh` or `cached Ns ago`.

### Scope and time range

A **range selector** in the header offers Last 24 hours, Last 7 days, and Last 30 days. The choice persists per browser session. The bucket granularity follows the range: 24h buckets by hour, longer ranges bucket by day.

Insights is scoped to the active instance. The endpoint accepts `instance_id` with the same convention as the errors view (`active` -> current instance, `all` -> every instance, or a concrete id).

The **Refresh** button drops the server cache for the current range and reloads, so you get a fresh pull from n8n on demand (`POST /api/insights/refresh`).

## Pagination cap

To stop a single API call from running away on a chatty instance, n8n pagination is capped per range (4 pages for 24h, 16 for 7d, 40 for 30d, at 250 rows per page). If the window hits that cap, a dashed banner warns that older runs in the window may be missing. The view prefers truncated-but-fresh over complete-but-stale.

## Summary tiles

Four tiles across the top:

| Tile | Value | Subtext |
|---|---|---|
| **Executions** | Total executions in the window | `ok · err · running` breakdown |
| **Success rate** | Percentage of successful executions, color-coded | `success of total` |
| **Errors** | Count of failed executions | How many are also in the local error log |
| **Avg duration** | Mean execution duration | measured per execution |

Success rate color thresholds: green at 95% or above, amber from 80% to 95%, red below 80%.

## Timeline

A stacked bar strip, one bar per time bucket (hourly or daily depending on range). Each bar stacks three segments:

- green: successful executions
- red: failed executions
- amber: running executions

Hover a bar to see the bucket timestamp and the exact `ok / err / running` counts. The card title notes whether buckets are hourly or daily.

## Top workflow lists

Three tables surface where activity and failures concentrate. Workflow names link into the Workflows view for that workflow.

| List | Sort | Columns |
|---|---|---|
| **Top by volume** | Most-run workflows | Workflow, run count, success rate (color-coded) |
| **Top by execution errors** | Most failures reported by n8n | Workflow, errors, total runs |
| **Top by local error log** | Most failures in AgeniusDesk's stored error feed | Workflow, errors, last seen |

The two error lists can differ: "execution errors" counts failures n8n reports for the window, while "local error log" counts what was actually captured by the error handler and stored in AgeniusDesk. A gap between them usually means the global error handler is not installed or not selected as the instance Error Workflow. See [Executions & Errors](errors.md#installing-the-global-error-handler).
