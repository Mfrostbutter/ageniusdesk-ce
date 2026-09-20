# n8n capability record

Per-instance detection of what the connected n8n can do natively, so each module can take the Community path (AgeniusDesk supplies the feature) or the licensed path (AgeniusDesk drives n8n's own feature through the public API). Read-only against n8n; nothing is written to the instance.

Code: `backend/n8n_capabilities.py` (record builder), `n8n_proxy/client.probe_get` (one GET against a specific instance, per-instance TLS, never raises), routes in `n8n_proxy/router.py`.

## IN

- A stored instance record (`data/config.json`): `url`, `api_key`, optional `tls_verify`.
- Triggers: `POST /api/n8n/instances/{id}/capabilities/refresh` (operator), a successful add-instance, a successful `rotate-key`. The two automatic triggers are best-effort and never fail the calling route.

## READS

One GET each, short timeout (`PROBE_TIMEOUT`, 8 s), every status tolerated.

| Signal | Request | Auth |
|---|---|---|
| Edition, version, SSO state | `GET {url}/rest/settings` | none |
| Key scopes | `GET /api/v1/discover` | API key |
| Endpoint presence | `/api/v1/projects?limit=1`, `/api/v1/variables?limit=1`, `/api/v1/insights/summary`, `/api/v1/roles`, `/api/v1/settings/log-streaming/destinations`, `/api/v1/settings/otel`, `/api/v1/settings/security-policy`, `/api/v1/source-control/pull`, `/api/v1/data-tables?limit=1` | API key |
| Workflow history | `/api/v1/workflows?limit=1`, then `/api/v1/workflows/{firstId}/history?limit=1` | API key |

`licensed` is true when any of `data.enterprise.{saml,ldap,oidc}` is true in the unauthenticated settings payload. `version` comes from `versionCli` in that same payload when the build exposes it; n8n 2.37 omits it unauthenticated, so `version` stays `null` there until the session transport lands. A feature is present when its endpoint answered 200; `source_control` counts a 405 as present (the route is POST-only). The body of `/api/v1/settings/otel` is dropped unparsed: it echoes `exporterHeaders`, which carries a bearer. Only the status code is kept.

## OUT

Persisted on the instance as `capabilities` and returned by `GET /api/n8n/instances` (per row), `GET /api/n8n/instances/{id}/capabilities` (viewer), and the refresh route:

```json
{
  "probed_at": "2026-09-19T20:11:03.120000+00:00",
  "version": "2.37.7",
  "licensed": true,
  "sso": {"saml": true, "ldap": true, "oidc": true},
  "key_scopes": ["workflow:list", "..."],
  "endpoints": {"projects": 200, "variables": 200, "insights_summary": 200, "roles": 403,
                "log_streaming": 403, "settings_otel": 403, "security_policy": 403,
                "source_control": 405, "data_tables": 200, "workflow_history": 200},
  "features": {"projects": true, "variables": true, "insights": true, "roles": false,
               "log_streaming": false, "otel_settings": false, "security_policy": false,
               "source_control": true, "data_tables": true, "workflow_history": true},
  "notes": ["key minted before licensing: scopes frozen; re-mint to reach enterprise endpoints (log_streaming, roles, security_policy, settings_otel)"]
}
```

`endpoints` holds the raw status per probe (`null` = transport failure or not probed). `features` is the boolean a module should read. `capabilities` is `null` on an instance that has never been probed; the Instances panel shows it as Unprobed with a Refresh action.

## ON FAILURE

- `/rest/settings` unreachable or non-200: `licensed` is false, `version` is null, and a note says the edition is unknown and treated as Community. Endpoint probes still run.
- `/api/v1/discover` non-200 (older n8n, or a key without the scope): `key_scopes` is `[]`, no note.
- Licensed instance with a 403 on an endpoint the licence should unlock: the frozen-key note lists the endpoints. n8n API keys freeze their scope list at mint time, so a key minted before the licence never gains the new scopes; the fix is a new key, then Rotate.
- No workflows on the instance: history is not probed, `workflow_history` is false, a note says why.
- Transport failure on any single probe: that endpoint records `null`; the rest of the record is still built and stored.
- Refresh on an unknown instance id: 404. The automatic refresh after add or rotate logs a warning and returns `capabilities: null` in the route's response without failing it.

## Consumers

Nothing reads the record yet beyond the Instances chip. Insights, Version History, Promotion and the event ingest are the planned readers (see ROADMAP, "Licensed n8n: the control-plane rule").
