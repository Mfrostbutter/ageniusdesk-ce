"""Markdown note parsing — frontmatter, wikilinks, tags.

Pure functions, no I/O. Unit-testable.

Obsidian-compatible subset:
  - YAML frontmatter between leading `---` fences.
  - `[[Target]]`, `[[Target|Display]]`, `[[Target#heading]]`, `[[path/Target]]`.
  - Inline `#tag-name` and frontmatter `tags:` array.

This module is the reference implementation for Phase 2 backend extraction;
keep it side-effect-free and dependency-free so the Protocol pull is a rename
rather than a rewrite.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# Frontmatter: "---\n<yaml>\n---\n" at the very top of the file.
_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)

# [[target]] with optional |display and optional #heading and optional folder/.
# Match only when not preceded by a backtick (skip code spans is handled by
# stripping fenced blocks before regex — see parse_note).
_WIKILINK_RE = re.compile(r"\[\[([^\[\]|#\n]+?)(?:#[^\[\]|\n]*)?(?:\|[^\[\]\n]*)?\]\]")

# Inline hashtag: #foo-bar or #foo/bar. Must be at start of line or after whitespace.
# Excludes common false positives: `#` in code, markdown headings, URLs.
_TAG_RE = re.compile(r"(?:^|\s)#([A-Za-z][\w/-]*)")

# Strip fenced code blocks (``` or ~~~) and inline code (`...`) before parsing
# wikilinks/tags so code examples don't leak into the index.
_FENCED_CODE_RE = re.compile(r"```[\s\S]*?```|~~~[\s\S]*?~~~", re.MULTILINE)
_INLINE_CODE_RE = re.compile(r"`[^`\n]+`")

# ATX headings — take first # line as implicit title if no frontmatter title.
_HEADING_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)


@dataclass
class Note:
    """Parsed note. `body` is the markdown content *without* the frontmatter."""
    title: str
    body: str
    frontmatter: dict[str, Any] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    links: list[str] = field(default_factory=list)  # normalized wikilink targets


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Extract YAML frontmatter. Returns (dict, body-without-frontmatter).

    YAML parsing is intentionally minimal — only flat `key: value` and
    `key: [a, b]`/`key: [a, "b c"]` array forms, no nesting, no block styles.
    That covers Obsidian's default note metadata and avoids a YAML lib
    dependency. Anything fancier gets dropped silently; the operator can
    still see the raw frontmatter in the editor.
    """
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    raw = m.group(1)
    body = text[m.end():]
    out: dict[str, Any] = {}
    for line in raw.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip()
        val = val.strip()
        if not key:
            continue
        if val.startswith("[") and val.endswith("]"):
            inner = val[1:-1]
            # split on commas not inside quotes
            items: list[str] = []
            for part in _split_csv(inner):
                part = part.strip()
                if (part.startswith('"') and part.endswith('"')) or (part.startswith("'") and part.endswith("'")):
                    part = part[1:-1]
                if part:
                    items.append(part)
            out[key] = items
        elif (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
            out[key] = val[1:-1]
        elif val.lower() in {"true", "false"}:
            out[key] = val.lower() == "true"
        elif val == "":
            out[key] = ""
        else:
            # number or plain string
            try:
                out[key] = int(val)
            except ValueError:
                try:
                    out[key] = float(val)
                except ValueError:
                    out[key] = val
    return out, body


def _split_csv(s: str) -> list[str]:
    """Split on commas respecting single/double quotes. No escape support."""
    out: list[str] = []
    cur = []
    quote: str | None = None
    for ch in s:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
            cur.append(ch)
        elif ch == ",":
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    if cur:
        out.append("".join(cur))
    return out


def extract_wikilinks(body: str) -> list[str]:
    """Return normalized wikilink targets (basename only, no heading/alias).

    `[[Foo Bar]]` → `"Foo Bar"`.
    `[[notes/Foo|Display]]` → `"Foo"` (last path segment, used for matching
    any note whose basename equals that — matches Obsidian's "shortest path
    when possible" default).
    Duplicates are preserved (for occurrence counts); de-dupe at the caller
    if you want unique targets.
    """
    stripped = _INLINE_CODE_RE.sub("", _FENCED_CODE_RE.sub("", body))
    out: list[str] = []
    for m in _WIKILINK_RE.finditer(stripped):
        target = m.group(1).strip()
        if not target:
            continue
        # Keep only the last segment so `folder/Note` → `Note`.
        if "/" in target:
            target = target.rsplit("/", 1)[1]
        out.append(target)
    return out


def extract_tags(body: str) -> list[str]:
    """Return inline #tags (lowercased, de-duped, preserved order)."""
    stripped = _INLINE_CODE_RE.sub("", _FENCED_CODE_RE.sub("", body))
    seen: set[str] = set()
    out: list[str] = []
    for m in _TAG_RE.finditer(stripped):
        tag = m.group(1).lower()
        if tag not in seen:
            seen.add(tag)
            out.append(tag)
    return out


def derive_title(frontmatter: dict[str, Any], body: str, fallback: str) -> str:
    """Prefer frontmatter.title, then first H1, then filename fallback."""
    ft = frontmatter.get("title")
    if isinstance(ft, str) and ft.strip():
        return ft.strip()
    m = _HEADING_RE.search(body)
    if m:
        return m.group(1).strip()
    return fallback


def parse_note(text: str, filename_hint: str = "Untitled") -> Note:
    """Parse a full note document into its structured parts."""
    fm, body = parse_frontmatter(text)
    links = extract_wikilinks(body)
    inline_tags = extract_tags(body)
    # Combine inline tags with frontmatter tags (Obsidian behavior).
    fm_tags: list[str] = []
    raw_fm_tags = fm.get("tags")
    if isinstance(raw_fm_tags, list):
        fm_tags = [str(t).lstrip("#").lower() for t in raw_fm_tags if str(t).strip()]
    elif isinstance(raw_fm_tags, str) and raw_fm_tags.strip():
        fm_tags = [raw_fm_tags.lstrip("#").lower()]
    seen: set[str] = set()
    all_tags: list[str] = []
    for t in fm_tags + inline_tags:
        if t not in seen:
            seen.add(t)
            all_tags.append(t)
    return Note(
        title=derive_title(fm, body, filename_hint),
        body=body,
        frontmatter=fm,
        tags=all_tags,
        links=links,
    )
