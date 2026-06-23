"""Post-deploy hook engine for AGD-managed container templates.

After a container deploy succeeds, ``run_hooks`` fires each hook listed in
``template.post_deploy_hooks`` sequentially.  Hook order matters: hooks that
depend on each other (e.g. vault_bootstrap must finish before mcp_register can
push a credential) are listed in dependency order in the template JSON.

Hook contract
-------------
- Each hook is an async callable ``(container_id: str) -> HookResult``.
- Hooks are registered by name in ``HOOK_REGISTRY``.
- A failed hook does NOT roll back the container; the container stays deployed
  and the caller surfaces the failure so the operator can re-run.
- Per-hook wall-clock timeout: 60 seconds.  Exceeding it produces
  ``HookResult(ok=False, reason="timeout")``.

Adding a new hook
-----------------
1. Create ``backend/modules/docker_mgr/post_deploy_hooks/agd_<name>.py``
   exporting ``async def run(container_id: str) -> HookResult``.
2. Register it in HOOK_REGISTRY below.
3. Write tests in ``tests/test_post_deploy_hooks.py``.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)

# ── Result types ──────────────────────────────────────────────────────────────

HOOK_TIMEOUT_SECONDS = 60


@dataclass
class HookResult:
    """Outcome of a single hook invocation."""
    hook: str
    ok: bool
    reason: str = ""
    details: dict = field(default_factory=dict)


@dataclass
class RunHooksResult:
    """Aggregated outcome of all hooks fired for one deploy."""
    all_ok: bool
    results: list[HookResult] = field(default_factory=list)

    @property
    def failed(self) -> list[HookResult]:
        return [r for r in self.results if not r.ok]


# ── Registry ──────────────────────────────────────────────────────────────────

# Maps hook name (as declared in template JSON) to the async callable.
# Import the implementation module lazily so registry stays importable even
# when the hook's optional dependencies are absent.
HookCallable = Callable[[str], Awaitable[HookResult]]

HOOK_REGISTRY: dict[str, HookCallable] = {}


def _register_builtin_hooks() -> None:
    """Populate HOOK_REGISTRY with all built-in hooks.

    Called once at module import.  Importing each hook module here keeps the
    registry declaration in one place and avoids circular imports.

    No built-in hooks ship in the Community Edition; the registry starts empty
    and the engine supports multiple hooks per tile when one is added.
    """
    return


_register_builtin_hooks()

# ── Unknown-hook validation ───────────────────────────────────────────────────


class UnknownHookError(ValueError):
    """Raised when a template references a hook name not in HOOK_REGISTRY."""


def validate_hook_names(hook_names: list[str]) -> None:
    """Raise UnknownHookError for any name not in HOOK_REGISTRY.

    Called by templates.py at template load time so operators see a clear
    message rather than a silent no-op at deploy time.
    """
    unknown = [n for n in hook_names if n not in HOOK_REGISTRY]
    if unknown:
        raise UnknownHookError(f"Unknown post-deploy hook(s): {unknown!r}. Known: {sorted(HOOK_REGISTRY)!r}")


# ── Orchestrator ──────────────────────────────────────────────────────────────


async def run_hooks(container_id: str, hook_names: list[str]) -> RunHooksResult:
    """Fire hooks sequentially for *container_id*.

    Order is preserved.  A hook failure does not prevent subsequent hooks from
    running — each hook is independent.  If a hook raises an unexpected
    exception it is caught and surfaced as a failed HookResult so callers
    always receive a complete RunHooksResult.

    Args:
        container_id: Short or full Docker container ID for the just-deployed container.
        hook_names:   Ordered list of hook names from ``template.post_deploy_hooks``.

    Returns:
        RunHooksResult with per-hook status.  ``all_ok`` is True only when
        every hook succeeded.
    """
    if not hook_names:
        return RunHooksResult(all_ok=True)

    results: list[HookResult] = []

    for name in hook_names:
        fn = HOOK_REGISTRY.get(name)
        if fn is None:
            logger.error("post_deploy_hooks: unknown hook %r — skipping", name)
            results.append(HookResult(hook=name, ok=False, reason="unknown_hook"))
            continue

        logger.info("post_deploy_hooks: running %r for container %s", name, container_id[:12])
        try:
            result = await asyncio.wait_for(fn(container_id), timeout=HOOK_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            logger.error("post_deploy_hooks: hook %r timed out after %ds", name, HOOK_TIMEOUT_SECONDS)
            result = HookResult(hook=name, ok=False, reason="timeout")
        except Exception as exc:  # noqa: BLE001
            logger.exception("post_deploy_hooks: hook %r raised unexpectedly: %s", name, exc)
            result = HookResult(hook=name, ok=False, reason="unexpected_error", details={"error": str(exc)})

        # Ensure hook name is stamped on result regardless of what the hook returned.
        result.hook = name
        results.append(result)

        if result.ok:
            logger.info("post_deploy_hooks: %r succeeded — %r", name, result.details)
        else:
            logger.warning("post_deploy_hooks: %r failed — reason=%r details=%r", name, result.reason, result.details)

    all_ok = all(r.ok for r in results)
    return RunHooksResult(all_ok=all_ok, results=results)
