"""HTML sanitizer for user-supplied embed code.

Accepts either a full ``<iframe>`` snippet or a bare URL. Returns a dict with
a cleaned iframe src + whitelisted attributes. Everything else is stripped.
Scripts, event handlers, javascript: URIs, and unknown tags are rejected.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlparse

# Attributes we preserve on iframes (case-insensitive).
IFRAME_ALLOWED_ATTRS = {
    "src",
    "width",
    "height",
    "title",
    "allow",
    "allowfullscreen",
    "allowtransparency",
    "frameborder",
    "scrolling",
    "loading",
    "referrerpolicy",
    "sandbox",
    "style",
}

# Hosts we trust for embed sources. Users can still save embeds from any
# host — this list is used only to label sources and warn on unusual ones.
KNOWN_EMBED_HOSTS = {
    "open.spotify.com",
    "embed.spotify.com",
    "www.youtube.com",
    "youtube.com",
    "youtube-nocookie.com",
    "www.youtube-nocookie.com",
    "music.youtube.com",
    "w.soundcloud.com",
    "embed.music.apple.com",
    "embed.tidal.com",
    "bandcamp.com",
    "www.bandcamp.com",
    "mixcloud.com",
    "www.mixcloud.com",
    "player-widget.mixcloud.com",
    "radio.garden",
    "www.radio.garden",
    "last.fm",
    "www.last.fm",
}


class _IframeExtractor(HTMLParser):
    """Pull the first <iframe> tag out of a snippet, ignoring everything else."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.attrs: dict[str, str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.attrs is not None:
            return
        if tag.lower() != "iframe":
            return
        self.attrs = {k.lower(): (v or "") for k, v in attrs}


def _is_safe_url(url: str) -> bool:
    """Block javascript:, data:, vbscript:, file: and relative URLs without host."""
    if not url:
        return False
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return False
    if parsed.scheme.lower() not in ("http", "https"):
        return False
    if not parsed.netloc:
        return False
    return True


def _safe_attrs(attrs: dict[str, str]) -> dict[str, str]:
    """Filter attributes to the whitelist and strip any event handlers."""
    out: dict[str, str] = {}
    for k, v in attrs.items():
        key = k.lower()
        if key.startswith("on"):
            continue
        if key not in IFRAME_ALLOWED_ATTRS:
            continue
        # Reject style containing expression() or javascript:
        if key == "style" and re.search(r"(javascript:|expression\s*\()", v, re.I):
            continue
        out[key] = v
    return out


def sanitize_embed(raw: str) -> dict[str, Any]:
    """Parse and sanitize user-supplied embed HTML or URL.

    Returns:
        {
          "ok": True,
          "src": "...",            # always set on success
          "attrs": {...},          # whitelisted attrs (may include width/height/etc)
          "host": "...",
          "known_host": bool,
          "html": "<iframe ...>",  # rebuilt, clean iframe markup
        }
        or {"ok": False, "error": "..."} on failure.
    """
    if not raw or not isinstance(raw, str):
        return {"ok": False, "error": "Empty input"}

    raw = raw.strip()

    # Case 1: user pasted a bare URL
    if raw.lower().startswith(("http://", "https://")) and "<" not in raw:
        if not _is_safe_url(raw):
            return {"ok": False, "error": "URL must be http(s) with a host"}
        host = urlparse(raw).hostname or ""
        attrs = {"src": raw, "width": "100%", "height": "200", "frameborder": "0",
                 "allow": "autoplay; encrypted-media; picture-in-picture",
                 "loading": "lazy"}
        return {
            "ok": True,
            "src": raw,
            "attrs": attrs,
            "host": host,
            "known_host": host in KNOWN_EMBED_HOSTS,
            "html": _build_iframe(attrs),
        }

    # Case 2: user pasted iframe HTML
    parser = _IframeExtractor()
    try:
        parser.feed(raw)
    except Exception as e:
        return {"ok": False, "error": f"Could not parse HTML: {e}"}

    if parser.attrs is None:
        return {"ok": False, "error": "No <iframe> tag found in input"}

    src = parser.attrs.get("src", "")
    if not _is_safe_url(src):
        return {"ok": False, "error": "iframe src is missing or unsafe"}

    clean = _safe_attrs(parser.attrs)
    clean.setdefault("width", "100%")
    clean.setdefault("height", "200")
    clean.setdefault("loading", "lazy")
    host = urlparse(src).hostname or ""

    return {
        "ok": True,
        "src": src,
        "attrs": clean,
        "host": host,
        "known_host": host in KNOWN_EMBED_HOSTS,
        "html": _build_iframe(clean),
    }


def _build_iframe(attrs: dict[str, str]) -> str:
    """Rebuild a clean iframe tag from a dict of attributes."""
    parts = ["<iframe"]
    for k, v in attrs.items():
        # Boolean-style attrs
        if k in ("allowfullscreen", "allowtransparency") and v in ("", "true", k):
            parts.append(k)
        else:
            parts.append(f'{k}="{_escape_attr(v)}"')
    parts.append("></iframe>")
    return " ".join(parts)


def _escape_attr(v: str) -> str:
    return v.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")
