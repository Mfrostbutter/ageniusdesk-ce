#!/usr/bin/env python3
"""LLM Cost forwarder: push local Claude Code usage to an AgeniusDesk dashboard.

Standalone and stdlib-only. Runs on the workstation where Claude Code runs,
tails ~/.claude/projects/**/*.jsonl, and POSTs aggregate token counts per model
(the dashboard prices them) plus Claude plan limits to
<AGD_URL>/api/llm-cost/ingest with a per-device token.

Only aggregate counters leave the machine. Prompts, responses, file contents,
commands, and project paths never do.

    AGD_LLM_COST_TOKEN=agdlc_... python3 llm-cost-forward.py --url https://agd.example.com
    python3 llm-cost-forward.py --dry-run --once
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import platform
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Iterable, Optional

LOG = logging.getLogger("llm-cost-forward")

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CREDENTIALS_PATH = Path.home() / ".claude" / ".credentials.json"
KEYCHAIN_SERVICE = "Claude Code-credentials"
_LIMIT_SCOPES = {"session": "session", "weekly_all": "weekly", "weekly_scoped": "model_weekly"}

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


# ── plan quotas (Claude subscription limits via the local OAuth session) ──


def _token_from_blob(blob: Any) -> Optional[str]:
    if not isinstance(blob, dict):
        return None
    token = (blob.get("claudeAiOauth") or {}).get("accessToken")
    return token if isinstance(token, str) and token else None


def read_oauth_token(path: Path = CREDENTIALS_PATH) -> Optional[str]:
    """The Claude Code OAuth token, or None. Never logged, never persisted.

    Linux and Windows keep it in ~/.claude/.credentials.json; macOS keeps it in
    the login Keychain.
    """
    try:
        token = _token_from_blob(json.loads(path.read_text(encoding="utf-8")))
        if token:
            return token
    except (OSError, ValueError):
        pass
    if sys.platform == "darwin":
        try:
            out = subprocess.run(["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
                                 capture_output=True, text=True, timeout=10)
            if out.returncode == 0 and out.stdout.strip():
                return _token_from_blob(json.loads(out.stdout.strip()))
        except (OSError, ValueError, subprocess.SubprocessError):
            return None
    return None


def _iso_to_epoch(text: Any) -> Optional[int]:
    if not isinstance(text, str) or not text:
        return None
    try:
        return int(dt.datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def limits_to_quotas(payload: Any) -> list:
    """Map the usage endpoint's limits[] to ingest quota dicts; other fields are unstable."""
    quotas = []
    for limit in ((payload or {}).get("limits") or []) if isinstance(payload, dict) else []:
        if not isinstance(limit, dict):
            continue
        scope = _LIMIT_SCOPES.get(limit.get("kind"))
        pct = limit.get("percent")
        if scope is None or isinstance(pct, bool) or not isinstance(pct, (int, float)):
            continue
        if limit.get("kind") == "session":
            label = "SESSION"
        elif limit.get("kind") == "weekly_all":
            label = "WEEK ALL"
        else:
            model = (((limit.get("scope") or {}).get("model") or {}).get("display_name")) or "MODEL"
            label = "WEEK {0}".format(str(model).upper()[:12])
        quotas.append({"scope": scope, "label": label, "pct": float(pct), "unit": "pct",
                       "resetsAt": _iso_to_epoch(limit.get("resets_at"))})
    return quotas


def fetch_plan_quotas(timeout: float = 15.0) -> Optional[list]:
    """Plan quotas, or None when unavailable. Never blocks the usage push."""
    token = read_oauth_token()
    if not token:
        return None
    request = urllib.request.Request(USAGE_URL)
    request.add_header("Authorization", "Bearer {0}".format(token))
    request.add_header("anthropic-beta", "oauth-2025-04-20")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        LOG.info("plan usage endpoint returned HTTP %s; pushing without quotas", exc.code)
        return None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        LOG.info("plan usage unavailable (%s); pushing without quotas", type(exc).__name__)
        return None
    return limits_to_quotas(payload) or None


class PlanQuotaSource:
    """Plan quotas on their own cadence with a bounded cache (the endpoint rate-limits)."""

    FETCH_SEC = 300
    CACHE_MAX_SEC = 1800

    def __init__(self, fetcher=None) -> None:
        self.fetcher = fetcher or fetch_plan_quotas
        self.quotas = None
        self.fetched_at = 0.0
        self.next_try = 0.0

    def get(self, now: Optional[float] = None) -> Optional[list]:
        moment = time.time() if now is None else now
        if moment >= self.next_try:
            self.next_try = moment + self.FETCH_SEC
            fresh = self.fetcher()
            if fresh:
                self.quotas = fresh
                self.fetched_at = moment
        if self.quotas and moment - self.fetched_at <= self.CACHE_MAX_SEC:
            return self.quotas
        return None


# ── payload + push ─────────────────────────────────────────────────────────


def host_name() -> str:
    """Cross-platform machine name (os.uname does not exist on Windows)."""
    return (socket.gethostname() or platform.node() or "workstation").split(".")[0][:60]


def build_payload(scan: dict, device: str, display_name: str = "Claude Code", accent: str = "#d97757",
                  plan_quotas: Optional[list] = None, now: Optional[float] = None) -> dict:
    meta = dict(scan.get("meta") or {})
    meta["host"] = device
    return {
        "displayName": display_name, "accent": accent, "kind": "agent",
        "observedAt": int(time.time() if now is None else now), "health": "ok",
        "quotas": list(plan_quotas or []), "counters": scan.get("counters") or [],
        "usage": scan.get("usage") or [], "meta": meta,
    }


def require_http_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("--url must be an http(s) URL")
    return url.rstrip("/")


def push(url: str, token: str, payload: dict, timeout: float = 15.0) -> bool:
    request = urllib.request.Request(require_http_url(url) + "/api/llm-cost/ingest",
                                     data=json.dumps(payload).encode("utf-8"), method="POST")
    request.add_header("Content-Type", "application/json")
    request.add_header("Authorization", "Bearer {0}".format(token))
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:200] if exc.fp else ""
        LOG.error("ingest rejected: %s %s %s", exc.code, exc.reason, detail)
        return False
    except (urllib.error.URLError, OSError) as exc:
        LOG.warning("cannot reach the dashboard: %s", exc)
        return False
    LOG.info("pushed %s", ",".join(result.get("accepted", [])) or "nothing")
    return bool(result.get("accepted"))


def resolve_token(args) -> str:
    if args.token:
        return args.token.strip()
    if args.token_file:
        try:
            return Path(os.path.expanduser(args.token_file)).read_text(encoding="utf-8").strip()
        except OSError as exc:
            LOG.error("cannot read token file: %s", exc)
    return ""


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default=os.environ.get("AGD_URL", ""), help="dashboard base URL (or AGD_URL)")
    parser.add_argument("--token", default=os.environ.get("AGD_LLM_COST_TOKEN", ""),
                        help="device token (or AGD_LLM_COST_TOKEN); prefer the env var or --token-file")
    parser.add_argument("--token-file", default=os.environ.get("AGD_LLM_COST_TOKEN_FILE", ""),
                        help="file holding the device token")
    parser.add_argument("--device", default=host_name(), help="name reported for this machine")
    parser.add_argument("--projects-dir", default="~/.claude/projects", help="Claude Code session log root")
    parser.add_argument("--display-name", default="Claude Code")
    parser.add_argument("--accent", default="#d97757")
    parser.add_argument("--interval", type=int, default=60, help="seconds between pushes (min 15)")
    parser.add_argument("--no-plan-quotas", action="store_true", help="skip Claude plan limits")
    parser.add_argument("--once", action="store_true", help="push a single snapshot and exit")
    parser.add_argument("--dry-run", action="store_true", help="print the payload instead of sending it")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%Y-%m-%dT%H:%M:%S",
                        stream=sys.stdout)

    token = resolve_token(args)
    if not args.dry_run:
        if not args.url:
            parser.error("--url is required (or set AGD_URL)")
        if not token:
            parser.error("a device token is required (AGD_LLM_COST_TOKEN or --token-file)")
        try:
            require_http_url(args.url)
        except ValueError as exc:
            parser.error(str(exc))

    tailer = ClaudeLogTailer(args.projects_dir)
    if not tailer.exists():
        LOG.error("no Claude Code logs at %s; pass --projects-dir", tailer.root)
        return 2
    plans = PlanQuotaSource()
    stopping = False

    def _stop(_signum, _frame):
        nonlocal stopping
        stopping = True

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _stop)
        except (ValueError, OSError, AttributeError):
            pass

    interval = max(15, args.interval)
    while not stopping:
        payload = None
        try:
            now = time.time()
            scan = tailer.scan(now)
            quotas = None if args.no_plan_quotas else plans.get(now)
            payload = build_payload(scan, args.device, args.display_name, args.accent, quotas, now)
        except Exception:
            LOG.exception("scan failed")
        if payload is not None:
            if args.dry_run:
                print(json.dumps(payload, indent=2))
            else:
                push(args.url, token, payload)
        if args.once or args.dry_run:
            break
        for _ in range(interval):
            if stopping:
                break
            time.sleep(1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
