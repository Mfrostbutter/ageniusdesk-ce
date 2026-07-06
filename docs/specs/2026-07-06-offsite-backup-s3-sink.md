# Spec: Offsite Backup Destinations (S3-compatible sink)

Status: Draft
Date: 2026-07-06
Owner: Michael Frostbutter
Scope: AgeniusDesk Community Edition
Release gate: no (additive to the shipped scheduled-backups milestone)
Decision on record: the scheduled-backup job already produces a local snapshot;
offsite is a **sink step after the local write**, not a rewrite. The local copy
stays authoritative and an offsite push failing must never lose it.

## 1. Goal

Let an operator push each workflow snapshot to offsite storage so a backup
survives loss of the host or its Docker volume. Ship **one** integration that
covers the most destinations: an **S3-compatible object store**. A single
protocol + `endpoint_url` reaches AWS S3, Cloudflare R2, Backblaze B2, Wasabi,
and self-hosted MinIO, with plain key/secret auth and no OAuth flow. Google
Drive and rclone are explicit non-goals for v1 (Section 6).

## 2. What exists today (load-bearing; do not re-discover)

- `backend/scheduler.py` fires the `workflow-backup` job on its interval.
- `backend/modules/backups/service.py::run_backup()` iterates every instance,
  calls `n8n_client.export_all_workflows_for(inst)`, writes
  `data/backups/<instance_id>/<stamp>.json` via `_write_snapshot`, then
  `_prune(inst_id, retention)`. It returns a per-instance summary
  (`{id, name, ok, count, error, file}`).
- Settings live in `config.json` under `backups`
  (`enabled`, `interval_hours`, `retention`, `active_only`), read live each run.
- Secret refs: `decrypt_value()` resolves `$VAR` against env then
  `data/secrets.json` (env wins). Credentials must go through this, never
  plaintext in `config.json`.
- The snapshot JSON is workflow **definitions**. n8n's API export does not carry
  credential secrets (credentials are a separate resource), but nodes can hold
  inline config, so the file is sensitive.

The sink hooks in **after `_write_snapshot` succeeds**, inside the per-instance
loop, so a snapshot uploads as soon as it is on disk.

## 3. Design

### 3.1 Destinations model

v1 keeps exactly two: `local` (always on, unchanged) plus one optional `remote`.
Extend the `backups` config with a nested object:

```json
"backups": {
  "enabled": true, "interval_hours": 24, "retention": 14, "active_only": false,
  "remote": {
    "enabled": false,
    "provider": "s3",
    "bucket": "agd-backups",
    "prefix": "ageniusdesk/",
    "endpoint_url": "",              // blank = AWS; set for R2/B2/MinIO/Wasabi
    "region": "auto",
    "access_key_id_ref": "$AGD_S3_ACCESS_KEY_ID",
    "secret_access_key_ref": "$AGD_S3_SECRET_ACCESS_KEY",
    "mirror_retention": true,        // apply the same keep-N prune offsite
    "encrypt": false                 // Fernet-encrypt bytes before upload
  }
}
```

Only the two `_ref` fields touch credentials; they are stored as `$VAR` refs and
resolved with `decrypt_value()` at run time. `save_settings()` validates
`provider in {"s3"}`, non-empty bucket when `remote.enabled`, and leaves the
refs opaque.

### 3.2 Upload step

After `_write_snapshot` returns `path`, if `remote.enabled`:

- Key = `f"{prefix}{instance_id}/{path.name}"` (prefix normalized to end in `/`
  or empty).
- Body = the snapshot bytes, optionally Fernet-encrypted (Section 3.4).
- `put_object(Bucket, Key, Body)`.

Record the outcome on the existing per-instance summary entry
(`remote_ok: bool`, `remote_error: str`), so the UI shows local and offsite state
independently. **The upload is wrapped in its own try/except**: a failed push
leaves `ok: true` (local succeeded) with `remote_ok: false`. One instance's
upload failure does not abort the others, matching the existing isolation.

### 3.3 S3 client + optional dependency

S3 support is an **opt-in extra** so the default image stays lean (mirrors the
`langgraph` extra pattern): `pip install '.[s3]'` /
`AGD_EXTRAS="...,s3"`. Candidate libs, decide in phase 1:

- **aioboto3** (async, pulls botocore/boto3): native async, first-class
  `endpoint_url`, ubiquitous. Heaviest.
- **minio-py** (sync, small): tidy API, S3-compatible, run in a thread via
  `asyncio.to_thread`. Lighter.
- httpx + hand-rolled SigV4: no new dep but error-prone; rejected unless the
  image-size cost of the above is unacceptable.

Lean toward **aioboto3** for endpoint/region breadth; revisit if image size is a
concern. When `remote.enabled` but the extra is absent, the run logs a clear
"install the s3 extra to enable offsite backup" and keeps the local snapshot
(never a hard failure).

### 3.4 Optional encryption before upload

When `remote.encrypt` is true, Fernet-encrypt the snapshot bytes with the app
`SECRET_KEY` before `put_object` and suffix the key `.json.enc`. Restoring an
encrypted offsite copy then requires the **same `SECRET_KEY`**; document this
loudly (lose the key, lose the offsite backup). Local snapshots stay plaintext
(they are already inside the trust boundary). Default off.

### 3.5 Remote retention

When `mirror_retention` is true, after a successful upload `list_objects_v2`
under `{prefix}{instance_id}/`, sort by key (the stamp sorts lexically), and
delete all but the newest `retention`. When false, upload only and let the
bucket's own lifecycle policy manage age (documented as the recommended path for
large fleets). Prune failures are logged, non-fatal.

### 3.6 Security

- Credentials come only from the secret store via `$VAR` refs; never persisted
  in `config.json` and never returned by `GET /settings` (return the ref name,
  not the resolved value).
- **Do not** run `endpoint_url` through the standard `assert_safe_probe_url`
  SSRF guard: a self-hosted MinIO on the LAN (RFC1918) is a legitimate target,
  so blocking private ranges would break the main self-hoster use case. Block
  only the cloud-metadata address (`169.254.169.254`) and require
  `https`/`http` scheme. Note this deviation explicitly in the code.
- `AGD_TLS_VERIFY` is honored on the S3 client, like every other outbound path.

### 3.7 API + UI

- Fold remote config into the existing `PUT /api/backups/settings` (accepts the
  `remote` object) and surface it on `GET /settings` with refs unresolved.
- Add `POST /api/backups/test-remote` (operator): resolve creds, `put_object`
  then `delete_object` of a tiny `___agd-probe` key, return
  `{ok, error, latency_ms}`. This is the "Test connection" button.
- Extend the Scheduled Backups card with a **Remote destination** subsection
  (provider select, bucket, prefix, endpoint, region, two key-ref inputs,
  encrypt + mirror-retention toggles, Test connection). Snapshot rows gain a
  small offsite badge (uploaded / failed) from the run summary.

## 4. Testing

- Mock the S3 client (no network): assert `put_object` called with the expected
  bucket/key/body for each instance; a local write always happens even when the
  upload raises; `remote_ok`/`remote_error` recorded correctly.
- Retention mirror: seed a fake object listing, assert only the oldest beyond N
  are deleted.
- Encryption: `encrypt=true` uploads Fernet ciphertext under a `.enc` key that
  round-trips with the app key.
- Missing extra: `remote.enabled` with the lib absent logs and degrades, local
  snapshot still written.
- `test-remote` happy path and auth-failure path.
- Settings: refs stored opaque, resolved values never leak through `GET`.

## 5. Implementation phases

1. Sink abstraction + S3 uploader behind the `s3` extra; wire the post-write
   upload into `run_backup`; per-instance `remote_ok`/`remote_error` in the
   summary; settings schema + validation.
2. `test-remote` endpoint + UI Remote destination subsection + offsite badges.
3. Remote retention mirror.
4. Optional Fernet-before-upload.
5. Docs: extend `docs/guide/import-export.md` (offsite section, key-management
   warning for `encrypt`), CHANGELOG, ROADMAP.

## 6. Non-goals

- **Google Drive / OAuth** destinations. Consumer-oriented, token-refresh
  overhead, single destination. Revisit after S3 if there is demand.
- **rclone** shell-out (70+ backends incl. Drive/SFTP/WebDAV). Powerful but adds
  a binary dependency and cross-platform packaging; a later "advanced" option,
  not v1.
- **Multiple simultaneous remotes.** One `remote` in v1; a destinations array is
  a later change if needed.
- **Restore-from-remote UI.** v1 is push-only; to restore, download the object
  (or pull via the console) and use the existing restore drop zone. A
  browse/restore-from-remote surface can follow.
- Backing up **credentials or the app's own DB/secrets**; this snapshots n8n
  workflow definitions only, same as the local job.

## 7. Relationship to the roadmap

Follows the shipped **Scheduled backups** Near-Term item; the offsite sink is the
natural durability layer on top. Core built-in with direct outbound HTTP, so it
does **not** depend on the `http.request` bridge (that gates credential-holding
community modules under isolation, a different concern).

## 8. Open questions

- S3 library: aioboto3 vs minio-py (image-size vs async-native tradeoff).
- Mirror retention on by default, or upload-only and lean on bucket lifecycle?
- Is restore-from-remote wanted in v1, or is push-only acceptable to start?
