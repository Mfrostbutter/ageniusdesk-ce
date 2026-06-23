"""C3 constitution package.

Re-exports the public API from loader so callers can write:
    from backend.modules.assistant.baseline import loader as constitution
or:
    from backend.modules.assistant.baseline import render, read, write, ensure_baseline
"""

from backend.modules.assistant.baseline.loader import (
    ensure_baseline,
    read,
    render,
    write,
)

__all__ = ["render", "read", "write", "ensure_baseline"]
