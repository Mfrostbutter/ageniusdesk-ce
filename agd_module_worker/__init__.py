"""Out-of-process worker runtime for community modules.

This package lives OUTSIDE the `backend` package on purpose: a community-module
worker is launched by absolute path (`python /app/agd_module_worker/main.py`) so
that starting it never imports the host application. Its own import graph is
stdlib + third-party deps only.

See docs/specs/2026-06-27-out-of-process-backend-isolation.md (Section 5.4).

Phase 1 (this commit): the sandbox primitives (env allowlist, sys.path curation,
host-import blocker) and the bootstrap that serves a module's router behind a
proxy-secret check. The host-side spawner and reverse proxy land in phase 2.
"""

__version__ = "0.1.0"
