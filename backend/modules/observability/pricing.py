"""Compatibility shim: the price book moved to `backend.pricing` (shared by Observe
and LLM Cost). Import from there; this module re-exports the old names."""

from backend import pricing as _pricing
from backend.pricing import (  # noqa: F401
    BUNDLED,
    OPENROUTER_MODELS_URL,
    PRICE_BOOK_FILE,
    clear_override,
    estimate_cost,
    normalize,
    price_for,
    refresh,
    set_override,
    status,
)

_BUNDLED = BUNDLED
_normalize = normalize
_match = _pricing._exact
settings_tls_verify = _pricing._tls_verify
