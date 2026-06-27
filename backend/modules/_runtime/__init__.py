"""Host-side runtime for out-of-process community modules.

This package is the HOST half of out-of-process isolation (spec 2026-06-27): it
spawns a sandboxed worker subprocess per isolated community module and
reverse-proxies /api/{id}/* to it. The worker bootstrap itself lives OUTSIDE
`backend` (in agd_module_worker/) so launching it never imports the host.

Phase 2: supervisor (spawn/health/stop/orphan-cleanup) + reverse proxy. Gated by
AGD_MODULE_ISOLATION; default "in_process" so existing behavior is unchanged.
"""
