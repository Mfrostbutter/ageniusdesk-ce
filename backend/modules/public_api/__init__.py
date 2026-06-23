"""Public API v1 — versioned, auth-gated surface for external integrations.

This module is mounted as a FastAPI sub-app in main.py (not auto-discovered
by register_modules) so it gets its own /api/v1/docs OpenAPI page.
"""
