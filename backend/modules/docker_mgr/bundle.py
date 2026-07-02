"""Multi-container template (bundle) primitives.

Scaffolding for task #23 — see docs/specs/multi-container-templates-2026-05-21.md
for the design rationale, SSE protocol, and verification plan.

A bundle is a Template whose `build(fields)` returns more than one container.
Bundles are deployed as a unit (topological order, shared field set, shared
auto-secret store), labelled for cascade destroy, and grouped in the UI by
faking a `com.docker.compose.project` label.

This module owns the pure-data primitives:

- ContainerSpec dataclass and BuildResult type.
- normalise_build_result() — adapter that lifts legacy `(config, volumes)` tuples
  into a list[ContainerSpec] so deployer.deploy_bundle() has one code path.
- topological_sort() — Kahn's algorithm over depends_on. Raises BundleCycleError
  on a cycle. Lexicographic tie-break for determinism.
- mint_bundle_id() / bundle_labels() — label minting for cascade destroy.
- validate_bundle() — registration-time check: exactly-one primary, no cycles,
  every depends_on name resolves to a sibling.

Builder agent: implement the bodies below. Signatures and return types are
pinned by the spec; do not widen them without updating the spec first.

NOTE: This module deliberately has no side effects. Docker calls, template_state
writes, and SSE event emission all live in deployer.py. Keep it that way so
unit tests can exercise the topo + validation + threading logic without a
Docker daemon.
"""

from __future__ import annotations

import secrets
import string
from dataclasses import dataclass, field
from typing import Any, Union

from . import template_state

# ── Types ────────────────────────────────────────────────────────────────────


@dataclass
class ContainerSpec:
    """One container inside a bundle.

    name:         short member id (e.g. "infisical-db"). Becomes
                  "agd-<bundle_instance>-<name>" as the docker container name.
    config:       aiodocker create-payload (Image / Env / HostConfig / Labels).
                  The deployer injects bundle-aware labels and network attachment;
                  the builder should not pre-set them.
    volumes:      named volume IDs to ensure-create before the container starts.
    depends_on:   sibling spec names that must be started first. Empty for roots.
    role:         "primary" | "service". Exactly one spec per bundle must be
                  "primary" — the UI uses it for the Open URL and port discovery.
    healthcheck:  reserved. v1 ignores this; documented for forward compat.
    expose_port:  the host port the primary container's URL should be built
                  against. Ignored on non-primary specs.
    """

    name: str
    config: dict
    volumes: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    role: str = "service"
    healthcheck: dict | None = None
    expose_port: int | None = None


# A builder may return any of these shapes. normalise_build_result() flattens
# them into a list[ContainerSpec] for the deployer.
LegacyBuildResult = tuple[dict, list[str]]
BuildResult = Union[ContainerSpec, LegacyBuildResult, list[ContainerSpec]]


# ── Errors ───────────────────────────────────────────────────────────────────


class BundleError(Exception):
    """Base for bundle validation / topology errors."""


class BundleCycleError(BundleError):
    """Raised when depends_on edges form a cycle."""


class BundleValidationError(BundleError):
    """Raised when a bundle fails registration-time checks (e.g. zero / two
    primaries, dangling depends_on, duplicate spec names)."""


# ── Public API ───────────────────────────────────────────────────────────────


def normalise_build_result(result: BuildResult, *, fallback_name: str) -> list[ContainerSpec]:
    """Lift any allowed builder return shape into a list[ContainerSpec].

    - tuple (config, volumes)            -> [ContainerSpec(name=fallback_name, role="primary")]
    - single ContainerSpec               -> [spec]   (role coerced to "primary" if "service")
    - list[ContainerSpec]                -> as-is, validated

    fallback_name is the legacy container name the deployer would have used
    (typically the template_id / instance_name). It is the name the resulting
    single-spec gets so single-container templates keep emitting the same
    container name on the host.
    """
    # Legacy 2-tuple: (config, volumes).
    if isinstance(result, tuple) and len(result) == 2:
        config, volumes = result
        return [ContainerSpec(
            name=fallback_name,
            config=config,
            volumes=list(volumes or []),
            role="primary",
        )]
    # Single ContainerSpec.
    if isinstance(result, ContainerSpec):
        if result.role == "service":
            result.role = "primary"
        return [result]
    # list[ContainerSpec] — pass-through, but validate basic shape.
    if isinstance(result, list) and all(isinstance(x, ContainerSpec) for x in result):
        return list(result)
    raise BundleValidationError(
        f"Builder returned an unrecognised shape: {type(result).__name__}. "
        "Expected ContainerSpec, (config, volumes) tuple, or list[ContainerSpec]."
    )


def topological_sort(specs: list[ContainerSpec]) -> list[ContainerSpec]:
    """Return specs in deploy order. Roots first, leaves last.

    Algorithm: Kahn's. On ties, sort lexicographically by name so the order
    is deterministic across runs (and so progress framing "1 of 3" is stable).

    Raises BundleCycleError if depends_on contains a cycle.
    """
    by_name = {s.name: s for s in specs}
    indegree = {s.name: 0 for s in specs}
    for s in specs:
        for dep in s.depends_on:
            if dep not in by_name:
                # Dangling depends_on is a validation error, not a sort error.
                # validate_bundle() catches this earlier; we still defend here
                # in case a caller bypasses validation.
                raise BundleValidationError(
                    f"Spec '{s.name}' depends on unknown sibling '{dep}'."
                )
            indegree[s.name] += 1
    # Kahn's algorithm with deterministic lexicographic tie-break.
    ready = sorted([name for name, d in indegree.items() if d == 0])
    ordered: list[ContainerSpec] = []
    while ready:
        name = ready.pop(0)
        ordered.append(by_name[name])
        # For each sibling that depends on `name`, decrement and maybe enqueue.
        new_ready: list[str] = []
        for s in specs:
            if name in s.depends_on:
                indegree[s.name] -= 1
                if indegree[s.name] == 0:
                    new_ready.append(s.name)
        if new_ready:
            ready = sorted(ready + new_ready)
    if len(ordered) != len(specs):
        unresolved = [name for name, d in indegree.items() if d > 0]
        raise BundleCycleError(
            f"Cycle detected among containers: {sorted(unresolved)}"
        )
    return ordered


def validate_bundle(specs: list[ContainerSpec]) -> None:
    """Registration-time invariant checks.

    - Spec names are unique.
    - Every name in depends_on resolves to a sibling.
    - Exactly one spec has role == "primary".
    - topological_sort succeeds (i.e. no cycle).

    Raises BundleValidationError or BundleCycleError on failure. Returns None
    on success. Called from template registration (built-in module load and
    community JSON load) so a malformed bundle never reaches the UI.
    """
    if not specs:
        raise BundleValidationError("Bundle must have at least one container spec.")
    names = [s.name for s in specs]
    if len(set(names)) != len(names):
        dupes = sorted({n for n in names if names.count(n) > 1})
        raise BundleValidationError(f"Duplicate spec names: {dupes}")
    name_set = set(names)
    for s in specs:
        for dep in s.depends_on:
            if dep not in name_set:
                raise BundleValidationError(
                    f"Spec '{s.name}' depends on unknown sibling '{dep}'."
                )
    primaries = [s.name for s in specs if s.role == "primary"]
    if len(primaries) == 0:
        raise BundleValidationError(
            "Bundle must declare exactly one primary spec (role='primary'); found 0."
        )
    if len(primaries) > 1:
        raise BundleValidationError(
            f"Bundle must declare exactly one primary spec; found {len(primaries)}: {primaries}"
        )
    # Topo sort raises BundleCycleError on cycle.
    topological_sort(specs)


# ── Label / id helpers ───────────────────────────────────────────────────────


def mint_bundle_id(template_id: str, instance_name: str) -> str:
    """Return the canonical bundle id string `<template_id>:<instance_name>`.

    This is the value stamped into the `ageniusdesk.bundle` label and used as
    the path segment for `/api/containers/bundle/<bundle_id>`. Both halves are
    already sanitised by the time they reach here (instance_name comes through
    the same `.strip().replace(" ", "-").lower()` normalisation as single
    templates), but bundle_id() is the single place we encode the separator.
    """
    return f"{template_id}:{instance_name}"


def parse_bundle_id(bundle_id: str) -> tuple[str, str]:
    """Inverse of mint_bundle_id. Returns (template_id, instance_name).

    Raises ValueError if the bundle_id is malformed.
    """
    if ":" not in bundle_id:
        raise ValueError(f"Malformed bundle_id (missing ':'): {bundle_id}")
    template_id, _, instance_name = bundle_id.partition(":")
    if not template_id or not instance_name:
        raise ValueError(f"Malformed bundle_id (empty half): {bundle_id}")
    return template_id, instance_name


def bundle_labels(template_id: str, instance_name: str, spec: ContainerSpec) -> dict[str, str]:
    """Return the docker labels every bundle member must carry.

    Includes:
    - ageniusdesk.managed = "true"
    - ageniusdesk.template = <template_id>
    - ageniusdesk.instance = <instance_name>
    - ageniusdesk.bundle = <bundle_id>
    - ageniusdesk.bundle.role = primary | service
    - ageniusdesk.bundle.member = <spec.name>
    - com.docker.compose.project = "agd-bundle-<template_id>-<instance_name>"
      (faked for UI grouping; see spec §7)

    These labels are merged into the spec's config["Labels"] by the deployer
    just before container create. Do not call this from a Template.build();
    the deployer owns label minting.
    """
    bid = mint_bundle_id(template_id, instance_name)
    return {
        "ageniusdesk.managed": "true",
        "ageniusdesk.template": template_id,
        "ageniusdesk.instance": instance_name,
        "ageniusdesk.bundle": bid,
        "ageniusdesk.bundle.role": spec.role,
        "ageniusdesk.bundle.member": spec.name,
        "com.docker.compose.project": f"agd-bundle-{template_id}-{instance_name}",
        "com.docker.compose.service": spec.name,
    }


def bundle_network_name(template_id: str, instance_name: str) -> str:
    """Return the per-bundle user-defined bridge network name.

    All bundle members are attached to this network at create time so they
    can resolve siblings by container name. See spec "Networking" section.
    """
    return f"agd-bundle-{template_id}-{instance_name}-net"


def member_container_name(template_id: str, instance_name: str, spec_name: str) -> str:
    """Compose the docker container name for a bundle member.

    Pattern: agd-<template_id>-<instance_name>-<spec_name>. Stable + collision-safe
    because instance_name is single-template-scoped and spec_name is unique
    within a bundle (validate_bundle enforces this).
    """
    return f"agd-{template_id}-{instance_name}-{spec_name}"


# ── Field threading helpers ──────────────────────────────────────────────────


def mint_shared_secrets(
    template_id: str,
    instance_name: str,
    fields: dict[str, Any],
    auto_secret_keys: list[str],
) -> dict[str, Any]:
    """Mint auto-generated secrets for a bundle and persist them per-instance.

    For each key in auto_secret_keys:
      1. If fields[key] is truthy, keep it (user supplied a value).
      2. Else if template_state has a persisted value, reuse it.
      3. Else generate a fresh value via secrets module, persist it, and use it.

    Persisted under template_state namespace "bundle:<template_id>" with
    instance_name as the per-instance key. This is the same persistence
    pattern n8n uses for its encryption key, just lifted to bundles.

    Returns a NEW dict with the auto-minted values filled in. Caller threads
    the returned dict into each ContainerSpec.config["Env"] via string
    substitution.
    """
    ns = f"bundle:{template_id}"
    persisted = template_state.load(ns, instance_name)
    out = dict(fields)
    to_persist: dict[str, Any] = {}
    for key in auto_secret_keys:
        if out.get(key):
            # User supplied a value. Persist it so redeploys are idempotent.
            if persisted.get(key) != out[key]:
                to_persist[key] = out[key]
            continue
        if persisted.get(key):
            out[key] = persisted[key]
            continue
        # Mint fresh.
        out[key] = _rand_secret()
        to_persist[key] = out[key]
    # Also persist instance metadata so recreate_bundle can replay without the
    # original field values being in flight.
    snapshot_keys = {"_template_id": template_id, "_instance_name": instance_name}
    for k, v in snapshot_keys.items():
        if persisted.get(k) != v:
            to_persist[k] = v
    if to_persist:
        template_state.save(ns, instance_name, to_persist)
    return out


def load_bundle_snapshot(template_id: str, instance_name: str) -> dict[str, Any]:
    """Return the persisted bundle field snapshot used by recreate_bundle.

    Returns the same dict mint_shared_secrets persisted, including the
    minted auto-secrets and any user-supplied values it captured. Returns an
    empty dict if no snapshot exists (bundle was never deployed successfully).
    """
    return template_state.load(f"bundle:{template_id}", instance_name)


def save_bundle_snapshot(template_id: str, instance_name: str, fields: dict[str, Any]) -> None:
    """Persist the full set of user-supplied field values for replay.

    Called at the end of a successful initial deploy so recreate_bundle can
    re-derive specs from the same inputs. Separate from mint_shared_secrets
    because we only want to write this after the deploy succeeded; a failed
    deploy must not poison future recreates.
    """
    snapshot = {
        "_template_id": template_id,
        "_instance_name": instance_name,
        "_fields": fields,
    }
    template_state.save(f"bundle:{template_id}", instance_name, snapshot)


_SECRET_ALPHABET = string.ascii_letters + string.digits


def _rand_secret(n: int = 32) -> str:
    """Generate a cryptographically random alphanumeric secret of length n."""
    return "".join(secrets.choice(_SECRET_ALPHABET) for _ in range(n))
