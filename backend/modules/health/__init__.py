"""Health module — the Fleet Health aggregator + fleet-source registry.

Promotes the former placeholder into the cross-cutting roll-up: modules publish
`{label, status, metrics}` rows (via an in-process provider or a pulled health
route) that `GET /api/health/fleet` merges next to the n8n instance roll-up. See
docs/specs/2026-07-01-fleet-health-contribution-api.md.
"""

from .router import router  # noqa: F401
