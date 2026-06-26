# Admin & Users

The **Admin** view manages who can use the dashboard, who can use the n8n instance behind it, and a handful of system actions. It has three tabs: **n8n Instance Users**, **Dashboard Users**, and **System**. Dashboard accounts carry a role (viewer, operator, admin) that gates what they can do in AgeniusDesk itself; n8n users are separate accounts that live inside the active n8n instance. The full identity model (login sessions, edge auth, admin token, role ranking) is documented in [Authentication & RBAC](../architecture/auth.md). The n8n credentials mirror referenced below lives in the [Secrets](secrets.md) view, not in Admin.

> All `/api/admin/*` routes require an admin-level identity (the router attaches `require_role("admin")`). When login is disabled on an open install, the gate is a no-op.

---

## Dashboard roles

Dashboard users authenticate against AgeniusDesk and are assigned one of three roles. Roles are ranked `viewer < operator < admin`; a route requiring a given role accepts that role or higher.

| Role | Rank | Can do |
|---|---|---|
| Viewer | 1 | Read-only access. View workflows, errors, executions, and other read endpoints. |
| Operator | 2 | Everything a viewer can, plus trigger workflows and other operator-gated actions. |
| Admin | 3 | Full access, including this Admin view, the secrets store, config reset, and user management. |

An edge-authenticated identity (a trusted reverse proxy such as Cloudflare Access) and the break-glass admin token both resolve to **admin**. See [Authentication & RBAC](../architecture/auth.md) for precedence and the `AGD_*` environment flags that control enforcement.

> If no dashboard users exist, the dashboard is open access. Add at least one admin user to lock it down.

---

## n8n Instance Users tab

This tab manages user accounts inside the **active** n8n instance over n8n's own API. It is the default tab.

### View users

The **Users on Active Instance** card lists each n8n user with name, email, role pill (`admin` or `member`), and a status pill (`Active` or `Pending`). Data comes from `GET /api/n8n/users`.

### Invite a user

1. Click **Invite User**.
2. Enter the user's **Email**.
3. Choose a **Role**:

   | n8n role | Value | Meaning |
   |---|---|---|
   | Member | `global:member` | Can view and run workflows. |
   | Admin | `global:admin` | Full access to the n8n instance. |

4. Click **Send Invite**. AgeniusDesk calls `POST /api/n8n/users/invite`; n8n emails the invitee to set up their account.

### Remove a user

1. Click **Remove** on the user's row and confirm.
2. AgeniusDesk calls `DELETE /api/n8n/users/{id}`. The user's workflows in n8n may need to be transferred (n8n supports a transfer target on delete).

---

## Dashboard Users tab

This tab manages AgeniusDesk's own login accounts (stored in `data/users.json`, passwords hashed).

### View users

The **Dashboard Access** card lists each account with username, display name, and a role pill. Empty list means the dashboard is open access.

### Add a user

1. Click **Add User**.
2. Fill the form:

   | Field | Notes |
   |---|---|
   | Username | Required. Letters, numbers, hyphens, underscores only. |
   | Display Name | Optional. |
   | Role | `viewer`, `operator`, or `admin` (see the role table above). |
   | Password | Required. Minimum length is enforced server-side (`agd_password_min_length`, 6 by default). |

3. Click **Create User**. AgeniusDesk calls `POST /api/admin/users`. Duplicate usernames return a 409; a too-short password or an invalid role returns a 400.

### Remove a user

Click **Remove** on the row and confirm. AgeniusDesk calls `DELETE /api/admin/users/{username}`.

---

## System tab

Two cards: read-only system info and a danger zone.

### System Info

Pulled from `GET /api/status` and `GET /api/n8n/instances`:

| Field | Source |
|---|---|
| Version | App version. |
| Instances | Count of configured n8n instances. |
| Active Instance | Name of the active instance, or `None`. |
| n8n URL | Active instance URL, or `Not configured`. |
| WebSocket Clients | Currently connected dashboard clients. |
| Theme | Active theme id (see [Themes & Music](themes-music.md)). |
| Configured | Whether setup is complete. |

### Danger Zone

| Action | Effect | Endpoint |
|---|---|---|
| Clear Error History | Deletes all stored errors from the database. Cannot be undone. | `DELETE /api/errors` |
| Reset Config | Wipes dashboard configuration; you must reconnect every n8n instance. Reloads the page after. | `POST /api/admin/reset` |

Both prompt for confirmation first.

> Environment variables: AgeniusDesk exposes a names-only view of relevant env vars (those prefixed `N8N_`, `FLOW_DASHBOARD_`, `QDRANT_`, `LLM_`, `OLLAMA_`) at `GET /api/admin/env`. It reports each variable's name and whether it is set, never its value. This view is not surfaced as a card in the current System tab UI but the endpoint is available to admins.

> License activation: AgeniusDesk CE is the source-available Community Edition and has no in-app license tier or activation step. Any license/tier flows you may have seen in screenshots of other editions are not present here.

---

## n8n credentials mirror

AgeniusDesk can push a stored secret into the active (or a chosen) n8n instance as a native n8n **credential**, so a workflow can use it without you re-entering the value. This is wired into the [Secrets](secrets.md) view (a "Sync to n8n" panel), not the Admin view. It is documented here because it bridges dashboard secrets and n8n users.

How it works:

1. The secret you want to mirror must already exist in the secrets store as `$NAME` (or a compound `$NAME.field`). See [Secrets](secrets.md).
2. The mirror panel lists the credential types the target instance actually supports, fetched live from n8n (`GET /api/n8n-credentials/{instance_id}/mappings`, schema-cached for 5 minutes).
3. For each secret, pick a credential type (or skip it) and run the sync (`POST /api/n8n-credentials/{instance_id}/mirror`). One bad item does not abort the batch; you get a per-item `ok` / `skipped` / `error` result.
4. Already-synced state is shown via `GET /api/n8n-credentials/{instance_id}/mapped`. Re-mirroring deletes the prior n8n credential first, so you do not accumulate duplicates.
5. Unlinking (`DELETE /api/n8n-credentials/{instance_id}/{secret_name}`) deletes the n8n credential best-effort and forgets the mapping.

Scope rule: a secret is only mirrorable to an instance it is allowed on. Unscoped secrets are allowed everywhere; scoped secrets reject instances not in their "Applies To" list. Set scope from the [Secrets](secrets.md) view.
