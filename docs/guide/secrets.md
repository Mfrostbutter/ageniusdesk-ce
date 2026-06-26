# Secrets

The Secrets view is AgeniusDesk's local encrypted credential store. You add named secrets once, reference them anywhere as `$NAME` (or `$NAME.field` for typed multi-field secrets), and the raw value never leaves the server: it is decrypted only at the moment a backend call needs it. Values are encrypted at rest with Fernet in `data/secrets.json`. The view is a single pane rendered by `frontend/js/views/secrets.js`; the API lives in `backend/modules/admin/router.py` and the crypto/resolution in `backend/config.py`. See [Data Model](../architecture/data-model.md) for storage layout and resolution order.

There is no Infisical or external-vault tab in this build; the local store is the only backend on this view.

---

## How it works

- **Encryption at rest:** `encrypt_value()` wraps each value as `fernet:<token>` (AES-128-CBC + HMAC-SHA256). The Fernet key is derived from `SECRET_KEY`, which is loaded from the `SECRET_KEY` env var, else read from / generated into `data/.secret_key`. Losing that key makes encrypted values unrecoverable, so back it up with the `data/` directory.
- **Reference, never inline:** in any API-key field (n8n instances, LLM providers, MCP tokens, knowledge source key fields) you type `$NAME` instead of the secret. At runtime `decrypt_value("$NAME")` resolves it.
- **Resolution order:** for a bare `$NAME`, the process environment wins if set, then `data/secrets.json`. For a dotted `$NAME.field` the secrets store is authoritative (env vars can hold only one string). Full detail in [Data Model](../architecture/data-model.md).
- **Server-side only:** the list endpoint returns names and masked hints, never raw values. Decryption happens inside the backend when a call is made.

---

## Secret kinds

| Kind | Shape | Reference | Stored as |
|---|---|---|---|
| Simple (string) | One value: API key, bearer token, PAT | `$NAME` | Encrypted string |
| Compound (typed) | Multiple labeled fields under one name | `$NAME.fieldName` | `{type, fields: {name: encrypted}}` |

Compound secrets use templates from `backend/modules/admin/secret_templates.py`. Built-in types:

| Type | Fields |
|---|---|
| `api_key` | value (this is the simple/string case) |
| `oauth2_client` | clientId, clientSecret |
| `connectwise_manage` | siteUrl, companyId, publicKey, privateKey, clientId |
| `aws` | accessKeyId, secretAccessKey, region |
| `azure_ad` | tenantId, clientId, clientSecret |
| `custom` | freeform key/value rows you add yourself |

The type picker is populated from `GET /api/admin/secret-templates`.

---

## Store fields shown per secret

The **Stored Secrets** list (`GET /api/admin/secrets`) shows, per row:

| Element | Meaning |
|---|---|
| `$NAME` | The reference to copy and paste into key fields |
| Type badge | For compound secrets, the template label (e.g. AWS) |
| Hint | Masked preview (`abcd...xyz`), never the full value |
| Field count / Expand `▾` | Compound only: number of fields; expand to see each `$NAME.field` |
| Copy | Copies `$NAME` to the clipboard |
| Sync to n8n | Mirror this secret into an n8n instance (only shown when mappings exist) |
| Remove | Deletes the secret |

---

## Add a simple secret

1. In the **Add Secret** card, enter a **Name** (letters, numbers, underscores; it is upper-cased and spaces become `_`). This becomes `$NAME`.
2. Leave **Type** as **API Key / Token**.
3. Paste the value into **Value** (a masked password input).
4. Click **Save Secret** (`POST /api/admin/secrets` with `{name, value}`).

The value is encrypted into `data/secrets.json` and also set on the current process environment so `$NAME` resolves immediately. Use it anywhere a key is asked for:

```text
$N8N_PROD_KEY
```

## Add a compound (typed) secret

1. Enter a **Name**.
2. Pick a **Type** (e.g. **AWS**, **OAuth2 Client**, **ConnectWise Manage**). The form renders the template's labeled fields; secret fields are masked, non-secret fields (IDs, URLs, regions) are plain text and may carry a default.
3. For **Custom**, click **+ Add field** to add `key`/`value` rows.
4. Fill at least one field and click **Save Secret** (`POST /api/admin/secrets` with `{name, type, fields}`).

Each field is encrypted separately. Reference an individual field with the dotted form:

```text
$AWS_MAIN.accessKeyId
$AWS_MAIN.secretAccessKey
```

A bare `$AWS_MAIN` on a compound secret resolves to a JSON object of its fields; use the dotted form in string contexts.

---

## Reference and copy

- Click **Copy** on a row to put `$NAME` on the clipboard.
- Expand a compound secret (`▾`) to see and copy each `$NAME.fieldName`.
- Anywhere a field accepts a `$reference`, the [Models key picker](ai-assistant.md) and similar dropdowns are fed by `GET /api/admin/secrets/refs`. That endpoint returns one ref per simple secret and one ref per field of each compound secret, each with a masked hint, so you can pick a stored secret instead of pasting a raw key.

---

## Promote a raw value

`POST /api/admin/secrets/promote` takes a raw value and moves it into the store, returning a generated `$NAME`. It is idempotent: passing an existing `$NAME` returns it unchanged, and an identical raw value already stored under the same prefix is reused rather than duplicated. This backs flows that capture a key inline and want to store it without a manual round trip.

---

## Per-secret instance scopes

Each secret can carry an instance scope (`secret_scope.json`, keyed by secret name, value a list of n8n instance IDs; empty/missing means all instances). The API is `GET`/`PUT /api/admin/secrets/{name}/scope`.

**Important boundary:** scopes are consulted **only** by the n8n credential mirror (`backend/modules/n8n_credentials/`). General secret resolution (`_resolve_secret_ref` in `config.py`) ignores scopes entirely, so any module that calls `decrypt_value("$NAME")` resolves the secret regardless of scope. Do not treat scopes as a security boundary; they gate which instances a secret may be mirrored into, nothing more. The scope editor UI is hidden in this build for that reason; the backend endpoint remains for the mirror.

---

## Sync a secret into an n8n instance

When at least one connected n8n instance exposes credential mappings, each secret row gains a **Sync to n8n** button.

1. Click **Sync to n8n** on the secret to open its sync panel.
2. The panel lists the instances in the secret's scope (all instances if unscoped). Per instance, pick the n8n **credential type** from the dropdown. The dashboard suggests a type when the secret name matches a known pattern.
3. Click **Mirror** (`POST /api/n8n-credentials/{instanceId}/mirror`). On success the status shows the created credential name and the button becomes **Re-mirror**.
4. **Unlink** deletes the mirrored credential in n8n and forgets the mapping (`DELETE /api/n8n-credentials/{instanceId}/{secretName}`).

Compound templates declare which n8n credential types they can fill (the `n8n_types` list in `secret_templates.py`), e.g. `aws` fills `aws`/`s3`, `oauth2_client` fills the Google/Slack/Microsoft OAuth2 types. The mapper checks that list before mirroring.

---

## Delete a secret

Click **Remove** on a row (`DELETE /api/admin/secrets/{name}`). This deletes the encrypted entry, clears any instance scope, and unsets the process env var. Anything referencing `$NAME` stops resolving, so check usage first.
