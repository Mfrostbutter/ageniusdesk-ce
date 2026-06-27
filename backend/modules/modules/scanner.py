"""AST static scanner for community modules.

Parses every ``.py`` file in an extracted module with the stdlib ``ast`` module
and emits severity-ranked findings. It NEVER imports or executes module code.

Honest framing (must stay in the report and in every UI surface): a static
scanner over Python that runs in-process is a heuristic, not a boundary. A
determined author bypasses pattern matching trivially
(``getattr(__import__('os'), 'system')``, base64-then-exec, a payload fetched at
runtime). The value here is catching low-effort or accidental danger, forcing an
explicit consent moment, and recording what was approved. Do not ship a
"scanned and safe" badge; the phrasing is "heuristic review, not a sandbox.
Only install modules you trust."

The headline output is the declared-vs-detected diff: a capability the code uses
but the manifest did not declare surfaces as a HIGH "undeclared capability."
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from backend.module_registry import Capabilities, ModuleManifest

Severity = Literal["CRITICAL", "HIGH", "MEDIUM", "INFO"]

_SEVERITY_RANK = {"CRITICAL": 3, "HIGH": 2, "MEDIUM": 1, "INFO": 0}

# Network client libraries. A dotted name is a "network name" when it equals one
# of these or starts with one followed by a dot.
_NETWORK_PREFIXES = (
    "httpx",
    "requests",
    "urllib",
    "aiohttp",
    "http.client",
    "urllib3",
    "websockets",
)

# Shell / process execution sinks that always warrant CRITICAL (they run a
# command line, not just a child process).
_SHELL_EXEC = {"os.system", "os.popen"}

# subprocess entry points.
_SUBPROCESS_CALLS = {
    "subprocess.run",
    "subprocess.call",
    "subprocess.check_call",
    "subprocess.check_output",
    "subprocess.Popen",
    "subprocess.getoutput",
    "subprocess.getstatusoutput",
}

# Deserializers that can lead to code execution on untrusted input.
_UNSAFE_LOADS = {"pickle.load", "pickle.loads", "marshal.load", "marshal.loads"}

# Secret-store / config paths a module must never touch directly.
_SECRET_PATH_MARKERS = (
    "secrets.json",
    ".secret_key",
    "data/config.json",
    "data\\config.json",
)

# A "large opaque literal" heuristic: long strings that are pure base64/hex are a
# common obfuscation carrier. Tuned to avoid flagging ordinary prose/templates.
_OPAQUE_MIN_LEN = 800
_BASE64_RE = re.compile(r"^[A-Za-z0-9+/=\s]+$")
_HEX_RE = re.compile(r"^[0-9a-fA-F\s]+$")

_LIMITATIONS = (
    "This is a heuristic AST scan, not a sandbox. It cannot follow obfuscation "
    "(getattr/base64/exec), runtime-fetched code, or dynamic imports with "
    "computed names. Once installed, the module's backend runs in-process with "
    "full data/credential access and its frontend runs in the app page (it can "
    "break the UI). Absence of findings is not a safety guarantee. Only install "
    "modules you trust."
)


class Finding(BaseModel):
    severity: Severity
    category: str
    file: str  # path relative to the module root
    line: int
    detail: str


class ScanReport(BaseModel):
    findings: list[Finding] = Field(default_factory=list)
    # Per-capability reconciliation of what the manifest declared vs what the
    # scan detected. The headline of the report.
    declared_vs_detected: dict = Field(default_factory=dict)
    summary: dict = Field(default_factory=dict)  # severity -> count
    files_scanned: int = 0
    parse_errors: list[str] = Field(default_factory=list)
    limitations: str = _LIMITATIONS

    @property
    def max_severity(self) -> str | None:
        if not self.findings:
            return None
        return max((f.severity for f in self.findings), key=lambda s: _SEVERITY_RANK[s])

    def has(self, severity: Severity) -> bool:
        return any(f.severity == severity for f in self.findings)


def _effective_caps(manifest: ModuleManifest) -> Capabilities:
    """A manifest with no capabilities block declares nothing (empty caps)."""
    return manifest.capabilities or Capabilities()


def _is_network_name(name: str | None) -> bool:
    if not name:
        return False
    return any(name == p or name.startswith(p + ".") for p in _NETWORK_PREFIXES)


def _const_str(node: ast.AST) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _host_of(url: str) -> str | None:
    """Best-effort hostname extraction from a string literal that looks like a URL."""
    m = re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://([^/:?#]+)", url)
    return m.group(1).lower() if m else None


def _host_matches(host: str, pattern: str) -> bool:
    """Glob host match: '*.youtube.com' matches 'www.youtube.com' and 'youtube.com'."""
    pattern = pattern.lower().strip()
    host = host.lower()
    if pattern.startswith("*."):
        base = pattern[2:]
        return host == base or host.endswith("." + base)
    return host == pattern


def _norm_write_path(p: str) -> str:
    """Normalize a path literal for comparison against declared write_paths.

    Declared write_paths are relative to data/. We strip a leading data/ (or
    ./data/) and normalize separators so 'data/research/x.json' and
    'research/x.json' both reduce to a path under the data root.
    """
    s = p.replace("\\", "/").lstrip("./")
    for prefix in ("data/",):
        if s.startswith(prefix):
            s = s[len(prefix):]
    return s


class _FileScanner(ast.NodeVisitor):
    """Walks one parsed file, recording findings and detected capabilities."""

    def __init__(self, rel_path: str, caps: Capabilities, declared_env: set[str]):
        self.rel_path = rel_path
        self.caps = caps
        self.declared_env = declared_env
        self.findings: list[Finding] = []
        # alias -> resolved dotted module path (import os as o -> {'o': 'os'})
        self.aliases: dict[str, str] = {}
        # accumulated detection signals (merged across files by the caller)
        self.detected_network = False
        self.detected_subprocess = False
        self.detected_hosts: set[str] = set()
        self.detected_writes: list[str] = []  # human-readable path/desc per write
        self.detected_env: set[str] = set()

    # -- finding helper --------------------------------------------------------

    def _add(self, severity: Severity, category: str, line: int, detail: str) -> None:
        self.findings.append(
            Finding(severity=severity, category=category, file=self.rel_path, line=line, detail=detail)
        )

    # -- name resolution -------------------------------------------------------

    def _resolve(self, node: ast.AST) -> str | None:
        """Resolve a Name/Attribute chain to a dotted path using the alias map."""
        if isinstance(node, ast.Name):
            return self.aliases.get(node.id, node.id)
        if isinstance(node, ast.Attribute):
            base = self._resolve(node.value)
            return f"{base}.{node.attr}" if base else None
        return None

    # -- imports ---------------------------------------------------------------

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            bound = alias.asname or alias.name.split(".")[0]
            self.aliases[bound] = alias.name if alias.asname else alias.name.split(".")[0]
            # When aliased, the alias points at the full module; otherwise the
            # bound top name is the module root. Track the full path for matching.
            full = alias.name
            self._note_import(full, node.lineno)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        # Relative imports (node.level) keep just the module tail for matching;
        # we do not resolve the package root, which is fine for capability heuristics.
        mod = node.module or ""
        for alias in node.names:
            bound = alias.asname or alias.name
            self.aliases[bound] = f"{mod}.{alias.name}" if mod else alias.name
        self._note_import(mod, node.lineno)
        self.generic_visit(node)

    def _note_import(self, module: str, line: int) -> None:
        if not module:
            return
        root = module.split(".")[0]
        if root == "ctypes":
            self._add("CRITICAL", "native-code", line, "imports ctypes (native FFI; can call arbitrary C)")
        if _is_network_name(module):
            self.detected_network = True
            if not self.caps.network.enabled:
                self._add(
                    "HIGH",
                    "network",
                    line,
                    f"imports network library '{module}' but the manifest does not declare network access",
                )
        if root == "subprocess":
            self.detected_subprocess = True
            if not self.caps.subprocess:
                self._add(
                    "HIGH",
                    "subprocess",
                    line,
                    "imports subprocess but the manifest does not declare subprocess access",
                )
        # Reaching into the AgeniusDesk host (backend.*) is expected for a
        # community module and surfaced as INFO transparency, not a risk.
        # Reaching into the COMMUNITY modules dir (data/modules/*) is another
        # community module poking at a sibling and stays MEDIUM.
        if root == "backend":
            self._add("INFO", "host-import", line, f"imports the AgeniusDesk host package '{module}'")
        elif root == "data" and "modules" in module.split("."):
            self._add("MEDIUM", "cross-module", line, f"imports another community module '{module}'")

    # -- calls -----------------------------------------------------------------

    def visit_Call(self, node: ast.Call) -> None:
        name = self._resolve(node.func)
        if name:
            self._check_call(node, name)
        self.generic_visit(node)

    def _check_call(self, node: ast.Call, name: str) -> None:
        line = node.lineno

        # CRITICAL: dynamic code execution.
        if name in ("eval", "exec", "compile"):
            self._add("CRITICAL", "code-exec", line, f"{name}() executes/compiles code at runtime")
            return
        if name in _SHELL_EXEC:
            self._add("CRITICAL", "shell-exec", line, f"{name}() runs a shell command line")
            return
        if name == "__import__" or name == "importlib.import_module":
            first = node.args[0] if node.args else None
            if first is not None and _const_str(first) is None:
                self._add("CRITICAL", "dynamic-import", line, f"{name}() with a non-literal module name")
            return
        if name in _UNSAFE_LOADS:
            self._add("CRITICAL", "deserialization", line, f"{name}() deserializes data (can execute code)")
            return
        if name.startswith("ctypes."):
            self._add("CRITICAL", "native-code", line, f"{name}() uses ctypes (native FFI)")
            return

        # HIGH: subprocess.
        if name in _SUBPROCESS_CALLS:
            self.detected_subprocess = True
            shell = any(
                kw.arg == "shell" and isinstance(kw.value, ast.Constant) and kw.value.value is True
                for kw in node.keywords
            )
            if shell:
                self._add("HIGH", "subprocess", line, f"{name}(shell=True) spawns a shell")
            elif not self.caps.subprocess:
                self._add("HIGH", "subprocess", line, f"{name}() spawns a child process; subprocess not declared")
            return

        # HIGH: raw sockets bypass any host allowlist entirely.
        if name in ("socket.socket", "socket.create_connection"):
            self.detected_network = True
            self._add("HIGH", "network", line, f"{name}() opens a raw socket (bypasses any host allowlist)")
            return

        # HIGH/INFO: network client calls.
        if _is_network_name(name):
            self.detected_network = True
            self._check_network_call(node, name)
            return

        # getattr/setattr on an imported module -> dynamic attribute access.
        if name in ("getattr", "setattr") and node.args:
            target = self._resolve(node.args[0])
            if target and target in self.aliases.values():
                self._add("MEDIUM", "dynamic-attr", line, f"{name}() on imported module '{target}'")
            return

        # Environment reads via getters (subscript form is handled separately).
        if name in ("os.getenv", "os.environ.get"):
            key = _const_str(node.args[0]) if node.args else None
            self._record_env(key, line)
            return

        # open() / pathlib writes.
        if name == "open":
            self._check_open(node)
            return
        if name.endswith(".write_text") or name.endswith(".write_bytes"):
            method = name.rsplit(".", 1)[-1]
            self.detected_writes.append(f"<computed path>.{method}")
            self._add("MEDIUM", "filesystem", line, f"{method}() writes to a path the scanner cannot resolve")
            return

    def _check_network_call(self, node: ast.Call, name: str) -> None:
        line = node.lineno
        # Extract a literal URL host from the first string-ish argument.
        host = None
        for arg in list(node.args) + [kw.value for kw in node.keywords if kw.arg in ("url", "base_url")]:
            s = _const_str(arg)
            if s:
                host = _host_of(s) or host
                if host:
                    break
        if not self.caps.network.enabled:
            # Covered by the import-time HIGH; record the host for the diff.
            if host:
                self.detected_hosts.add(host)
            return
        allow = self.caps.network.hosts
        if not allow:
            self._add("HIGH", "network", line, f"{name}() — network declared with an empty host allowlist (any host)")
            return
        if host:
            self.detected_hosts.add(host)
            if not any(_host_matches(host, p) for p in allow):
                self._add("HIGH", "network", line, f"{name}() targets '{host}', not in the declared host allowlist")

    def _check_open(self, node: ast.Call) -> None:
        line = node.lineno
        path_node = node.args[0] if node.args else None
        mode = "r"
        if len(node.args) >= 2:
            mode = _const_str(node.args[1]) or mode
        for kw in node.keywords:
            if kw.arg == "mode":
                mode = _const_str(kw.value) or mode
        is_write = any(c in mode for c in ("w", "a", "x", "+"))
        path = _const_str(path_node) if path_node is not None else None

        # Secret-store / config access regardless of mode.
        if path and any(marker in path.replace("\\", "/") for marker in ("secrets.json", ".secret_key", "config.json")):
            self._add("HIGH", "secret-access", line, f"open() references a sensitive path: {path!r}")
            return

        if is_write:
            desc = path if path else "<computed path>"
            self.detected_writes.append(desc)
            if path is None:
                self._add("HIGH", "filesystem", line, "open(..., write) with a computed path")
                return
            norm = _norm_write_path(path)
            declared = [_norm_write_path(p) for p in self.caps.filesystem.write_paths]
            if not any(norm == d or norm.startswith(d.rstrip("/") + "/") for d in declared):
                self._add("HIGH", "filesystem", line, f"open(..., write) to {path!r}, outside declared write_paths")
        else:
            # Reads outside the module dir: absolute paths or parent traversal.
            norm = path.replace("\\", "/") if path else ""
            is_abs = bool(path) and (path.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", path))
            out_of_tree = is_abs or (".." in norm.split("/") if path else False)
            if out_of_tree:
                self._add("MEDIUM", "filesystem", line, f"open() reads an out-of-tree path: {path!r}")

    # -- attribute / subscript reads (env) -------------------------------------

    def visit_Subscript(self, node: ast.Subscript) -> None:
        # os.environ['KEY']
        base = self._resolve(node.value)
        if base == "os.environ":
            key = _const_str(node.slice)
            self._record_env(key, node.lineno)
        self.generic_visit(node)

    def _record_env(self, key: str | None, line: int) -> None:
        if key is None:
            self._add("MEDIUM", "env", line, "dynamic os.environ access with a non-literal key")
            return
        self.detected_env.add(key)
        if key not in self.declared_env:
            self._add("HIGH", "env", line, f"reads undeclared environment variable {key!r}")

    # -- string literals (secret paths, opaque blobs) --------------------------

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str):
            v = node.value
            norm = v.replace("\\", "/")
            if any(marker in norm for marker in _SECRET_PATH_MARKERS):
                self._add("HIGH", "secret-access", node.lineno, f"references a sensitive path: {v[:60]!r}")
            elif "data/modules" in norm:
                self._add("MEDIUM", "cross-module", node.lineno, "references the modules directory")
            elif len(v) >= _OPAQUE_MIN_LEN and (_BASE64_RE.match(v) or _HEX_RE.match(v)):
                self._add("MEDIUM", "obfuscation", node.lineno, f"large opaque literal ({len(v)} chars)")
        self.generic_visit(node)


def scan_module(module_dir: Path, manifest: ModuleManifest) -> ScanReport:
    """Statically scan every .py file under module_dir against its manifest."""
    caps = _effective_caps(manifest)
    declared_env = {k for k in caps.env} | {s.key for s in manifest.secrets_required}

    report = ScanReport()
    detected_network = False
    detected_subprocess = False
    detected_hosts: set[str] = set()
    detected_writes: list[str] = []
    detected_env: set[str] = set()

    py_files = sorted(module_dir.rglob("*.py"))
    for path in py_files:
        rel = path.relative_to(module_dir).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"), filename=rel)
        except SyntaxError as e:
            report.parse_errors.append(f"{rel}: {e}")
            continue
        scanner = _FileScanner(rel, caps, declared_env)
        scanner.visit(tree)
        report.findings.extend(scanner.findings)
        detected_network |= scanner.detected_network
        detected_subprocess |= scanner.detected_subprocess
        detected_hosts |= scanner.detected_hosts
        detected_writes.extend(scanner.detected_writes)
        detected_env |= scanner.detected_env

    report.files_scanned = len(py_files)

    # Over-declaration (INFO): declared but never detected.
    def _over(detail: str) -> None:
        report.findings.append(
            Finding(severity="INFO", category="over-declared", file="manifest.json", line=0, detail=detail)
        )

    if caps.network.enabled and not detected_network:
        _over("network declared but no network usage detected")
    if caps.subprocess and not detected_subprocess:
        _over("subprocess declared but no subprocess usage detected")
    if caps.filesystem.write_paths and not detected_writes:
        _over("filesystem write_paths declared but no writes detected")
    for key in caps.env:
        if key not in detected_env:
            _over(f"env var {key!r} declared but never read")

    report.declared_vs_detected = {
        "network": {
            "declared": caps.network.enabled,
            "declared_hosts": list(caps.network.hosts),
            "detected": detected_network,
            "detected_hosts": sorted(detected_hosts),
        },
        "subprocess": {"declared": caps.subprocess, "detected": detected_subprocess},
        "filesystem": {
            "declared_write_paths": list(caps.filesystem.write_paths),
            "detected_writes": detected_writes,
        },
        "env": {"declared": sorted(declared_env), "detected": sorted(detected_env)},
    }

    counts = {"CRITICAL": 0, "HIGH": 0, "MEDIUM": 0, "INFO": 0}
    for f in report.findings:
        counts[f.severity] += 1
    report.summary = counts
    # Stable order: severity desc, then file/line.
    report.findings.sort(key=lambda f: (-_SEVERITY_RANK[f.severity], f.file, f.line))
    return report


def scan_summary(report: ScanReport) -> str:
    """Compact one-line summary for the audit record."""
    c = report.summary
    return (
        f"max={report.max_severity or 'none'} crit={c['CRITICAL']} "
        f"high={c['HIGH']} med={c['MEDIUM']} info={c['INFO']}"
    )
