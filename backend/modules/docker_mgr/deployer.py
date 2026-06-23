"""Async deployment engine — pull image, create volumes/container, start.

Each deploy gets an asyncio.Queue where step events are pushed as the
deploy progresses. The SSE endpoint reads from this queue and streams
to the browser. The queue sentinel (None) signals stream end.

deploy() is called as an asyncio background task. Errors are caught and
pushed as {"event": "error"} events so the browser always gets a clean
stream close.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

import aiodocker

from . import bundle as bundle_mod
from . import client as docker_client
from . import template_state
from . import templates as tmpl

logger = logging.getLogger(__name__)

# deploy_id → asyncio.Queue of event dicts.
_queues: dict[str, asyncio.Queue] = {}


def new_deploy_id() -> str:
    return uuid.uuid4().hex[:16]


def register(deploy_id: str) -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue()
    _queues[deploy_id] = q
    return q


def get_queue(deploy_id: str) -> asyncio.Queue | None:
    return _queues.get(deploy_id)


def release(deploy_id: str) -> None:
    _queues.pop(deploy_id, None)


async def _push(q: asyncio.Queue, event: str, **kwargs) -> None:
    await q.put({"event": event, **kwargs})


async def _migrate_n8n_key_from_existing_container(field_values: dict) -> None:
    """If an n8n container with this instance name already exists and
    template_state has no persisted key, scrape its N8N_ENCRYPTION_KEY env
    var and persist it. Lets redeploys of pre-fix containers keep their data.
    """
    raw_name = (field_values.get("instance_name") or "").strip().replace(" ", "-").lower()
    if not raw_name:
        return
    if template_state.load("n8n", raw_name).get("encryption_key"):
        return
    container_name = f"agd-{raw_name}"
    try:
        docker = docker_client._get_client()
        c = await docker.containers.get(container_name)
        info = await c.show()
    except Exception:
        return
    env = (info.get("Config") or {}).get("Env") or []
    for entry in env:
        if entry.startswith("N8N_ENCRYPTION_KEY="):
            key = entry.split("=", 1)[1]
            if key:
                template_state.update_field("n8n", raw_name, "encryption_key", key)
                logger.info("Migrated N8N_ENCRYPTION_KEY for instance %s into template_state", raw_name)
            return


async def _read_volume_file(docker, volume_name: str, file_path: str) -> str | None:
    """Read a single file out of a named volume via a throwaway busybox container.

    Returns the file contents, or None if the volume/file is absent or the
    probe fails. Used to recover key material that lives only inside an
    existing data volume (e.g. an orphaned n8n volume whose owning container
    was removed). Best-effort: any failure degrades to None.
    """
    probe_image = "busybox:latest"
    container = None
    try:
        try:
            await docker._query_json(f"volumes/{volume_name}")
        except aiodocker.DockerError:
            return None  # volume doesn't exist — nothing to adopt
        try:
            await docker.images.pull(probe_image)
        except Exception:
            pass  # already present, or offline with a cached copy
        cfg = {
            "Image": probe_image,
            "Cmd": ["cat", f"/vol/{file_path.lstrip('/')}"],
            "HostConfig": {"Binds": [f"{volume_name}:/vol:ro"], "AutoRemove": False},
        }
        container = await docker.containers.create(cfg)
        await container.start()
        await container.wait()
        logs = await container.log(stdout=True, stderr=False)
        text = "".join(logs) if isinstance(logs, list) else (logs or "")
        return text or None
    except Exception as exc:
        logger.debug("volume file probe failed (%s:%s): %s", volume_name, file_path, exc)
        return None
    finally:
        if container is not None:
            try:
                await container.delete(force=True)
            except Exception:
                pass


async def _migrate_n8n_key_from_existing_volume(field_values: dict) -> None:
    """Adopt an n8n encryption key that lives only inside an existing data volume.

    The volume's `config` is n8n's source of truth for the key. When the target
    volume already holds one (e.g. an orphaned volume whose owning container was
    removed), reuse it instead of minting a new key — otherwise n8n crash-loops
    with "Mismatching encryption keys". Persists into template_state so the
    builder picks it up.
    """
    raw_name = (field_values.get("instance_name") or "").strip().replace(" ", "-").lower()
    if not raw_name:
        return
    if template_state.load("n8n", raw_name).get("encryption_key"):
        return
    volume_name = f"agd-n8n-{raw_name}"
    docker = docker_client._get_client()
    raw = await _read_volume_file(docker, volume_name, "config")
    if not raw:
        return
    try:
        key = (json.loads(raw) or {}).get("encryptionKey")
    except (ValueError, TypeError):
        key = None
    if key:
        template_state.update_field("n8n", raw_name, "encryption_key", key)
        logger.info("Adopted N8N_ENCRYPTION_KEY from existing volume %s into template_state", volume_name)


async def recreate(deploy_id: str, config: dict, container_name: str) -> None:
    """Pull the latest image and recreate a container with its current config."""
    q = _queues.get(deploy_id)
    if q is None:
        return

    image = config["Image"]
    docker = docker_client._get_client()

    try:
        await _push(q, "step", message=f"Pulling latest {image}…", detail="This may take a moment.")
        try:
            await docker.images.pull(image)
            await _push(q, "step", message="Image up to date.")
        except Exception as exc:
            await _push(q, "error", message=f"Image pull failed: {exc}")
            return

        await _push(q, "step", message=f"Stopping {container_name}…")
        try:
            stale = await docker.containers.get(container_name)
            stale_info = await stale.show()
            if (stale_info.get("State") or {}).get("Status") == "running":
                await stale.stop()
            await stale.delete()
            await _push(q, "step", message="Old container removed.")
        except aiodocker.DockerError:
            await _push(q, "step", message="No existing container to remove.")

        await _push(q, "step", message=f"Creating {container_name}…")
        try:
            new_c = await docker.containers.create(config, name=container_name)
            await new_c.start()
            container_id = new_c._id
        except Exception as exc:
            await _push(q, "error", message=f"Container create failed: {exc}")
            return

        await _push(q, "done",
                    container_id=container_id[:12],
                    container_name=container_name,
                    url="",
                    template_id="")

    except Exception as exc:
        logger.exception("Recreate failed for deploy %s", deploy_id)
        await _push(q, "error", message=f"Unexpected error: {exc}")
    finally:
        await q.put(None)
        await asyncio.sleep(5)
        release(deploy_id)


async def deploy_bundle(
    deploy_id: str,
    template: "tmpl.Template",
    field_values: dict,
    public_host: str = "localhost",
) -> None:
    """Deploy a multi-container template as a unit.

    Spec: docs/specs/multi-container-templates-2026-05-21.md

    Lifecycle:
      1. Call template.build(field_values) and normalise to list[ContainerSpec].
      2. bundle_mod.mint_shared_secrets() for any auto_secret keys the template
         declares (community JSON) or the builder threads (built-in).
      3. bundle_mod.topological_sort() to fix deploy order.
      4. Create the bundle network (bundle_mod.bundle_network_name).
      5. For each spec in topo order, emit bundle_step then pull / volume /
         create / start with bundle_labels merged into the config.
      6. On mid-bundle failure: do NOT roll back. Emit error with
         {partial:true, bundle_id, started:[...], failed, remaining:[...]}.
      7. On success, persist {template_id, instance_name, fields} under
         template_state namespace "bundle:<template_id>" so recreate_bundle
         can replay later. Emit single done event with containers:[...] and
         primary_url.

    SSE event protocol (see spec §4):
      step          {message, detail}                 — same as today
      bundle_step   {current, total, container_name}  — NEW
      done          {bundle:true, bundle_id, primary_url, containers:[...]}
      error         {message, partial?, bundle_id?, started?, failed?, remaining?}

    Builder agent: implement per spec. Do not change the SSE event shape; the
    frontend branches on `bundle_step` presence and `done.bundle === true`.
    """
    q = _queues.get(deploy_id)
    if q is None:
        logger.error("deploy_bundle called without registered queue: %s", deploy_id)
        return

    template_id = template.id
    raw_instance = (
        field_values.get("instance_name", "").strip().replace(" ", "-").lower()
        or template_id
    )
    bundle_id = bundle_mod.mint_bundle_id(template_id, raw_instance)

    async def step(msg: str, detail: str = "") -> None:
        await _push(q, "step", message=msg, detail=detail)

    async def bundle_step(current: int, total: int, container_name: str) -> None:
        await _push(q, "bundle_step", current=current, total=total, container_name=container_name)

    async def error(msg: str, **kwargs: Any) -> None:
        await _push(q, "error", message=msg, **kwargs)

    try:
        # 1. Mint any auto-secrets the template declares. Persist them so
        #    redeploys keep using the same DB password / encryption key.
        auto_secret_keys = list(getattr(template, "auto_secrets", []) or [])
        if auto_secret_keys:
            field_values = bundle_mod.mint_shared_secrets(
                template_id, raw_instance, field_values, auto_secret_keys
            )

        # 2. Build → normalise → validate → topo sort.
        result = template.build(field_values)
        specs = bundle_mod.normalise_build_result(result, fallback_name=raw_instance)
        bundle_mod.validate_bundle(specs)
        ordered = bundle_mod.topological_sort(specs)
        total = len(ordered)

        docker = docker_client._get_client()

        # 3. Create per-bundle user-defined bridge network so siblings can
        #    resolve each other by container name / alias. The default bridge
        #    has no embedded DNS, so we must opt in.
        network_name = bundle_mod.bundle_network_name(template_id, raw_instance)
        await step(f"Preparing bundle network {network_name}…")
        try:
            await docker._query_json(f"networks/{network_name}")
            await step(f"Network {network_name} already exists — reusing.")
        except aiodocker.DockerError as exc:
            if exc.status == 404:
                await docker._query_json(
                    "networks/create",
                    method="POST",
                    data=json.dumps({"Name": network_name, "Driver": "bridge"}),
                    headers={"Content-Type": "application/json"},
                )
                await step(f"Network {network_name} created.")
            else:
                await error(f"Network create failed: {exc}")
                await q.put(None)
                return

        # 4. Deploy each spec in topo order. On the first failure we stop and
        #    emit a partial-bundle error event without rolling back what's
        #    already running.
        started: list[str] = []
        deployed: list[dict] = []
        primary_url = ""
        primary_id = ""
        primary_name = ""

        for idx, spec in enumerate(ordered, start=1):
            container_name = bundle_mod.member_container_name(template_id, raw_instance, spec.name)
            await bundle_step(idx, total, spec.name)

            try:
                image = spec.config.get("Image")
                if not image:
                    raise RuntimeError(f"Spec '{spec.name}' has no Image in config")

                await step(f"Pulling {image}…")
                await docker.images.pull(image)

                for vol in spec.volumes:
                    try:
                        await docker._query_json(f"volumes/{vol}")
                        await step(f"Volume {vol} already exists — reusing.")
                    except aiodocker.DockerError as ve:
                        if ve.status == 404:
                            await docker._query_json(
                                "volumes/create",
                                method="POST",
                                data=json.dumps({"Name": vol}),
                                headers={"Content-Type": "application/json"},
                            )
                            await step(f"Volume {vol} created.")
                        else:
                            raise

                # Remove any stale container with the same name (idempotent redeploy).
                try:
                    stale = await docker.containers.get(container_name)
                    sinfo = stale._container
                    if sinfo.get("State", {}).get("Running"):
                        await stale.stop()
                    await stale.delete()
                    await step(f"Removed existing container {container_name}.")
                except aiodocker.DockerError:
                    pass  # didn't exist — fine.

                # Merge bundle labels into the spec's Labels.
                config = json.loads(json.dumps(spec.config))  # deep copy
                existing_labels = config.get("Labels") or {}
                config["Labels"] = {
                    **existing_labels,
                    **bundle_mod.bundle_labels(template_id, raw_instance, spec),
                }
                # Attach to the bundle network at create time. Aliases let
                # siblings resolve by spec.name AND by container_name.
                config.setdefault("NetworkingConfig", {})
                config["NetworkingConfig"]["EndpointsConfig"] = {
                    network_name: {"Aliases": list({spec.name, container_name})}
                }

                container = await docker.containers.create(config, name=container_name)
                await container.start()
                container_id_full = container._id
                short_id = container_id_full[:12]
                started.append(spec.name)
                await step(f"Container {container_name} started.")

                member_url = ""
                if spec.role == "primary" and spec.expose_port:
                    member_url = f"http://{public_host}:{spec.expose_port}"
                    primary_url = member_url
                    primary_id = short_id
                    primary_name = container_name

                deployed.append({
                    "name": container_name,
                    "id": short_id,
                    "url": member_url,
                    "role": spec.role,
                    "member": spec.name,
                })

            except Exception as exc:  # noqa: BLE001
                remaining = [s.name for s in ordered[idx:]]
                logger.exception("Bundle deploy failed at %s", spec.name)
                await error(
                    f"{spec.name}: {exc}",
                    partial=True,
                    bundle_id=bundle_id,
                    started=started,
                    failed=spec.name,
                    remaining=remaining,
                )
                await q.put(None)
                return

        # 5. Persist the bundle snapshot for recreate_bundle to replay.
        bundle_mod.save_bundle_snapshot(template_id, raw_instance, field_values)

        # 6. Single bundle-shaped done event. Keep container_id / container_name /
        #    url top-level too so the existing single-container frontend code
        #    path keeps working until it learns to branch on `bundle:true`.
        await _push(
            q,
            "done",
            bundle=True,
            bundle_id=bundle_id,
            template_id=template_id,
            primary_url=primary_url,
            container_id=primary_id,
            container_name=primary_name,
            url=primary_url,
            containers=deployed,
        )

    except Exception as exc:  # noqa: BLE001
        logger.exception("Bundle deploy crashed for %s", deploy_id)
        await _push(q, "error", message=f"Unexpected error: {exc}")
    finally:
        await q.put(None)
        await asyncio.sleep(5)
        release(deploy_id)


async def recreate_bundle(
    deploy_id: str,
    bundle_id: str,
    public_host: str = "localhost",
) -> None:
    """Pull latest images and recreate every member of a bundle in topo order.

    Reads the persisted bundle definition (template_state "bundle:<template_id>"
    keyed by instance_name) to get the original fields, calls template.build()
    again to get fresh specs, then for each spec in topo order: pull, stop +
    delete the existing member, create, start. Volumes are reused.

    Emits the same SSE event protocol as deploy_bundle. The done event omits
    primary_url change (URL is unchanged); UI just refreshes the container list.

    If the persisted definition is missing (deployed before this feature
    landed, or template_state was wiped), emits an error event explaining the
    user must destroy + redeploy.
    """
    q = _queues.get(deploy_id)
    if q is None:
        return

    try:
        template_id, instance_name = bundle_mod.parse_bundle_id(bundle_id)
    except ValueError as exc:
        await _push(q, "error", message=f"Bad bundle_id: {exc}")
        await q.put(None)
        return

    snapshot = bundle_mod.load_bundle_snapshot(template_id, instance_name)
    if not snapshot or "_fields" not in snapshot:
        await _push(
            q,
            "error",
            message=(
                f"No persisted bundle definition for {bundle_id}. "
                "Bundle was deployed before recreate support landed, or "
                "template_state was reset. Destroy and redeploy to fix."
            ),
        )
        await q.put(None)
        return

    template = tmpl.get(template_id)
    if template is None or template.bundle_id is None:
        await _push(q, "error", message=f"Template {template_id} is not bundle-shaped.")
        await q.put(None)
        return

    field_values = dict(snapshot["_fields"])
    # Re-deploying through deploy_bundle handles network reuse, volume reuse,
    # and stale-container teardown idempotently — the recreate path is just
    # "deploy_bundle with the persisted snapshot fields", which intentionally
    # pulls the latest image each time.
    await deploy_bundle(deploy_id, template, field_values, public_host=public_host)


async def deploy(
    deploy_id: str,
    template_id: str,
    field_values: dict,
    public_host: str = "localhost",
) -> None:
    """Run the full deploy lifecycle. Pushes events to the registered queue.

    `public_host` is the hostname the operator's browser should use to reach
    container ports on this Docker host. Resolved by the router from
    AGD_PUBLIC_HOST → request Host header → "localhost".
    """
    q = _queues.get(deploy_id)
    if q is None:
        logger.error("deploy called without registered queue: %s", deploy_id)
        return

    async def step(message: str, detail: str = "") -> None:
        await _push(q, "step", message=message, detail=detail)

    async def done(container_id: str, container_name: str, port: int | None) -> None:
        url = f"http://{public_host}:{port}" if port else ""
        await _push(q, "done", container_id=container_id, container_name=container_name,
                    url=url, template_id=template_id)

    async def error(message: str) -> None:
        await _push(q, "error", message=message)

    template = tmpl.get(template_id)
    if template is None:
        await error(f"Unknown template: {template_id}")
        await q.put(None)
        return

    # Bundle-shaped templates use the multi-container path.
    if template.bundle_id is not None:
        await deploy_bundle(deploy_id, template, field_values, public_host=public_host)
        return

    try:
        # One-time migration for n8n: if an existing container already encodes
        # an N8N_ENCRYPTION_KEY in its env, capture it into template_state
        # BEFORE we rebuild — so the new container reuses it and the existing
        # data volume's encrypted credentials remain readable.
        had_persisted_key = False
        if template_id == "n8n":
            raw_name = (field_values.get("instance_name") or "").strip().replace(" ", "-").lower()
            # Recover an existing encryption key so the deploy never mints a new
            # one over existing data. The volume's own config is authoritative
            # (covers orphaned volumes whose container is gone); fall back to a
            # still-present container's env for volumes with no config yet.
            await _migrate_n8n_key_from_existing_volume(field_values)
            await _migrate_n8n_key_from_existing_container(field_values)
            had_persisted_key = bool(
                raw_name and template_state.load("n8n", raw_name).get("encryption_key")
            )

        container_config, volume_names = template.build(field_values)

        if template_id == "n8n" and had_persisted_key:
            await step(
                "Reusing persisted n8n encryption key.",
                "Avoids 'Mismatching encryption keys' crash on existing data volume.",
            )
        image = container_config["Image"]
        instance_name = (
            field_values.get("instance_name", "").strip().replace(" ", "-").lower()
            or template_id
        )
        container_name = f"agd-{instance_name}"

        # 1. Pull image
        await step(f"Pulling {image}…", "This may take a few minutes on first run.")
        try:
            docker = docker_client._get_client()
            # aiodocker pull: waits for the image pull to complete.
            await docker.images.pull(image)
            await step(f"Image ready: {image}")
        except Exception as exc:
            await error(f"Image pull failed: {exc}")
            await q.put(None)
            return

        # 2. Create volumes
        for vol_name in volume_names:
            await step(f"Creating volume {vol_name}…")
            try:
                # Try to get existing volume first to avoid duplicates.
                await docker._query_json(f"volumes/{vol_name}")
                await step(f"Volume {vol_name} already exists — reusing.")
            except aiodocker.DockerError as exc:
                if exc.status == 404:
                    await docker._query_json(
                        "volumes/create",
                        method="POST",
                        data=json.dumps({"Name": vol_name}),
                        headers={"Content-Type": "application/json"},
                    )
                    await step(f"Volume {vol_name} created.")
                else:
                    await error(f"Volume creation failed: {exc}")
                    await q.put(None)
                    return

        # 3. Remove any stale container with the same name.
        await step(f"Preparing container {container_name}…")
        try:
            stale = await docker.containers.get(container_name)
            info = stale._container
            if info.get("State", {}).get("Running"):
                await stale.stop()
            await stale.delete()
            await step(f"Removed existing container {container_name}.")
        except aiodocker.DockerError:
            pass  # container didn't exist — normal first-run path

        # 4. Create container.
        container_config["name"] = container_name  # aiodocker passes 'name' as param
        try:
            container = await docker.containers.create(container_config, name=container_name)
            container_id = container._id
            await step(f"Container {container_name} created.")
        except Exception as exc:
            await error(f"Container creation failed: {exc}")
            await q.put(None)
            return

        # 5. Start container.
        await step(f"Starting {container_name}…")
        try:
            await container.start()
        except Exception as exc:
            await error(f"Container start failed: {exc}")
            await q.put(None)
            return

        port = field_values.get("port")
        await done(container_id[:12], container_name, int(port) if port else None)

        # 6. Post-deploy hooks (non-blocking — container is already up).
        hook_names = getattr(template, "post_deploy_hooks", [])
        if hook_names:
            from backend.modules.docker_mgr.post_deploy_hooks import run_hooks  # noqa: PLC0415

            await step(f"Running {len(hook_names)} post-deploy hook(s)…")
            try:
                hooks_result = await run_hooks(container_id, hook_names)
            except Exception as exc:  # noqa: BLE001
                logger.exception("Post-deploy hooks raised unexpectedly for %s", deploy_id)
                await _push(q, "hook_result", all_ok=False, results=[
                    {"hook": name, "ok": False, "reason": "unexpected_error", "details": {"error": str(exc)}}
                    for name in hook_names
                ])
            else:
                await _push(q, "hook_result",
                            all_ok=hooks_result.all_ok,
                            results=[
                                {
                                    "hook": r.hook,
                                    "ok": r.ok,
                                    "reason": r.reason,
                                    "details": r.details,
                                }
                                for r in hooks_result.results
                            ])
                if not hooks_result.all_ok:
                    failed = [r.hook for r in hooks_result.failed]
                    await step(
                        f"Post-deploy hook(s) failed: {failed}. Container is running — re-run hooks from the tile menu.",
                        detail="The container deployed successfully. Hook failures do not roll back the container.",
                    )
                else:
                    await step("Post-deploy hooks completed successfully.")

    except Exception as exc:
        logger.exception("Unexpected deploy error for %s", deploy_id)
        await error(f"Unexpected error: {exc}")
    finally:
        await q.put(None)  # sentinel — tells SSE generator to close stream
        # Keep queue alive briefly so the SSE endpoint can drain it before release.
        await asyncio.sleep(5)
        release(deploy_id)
