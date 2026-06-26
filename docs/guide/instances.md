# n8n Instances

An "instance" is one n8n deployment that AgeniusDesk manages, identified by its **URL** plus an **API key**. You can register as many as you like (self-hosted, DigitalOcean, Hostinger, Railway, Render, Hetzner, Coolify, or n8n Cloud). One instance at a time is the **active** instance, and the whole dashboard, Workflows, Executions, Errors, and the rest, targets whichever instance is active.

You manage instances from the **n8n Instances** sidebar view, or from the same panel under **Settings -> Instances** (both render the identical panel, so they stay in lockstep).

## What an instance stores

| Field | Meaning |
|---|---|
| Name | A label you choose (for example `Production`, `Dev`). |
| URL | The address the dashboard's backend uses to reach n8n's REST API. |
| API key | An n8n API key, stored encrypted (or as a `$NAME` secret reference). |
| Color | An optional dot color used to identify the instance. |
| login_url | A browser-reachable URL, set automatically when the stored URL is only reachable from inside the dashboard container (see the Docker rewrite below). |

The list endpoint never returns the raw key. It returns either the `$NAME` reference (if you used one) or a `...last4` hint.

## Adding an instance

1. Click **+ Add Instance** in the n8n Instances panel (top right).
2. Enter a **Name**, the **URL** you use to open n8n in your browser, and an **API key**.
3. Save. The backend tests the connection before storing anything; if it cannot connect, the instance is not saved and you get a specific error (bad key, wrong URL, host unreachable, timeout, and so on).

### Where to get an n8n API key

In n8n, go to **Settings -> n8n API -> Create an API key**, give it a name, and copy the key. n8n only shows the key once. Paste the root n8n URL (not `/api/v1` or another subpath) into the URL field.

If you point an instance at a key that n8n later rejects, AgeniusDesk surfaces an HTTP 401 with a hint to generate a fresh key. An HTTP 403 usually means the URL is behind Cloudflare Access or another auth wall; use the direct internal n8n URL instead of the public browser URL.

### Reference a stored secret instead of pasting a key

You can store the key once in the secrets store and reference it as `$NAME` (for example `$N8N_KEY`) instead of pasting plaintext. The setup wizard and the connect guide do this automatically: they promote a pasted key into the encrypted store and register the instance with the `$REF`. See [Secrets](secrets.md).

## The active instance and switching

Each instance row has a colored **dot** on the left. The dot of the active instance is just shown; clicking any other instance's dot makes it active. The active instance is also tagged with an **ACTIVE** pill.

Switching the active instance re-points the entire dashboard. The Workflows list, executions, errors, exports, and every other n8n-backed view immediately reflect the newly-active instance. There is no separate "select instance" control on each page; the active instance is the single source of truth.

## Editing credentials

Use the **Credentials** button on an instance row to update its name, URL, API key, or color. As with adding, the URL goes through the same handling as a new instance.

Other row actions:

| Button | What it does |
|---|---|
| Credentials | Edit the instance's name / URL / API key / color. |
| Sign in to n8n | Shown only when a stored n8n owner login exists for the instance. Opens a modal with the URL, email, and password so you do not have to dig them up by hand. Fetched on demand so plaintext is never shipped in the list response. |
| Update | Update the managed n8n container in place (see below). Shown only when a matching container is found. |
| Remove | Removes the instance from AgeniusDesk. This does not touch the n8n server itself. |

## The localhost rewrite (when the dashboard runs in Docker)

When AgeniusDesk itself runs inside a Docker container, `localhost` means the container, not the host where n8n is published. So if you enter `http://localhost:5678`, the backend transparently rewrites the stored URL to `http://host.docker.internal:5678`, which Docker routes back to the host. The original `localhost` URL is kept as the browser-facing `login_url` so links you click still open correctly.

This rewrite happens automatically on both add and edit, only when the dashboard is containerized and only for localhost-family hostnames (`localhost`, `127.0.0.1`, `::1`, `0.0.0.0`). On Linux this relies on the compose file mapping `host.docker.internal` to the host gateway. If a connection still fails, prefer the host's **LAN IP** over `localhost`.

## Updating managed n8n containers in place

If an instance is backed by an n8n container that AgeniusDesk can see (matched by URL/host and port, restricted to containers that look like n8n), the row shows an **Update** button. It stops the container, pulls the latest n8n image, and restarts it, with live progress streamed into the row. There is brief downtime during the update.

If the row shows **Not auto-updateable** instead, no managed n8n container matched the instance's URL. When the n8n container runs on the same host as the dashboard but is not matched, set `AGD_HOST_ALIASES` to that host's LAN IP or hostname (the host shown in the instance URL) and recreate the dashboard to enable one-click updates.
