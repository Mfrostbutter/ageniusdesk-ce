"""Supervisor for out-of-process community-module workers.

Spawns one subprocess per isolated community module (running
agd_module_worker/main.py with a scrubbed, allowlisted environment), tracks its
bind + health, and tears it down on shutdown. The reverse proxy (proxy.py)
forwards /api/{id}/* to the worker's bind.

Transport: a Unix domain socket per worker on POSIX (file mode honored by the OS),
a loopback TCP port on Windows. Either way the worker rejects any request lacking
the per-spawn X-AGD-Proxy-Secret, so a direct local connection cannot bypass the
host's auth.

This is the HOST side and may import `backend`. The worker bootstrap it launches
deliberately cannot (see agd_module_worker).
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

# repo root = .../backend/modules/_runtime/supervisor.py -> parents[3]
HOST_ROOT = Path(__file__).resolve().parents[3]
WORKER_MAIN = HOST_ROOT / "agd_module_worker" / "main.py"

COMMUNITY_MODULES_DIR = Path("data/modules")
RUN_DIR = Path("data/run")
PIDFILE = RUN_DIR / "workers.json"

# Marker passed in the worker's argv so the orphan sweep can confirm a recorded
# PID is still OUR worker (and not a reused PID) before killing it.
WORKER_MARKER = "--agd-module"

# UDS on POSIX; loopback TCP on Windows (uvicorn UDS support there is unreliable).
USE_UDS = os.name == "posix"

HEALTH_TIMEOUT_S = 20.0
HEALTH_POLL_S = 0.25
STOP_GRACE_S = 5.0


class WorkerError(RuntimeError):
    """A worker failed to spawn or never became healthy."""


def _free_loopback_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


class ModuleWorker:
    """One community module running in its own subprocess behind a proxy secret."""

    def __init__(self, module_id: str, module_parent: Path, data_dir: Path):
        self.module_id = module_id
        self.module_parent = module_parent
        self.data_dir = data_dir
        import secrets as _secrets

        self.proxy_secret = _secrets.token_urlsafe(32)
        self.proc: subprocess.Popen | None = None
        self.started_at: float = 0.0
        self.restarts: int = 0
        self.log_path = RUN_DIR / f"mod-{module_id}.log"
        self._log_fh = None

        if USE_UDS:
            self.uds_path: str | None = str((RUN_DIR / f"mod-{module_id}.sock").resolve())
            self.port: int | None = None
            self.base_url = "http://worker"
        else:
            self.uds_path = None
            self.port = _free_loopback_port()
            self.base_url = f"http://127.0.0.1:{self.port}"

        self._client: httpx.AsyncClient | None = None
        self._client_loop = None

    # -- env + spawn -----------------------------------------------------------

    def _build_env(self, forward_env: list[str] | None) -> dict[str, str]:
        # Reuse the worker's own allowlist so host + worker agree on what leaks.
        from agd_module_worker.sandbox import build_worker_env

        bind = f"unix:{self.uds_path}" if USE_UDS else f"127.0.0.1:{self.port}"
        injected = {
            "AGD_MODULE_ID": self.module_id,
            "AGD_MODULE_PARENT": str(self.module_parent.resolve()),
            "AGD_MODULE_DATA_DIR": str(self.data_dir.resolve()),
            "AGD_HOST_ROOT": str(HOST_ROOT),
            "AGD_PROXY_SECRET": self.proxy_secret,
            "AGD_WORKER_BIND": bind,
        }
        return build_worker_env(dict(os.environ), injected, forward_env)

    def spawn(self, forward_env: list[str] | None = None) -> None:
        RUN_DIR.mkdir(parents=True, exist_ok=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if USE_UDS and self.uds_path and os.path.exists(self.uds_path):
            os.unlink(self.uds_path)

        env = self._build_env(forward_env)
        # Capture worker stdout+stderr to a per-module log (visible for debugging
        # and surfaced on a failed health check).
        self._log_fh = open(self.log_path, "wb")
        # cwd = the module's own data dir, so any stray relative path the module
        # uses resolves inside its sandbox, not the host tree. The marker args
        # make the process identifiable to the orphan sweep (PID-reuse safety).
        self.proc = subprocess.Popen(
            [sys.executable, str(WORKER_MAIN), WORKER_MARKER, self.module_id],
            env=env,
            cwd=str(self.data_dir.resolve()),
            stdout=self._log_fh,
            stderr=subprocess.STDOUT,
        )
        self.started_at = time.time()
        try:
            self._wait_healthy()
            # Restrict the socket to the owner (the proxy secret is the primary
            # control; this stops other local users connecting to the bind).
            if USE_UDS and self.uds_path and os.path.exists(self.uds_path):
                try:
                    os.chmod(self.uds_path, 0o600)
                except OSError as e:  # pragma: no cover - platform-dependent
                    logger.warning("could not chmod worker socket %s: %s", self.uds_path, e)
        except Exception:
            self.stop()
            raise

    def _log_tail(self, n: int = 1500) -> str:
        try:
            if self._log_fh:
                self._log_fh.flush()
            return self.log_path.read_text(errors="replace")[-n:]
        except OSError:
            return "(no worker log)"

    def _sync_transport(self) -> httpx.HTTPTransport:
        return httpx.HTTPTransport(uds=self.uds_path) if USE_UDS else httpx.HTTPTransport()

    def _wait_healthy(self) -> None:
        deadline = time.time() + HEALTH_TIMEOUT_S
        headers = {"x-agd-proxy-secret": self.proxy_secret}
        last_err = ""
        with httpx.Client(transport=self._sync_transport(), base_url=self.base_url, timeout=2.0) as c:
            while time.time() < deadline:
                if self.proc and self.proc.poll() is not None:
                    raise WorkerError(
                        f"worker '{self.module_id}' exited with code {self.proc.returncode} "
                        f"before becoming healthy. Log tail:\n{self._log_tail()}"
                    )
                try:
                    r = c.get("/_worker/health", headers=headers)
                    if r.status_code == 200:
                        logger.info("module worker '%s' healthy on %s", self.module_id, self.base_url)
                        return
                    last_err = f"health HTTP {r.status_code}"
                except httpx.HTTPError as e:
                    last_err = str(e)
                time.sleep(HEALTH_POLL_S)
        raise WorkerError(f"worker '{self.module_id}' not healthy within {HEALTH_TIMEOUT_S}s ({last_err})")

    # -- proxy client ----------------------------------------------------------

    @property
    def client(self) -> httpx.AsyncClient:
        """Async client used by the reverse proxy, bound to the running event loop.

        The host has one long-lived loop, so this builds once in production. It
        rebinds if called from a different loop (e.g. across test clients) instead
        of reusing a client whose loop has closed.
        """
        import asyncio

        loop = asyncio.get_running_loop()
        if self._client is None or self._client_loop is not loop:
            transport = httpx.AsyncHTTPTransport(uds=self.uds_path) if USE_UDS else httpx.AsyncHTTPTransport()
            self._client = httpx.AsyncClient(transport=transport, base_url=self.base_url, timeout=None)
            self._client_loop = loop
        return self._client

    # -- lifecycle -------------------------------------------------------------

    def is_alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    async def aclose(self) -> None:
        """Close the async proxy client from within the event loop (optional)."""
        if self._client is not None:
            try:
                await self._client.aclose()
            finally:
                self._client = None

    def stop(self) -> None:
        # The async client is bound to the host event loop; from sync teardown we
        # drop the ref (use aclose() to close it cleanly from async code).
        self._client = None
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=STOP_GRACE_S)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if USE_UDS and self.uds_path and os.path.exists(self.uds_path):
            try:
                os.unlink(self.uds_path)
            except OSError:
                pass
        if self._log_fh:
            try:
                self._log_fh.close()
            finally:
                self._log_fh = None

    def pid(self) -> int | None:
        return self.proc.pid if self.proc else None


# ── Registry of live workers ──────────────────────────────────────────────────

_workers: dict[str, ModuleWorker] = {}


def get(module_id: str) -> ModuleWorker | None:
    return _workers.get(module_id)


def start_worker(module_id: str, module_parent: Path, forward_env: list[str] | None = None) -> ModuleWorker:
    """Spawn (or replace) the worker for a module and block until it is healthy."""
    existing = _workers.pop(module_id, None)
    if existing:
        existing.stop()
    data_dir = COMMUNITY_MODULES_DIR / module_id / "_data"
    worker = ModuleWorker(module_id, module_parent, data_dir)
    worker.spawn(forward_env)
    _workers[module_id] = worker
    _save_pidfile()
    return worker


def stop_all() -> None:
    if not _workers:
        return  # nothing was started: no side effects in default (in_process) mode
    for worker in list(_workers.values()):
        try:
            worker.stop()
        except Exception as e:  # pragma: no cover - shutdown best-effort
            logger.warning("error stopping worker %s: %s", worker.module_id, e)
    _workers.clear()
    _save_pidfile()


# ── Orphan cleanup across host restarts ───────────────────────────────────────


def _save_pidfile() -> None:
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    data = {
        w.module_id: {"pid": w.pid(), "uds": w.uds_path, "port": w.port}
        for w in _workers.values()
        if w.pid()
    }
    try:
        PIDFILE.write_text(json.dumps(data, indent=2))
    except OSError as e:  # pragma: no cover
        logger.warning("could not write worker pidfile: %s", e)


def _parse_cmdline_string(s: str) -> list[str] | None:
    """Tokenize a command-line STRING (the ps/PowerShell fallback form).

    Returns None on empty or unparseable input: an ambiguous/malformed command
    line counts as "cannot verify", so the orphan sweep skips the kill rather than
    trusting a naive split. (The /proc path needs no parsing; it has exact tokens.)
    """
    s = (s or "").strip()
    if not s:
        return None
    try:
        return shlex.split(s, posix=(os.name == "posix")) or None
    except ValueError:
        return None


def _process_argv(pid: int) -> list[str] | None:
    """Best-effort argv token list for a pid, or None if it can't be read.

    /proc/<pid>/cmdline gives exact NUL-separated tokens (no quoting ambiguity);
    the ps/PowerShell fallbacks return a string we tokenize conservatively.
    """
    proc = f"/proc/{pid}/cmdline"
    if os.path.exists(proc):
        try:
            with open(proc, "rb") as f:
                raw = f.read()
            toks = [t.decode("utf-8", "replace") for t in raw.split(b"\x00") if t]
            return toks or None
        except OSError:
            return None
    try:
        if os.name == "posix":
            out = subprocess.run(["ps", "-p", str(pid), "-o", "command="],
                                 capture_output=True, text=True, timeout=5)
        else:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f"(Get-CimInstance Win32_Process -Filter \"ProcessId={pid}\").CommandLine"],
                capture_output=True, text=True, timeout=10)
        return _parse_cmdline_string(out.stdout)
    except Exception:
        return None


def _pid_is_our_worker(pid: int, module_id: str) -> bool:
    """True only if the live pid is provably one of our module workers.

    Guards against PID reuse after a host restart: the argv must contain an EXACT
    `--agd-module <module_id>` token pair (not a substring, so module id
    "trivial" never matches a "trivialmod" worker). If argv cannot be read we
    return False (skip the kill) rather than risk killing an unrelated process.
    """
    argv = _process_argv(pid)
    if not argv:
        return False
    mid = str(module_id)
    return any(argv[i] == WORKER_MARKER and argv[i + 1] == mid for i in range(len(argv) - 1))


def sweep_orphans() -> None:
    """Kill workers left running by a previous host process and clear their sockets.

    PID-reuse safe: a recorded pid is only signalled when we can prove it is still
    our worker (see _pid_is_our_worker); otherwise it is skipped and logged.
    """
    if not PIDFILE.exists():
        return
    try:
        data = json.loads(PIDFILE.read_text())
    except Exception:
        data = {}
    for module_id, info in data.items():
        pid = info.get("pid")
        if pid:
            if _pid_is_our_worker(pid, module_id):
                try:
                    if os.name == "posix":
                        os.kill(pid, 15)  # SIGTERM
                    else:
                        subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
                    logger.info("swept orphan worker '%s' (pid %s)", module_id, pid)
                except (ProcessLookupError, PermissionError, OSError):
                    pass
            else:
                logger.info("orphan sweep: pid %s is not the '%s' worker (gone or reused); skipping kill",
                            pid, module_id)
        uds = info.get("uds")
        if uds and os.path.exists(uds):
            try:
                os.unlink(uds)
            except OSError:
                pass
    try:
        PIDFILE.unlink()
    except OSError:
        pass
