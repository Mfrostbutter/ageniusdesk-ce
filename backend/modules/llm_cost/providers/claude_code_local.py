"""Claude Code token volume from local session logs (~/.claude/projects/**/*.jsonl).

`ClaudeLogTailer` is stdlib-only and vendored verbatim into
install/llm-cost-forward.py; keep the two in step. It reads only usage metadata,
never prompts, responses, file contents, or paths. Days are UTC so every device
lines up with the provider billing day. Server-side this source only works when
a log directory is mounted; the forwarder is the primary path.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
from pathlib import Path
from typing import Any, Iterable, Optional

from backend.modules.llm_cost.models import Snapshot
from backend.modules.llm_cost.providers.base import ConfigError, Provider

# --- vendored tailer: begin ---
MAX_LINE_BYTES = 1 * 1024 * 1024
MAX_READ_PER_FILE = 4 * 1024 * 1024
MAX_FILES_PER_SCAN = 400
MAX_DEDUPE_KEYS = 200_000
TOKEN_KINDS = ("input", "output", "cache_read", "cache_write_5m", "cache_write_1h")


def _parse_timestamp(value: Any) -> Optional[float]:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.timestamp()


def _utc_day(epoch: float) -> str:
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).strftime("%Y-%m-%d")


def _num(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value) if value > 0 else 0.0


def usage_tokens(usage: dict) -> dict:
    """Map a message.usage block to token kinds; cache writes split 5m/1h when reported."""
    creation = usage.get("cache_creation") if isinstance(usage.get("cache_creation"), dict) else {}
    w5 = _num(creation.get("ephemeral_5m_input_tokens"))
    w1 = _num(creation.get("ephemeral_1h_input_tokens"))
    if not (w5 or w1):
        w5 = _num(usage.get("cache_creation_input_tokens"))
    return {"input": _num(usage.get("input_tokens")), "output": _num(usage.get("output_tokens")),
            "cache_read": _num(usage.get("cache_read_input_tokens")), "cache_write_5m": w5, "cache_write_1h": w1}


class _FileCursor:
    __slots__ = ("offset", "inode", "size", "partial")

    def __init__(self) -> None:
        self.offset = 0
        self.inode: Optional[int] = None
        self.size = 0
        self.partial = b""


class ClaudeLogTailer:
    """Incremental JSONL reader: per-file offsets, dedupe on message.id|requestId."""

    def __init__(self, root: str) -> None:
        self.root = Path(os.path.expanduser(str(root)))
        self.cursors: dict = {}
        self.seen_keys: set = set()
        self.minutely: dict = {}
        self.day_models: dict = {}
        self.sessions: dict = {}
        self.first_scan = True

    def exists(self) -> bool:
        return self.root.is_dir()

    def _files(self) -> list:
        def mtime(p):
            try:
                return p.stat().st_mtime
            except OSError:
                return 0.0
        try:
            files = sorted(self.root.rglob("*.jsonl"), key=mtime, reverse=True)
        except OSError:
            return []
        return files[:MAX_FILES_PER_SCAN]

    def _read_new(self, path) -> Iterable[bytes]:
        cursor = self.cursors.setdefault(str(path), _FileCursor())
        try:
            stat = path.stat()
        except OSError:
            return []
        if cursor.inode is not None and (stat.st_ino != cursor.inode or stat.st_size < cursor.size):
            cursor.offset = 0
            cursor.partial = b""
        cursor.inode = stat.st_ino
        cursor.size = stat.st_size
        if cursor.offset >= stat.st_size:
            return []
        try:
            with path.open("rb") as handle:
                handle.seek(cursor.offset)
                chunk = handle.read(MAX_READ_PER_FILE)
        except OSError:
            return []
        cursor.offset += len(chunk)
        lines = (cursor.partial + chunk).split(b"\n")
        cursor.partial = lines.pop() if lines else b""
        if len(cursor.partial) > MAX_LINE_BYTES:
            cursor.partial = b""
        return lines

    def ingest_line(self, raw: bytes, now: float) -> None:
        if not raw or len(raw) > MAX_LINE_BYTES:
            return
        try:
            record = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return
        if not isinstance(record, dict):
            return
        message = record.get("message")
        if not isinstance(message, dict) or not isinstance(message.get("usage"), dict):
            return
        dedupe = "{0}|{1}".format(message.get("id") or "", record.get("requestId") or "")
        if dedupe != "|":
            if dedupe in self.seen_keys:
                return
            if len(self.seen_keys) < MAX_DEDUPE_KEYS:
                self.seen_keys.add(dedupe)
        tokens = usage_tokens(message["usage"])
        total = sum(tokens.values())
        if total <= 0:
            return
        stamp = _parse_timestamp(record.get("timestamp")) or now
        day = _utc_day(stamp)
        model = str(message.get("model") or "unknown")[:80]
        bucket = self.day_models.setdefault(day, {}).setdefault(model, dict.fromkeys(TOKEN_KINDS, 0.0))
        for k, v in tokens.items():
            bucket[k] += v
        minute = int(stamp) // 60
        self.minutely[minute] = self.minutely.get(minute, 0.0) + total
        session = record.get("sessionId")
        if isinstance(session, str) and session:
            self.sessions.setdefault(day, set()).add(session)

    def _prune(self, now: float) -> None:
        cutoff = int(now) // 60 - 180
        for minute in [m for m in self.minutely if m < cutoff]:
            del self.minutely[minute]
        keep_from = _utc_day(now - 62 * 86400)
        for day in [d for d in self.day_models if d < keep_from]:
            del self.day_models[day]
        for day in [d for d in self.sessions if d < keep_from]:
            del self.sessions[day]

    def scan(self, now: float) -> dict:
        """Read appended lines and return ingest-shaped counters, usage rows, and meta."""
        self._prune(now)
        scanned = 0
        for path in self._files():
            for line in self._read_new(path):
                self.ingest_line(line, now)
            scanned += 1
        today = _utc_day(now)
        month = today[:7]
        day_models = self.day_models.get(today, {})
        day = dict.fromkeys(TOKEN_KINDS, 0.0)
        for tok in day_models.values():
            for k in TOKEN_KINDS:
                day[k] += tok[k]
        mtd: dict = {}
        for key, models in self.day_models.items():
            if key.startswith(month):
                for model, tok in models.items():
                    acc = mtd.setdefault(model, dict.fromkeys(TOKEN_KINDS, 0.0))
                    for k in TOKEN_KINDS:
                        acc[k] += tok[k]
        this_minute = int(now) // 60
        last_hour = sum(v for m, v in self.minutely.items() if this_minute - 60 < m <= this_minute)
        this_hour = sum(v for m, v in self.minutely.items() if m // 60 == this_minute // 60)
        written = day["cache_write_5m"] + day["cache_write_1h"]
        counters = [
            {"key": "input_tokens", "value": day["input"]},
            {"key": "output_tokens", "value": day["output"]},
            {"key": "cache_write_tokens", "value": written},
            {"key": "cache_read_tokens", "value": day["cache_read"]},
            {"key": "total_tokens", "value": sum(day.values())},
            {"key": "tokens_per_hour", "value": last_hour},
            {"key": "tokens_this_hour", "value": this_hour},
            {"key": "sessions", "value": float(len(self.sessions.get(today, ()))), "unit": "requests"},
        ]
        usage = [{"model": m, "window": "today", **t} for m, t in day_models.items()]
        usage += [{"model": m, "window": "mtd", **t} for m, t in mtd.items()]
        usage.sort(key=lambda r: -sum(r[k] for k in TOKEN_KINDS))
        meta = {"filesTracked": len(self.cursors), "filesScanned": scanned, "backfill": self.first_scan}
        if day_models:
            meta["topModel"] = max(day_models, key=lambda m: sum(day_models[m].values()))
        self.first_scan = False
        return {"counters": counters, "usage": usage[:64], "meta": meta}
# --- vendored tailer: end ---


class ClaudeCodeLocalProvider(Provider):
    TYPE = "claude_code_local"
    DISPLAY_NAME = "Claude Code (server logs)"
    KIND = "agent"
    ACCENT = "#d97757"
    DEFAULT_INTERVAL = 60
    MIN_INTERVAL = 15
    DESCRIPTION = ("Tails Claude Code session logs in a directory mounted into the dashboard container. "
                   "Most installs use the workstation forwarder instead.")
    FIELDS = ({"key": "projects_dir", "label": "Projects directory", "type": "text",
               "default": "~/.claude/projects"},)

    def __init__(self, context) -> None:
        super().__init__(context)
        self.tailer = ClaudeLogTailer(str(context.option("projects_dir", "~/.claude/projects")))

    def validate(self) -> None:
        if not self.tailer.exists():
            raise ConfigError(f"no Claude Code logs at {self.tailer.root}; mount a directory and set projects_dir")

    async def fetch(self) -> Snapshot:
        import time

        from backend.modules.llm_cost.providers.push import parse_payload

        self.validate()
        now = time.time()
        body = await asyncio.to_thread(self.tailer.scan, now)
        body.update({"displayName": self.display_name, "accent": self.accent, "kind": self.KIND,
                     "observedAt": int(now)})
        snap = parse_payload(body, self.source_id, now=int(now))
        return self.snapshot(spend=snap.spend, counters=snap.counters, meta=snap.meta,
                             detail={"models": _models(snap)})


def _models(snap: Snapshot) -> list[dict]:
    from backend.modules.llm_cost.providers.base import model_rows

    per = {(r["model"], r["window"]): {k: r[k] for k in TOKEN_KINDS} for r in snap.detail.get("usage") or []}
    return model_rows(per)
