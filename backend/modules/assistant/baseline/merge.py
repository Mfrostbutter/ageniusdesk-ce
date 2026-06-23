"""Section-level merge logic for the C3 constitution.

``apply_overrides`` takes the rendered constitution body (no frontmatter),
a list of which H2 slug names are permitted to be overridden, and optional
per-agent markdown text.  It returns the merged body ready for injection.

Slug rule: lowercase, spaces to dashes, alphanumerics + dash only.
  "Hard Guardrails" -> "hard-guardrails"
  "Tools"           -> "tools"
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# Matches an H2 heading and everything below it until the next H2 (or EOF).
# Group 1 is the heading text; group 2 is the body (including a leading newline).
_H2_BLOCK_RE = re.compile(r"^## (.+?)[ \t]*\n(.*?)(?=^## |\Z)", re.MULTILINE | re.DOTALL)


def _slug(name: str) -> str:
    """Normalise an H2 heading name to its slug form."""
    lowered = name.lower()
    # Replace spaces (and any other non-alphanumeric, non-dash chars) with dashes.
    slugged = re.sub(r"[^a-z0-9]+", "-", lowered)
    # Strip leading/trailing dashes.
    return slugged.strip("-")


def _extract_h2_blocks(text: str) -> list[tuple[str, str, str]]:
    """Return a list of (heading_text, slug, full_block) tuples.

    ``full_block`` is the heading line + body, suitable for search-and-replace.
    """
    blocks: list[tuple[str, str, str]] = []
    for m in _H2_BLOCK_RE.finditer(text):
        heading = m.group(1)
        full = m.group(0)
        blocks.append((heading, _slug(heading), full))
    return blocks


def apply_overrides(
    constitution_md: str,
    overrideable_sections: list[str],
    per_agent_text: str | None,
) -> str:
    """Merge per-agent markdown into the constitution body.

    Rules (per C3 spec, question 2):
    - Sections in ``overrideable_sections`` (slug-matched) may be replaced when
      a matching H2 exists in ``per_agent_text``.
    - Sections NOT in the list are never replaced; per-agent H2s targeting them
      are appended at the end instead.
    - H2 sections in ``per_agent_text`` that do not match any constitution
      section are also appended.
    - If an overrideable slug is declared but the H2 is missing from the
      constitution, the per-agent block is appended (with a WARNING log).
    - Returns ``constitution_md`` unchanged when ``per_agent_text`` is empty.
    """
    if not per_agent_text or not per_agent_text.strip():
        return constitution_md

    agent_blocks = _extract_h2_blocks(per_agent_text)
    if not agent_blocks:
        # per_agent_text has no H2 sections; append the whole thing.
        return constitution_md.rstrip("\n") + "\n\n" + per_agent_text.strip() + "\n"

    result = constitution_md
    append_parts: list[str] = []

    for heading, slug, agent_block in agent_blocks:
        if slug not in overrideable_sections:
            # Not overrideable; queue for appending.
            append_parts.append(agent_block.rstrip("\n"))
            continue

        # Find matching H2 in the constitution (case-insensitive slug match).
        constitution_blocks = _extract_h2_blocks(result)
        matched = [(h, s, b) for h, s, b in constitution_blocks if s == slug]

        if not matched:
            # Declared overrideable but section is missing in the constitution.
            logger.warning(
                "constitution: overrideable section %r declared but H2 not found "
                "in the body; appending instead",
                slug,
            )
            append_parts.append(agent_block.rstrip("\n"))
            continue

        # Replace the first matching constitution block with the agent block.
        # If the constitution has the section multiple times (unusual), only the
        # first match is replaced.
        _const_heading, _const_slug, const_block = matched[0]
        result = result.replace(const_block, agent_block, 1)

    if append_parts:
        result = result.rstrip("\n") + "\n\n" + "\n\n".join(append_parts) + "\n"

    return result
