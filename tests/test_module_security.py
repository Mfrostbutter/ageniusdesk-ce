"""Community module security pipeline: AST scanner + two-phase inspect/install.

The scanner tests are pure (build fixture dirs, scan, assert findings). The
install-flow tests drive the HTTP surface with a synthetic GitHub-style tarball,
monkeypatching the network download so no real repo is fetched.
"""

import io
import sqlite3
import tarfile

import pytest
from pydantic import ValidationError

from backend.module_registry import (
    COMMUNITY_MODULES_DIR,
    Capabilities,
    FilesystemCapability,
    HostBridgeCapability,
    ModuleManifest,
    NetworkCapability,
    SecretRequirement,
    is_valid_module_id,
)
from backend.modules.modules import installer
from backend.modules.modules.scanner import scan_module

OWNER = {"email": "owner@example.com", "password": "Fro5tbutt3r!"}


def _auth(client):
    """Establish (or recover) the owner session so privileged routes are reachable."""
    client.cookies.clear()
    r = client.post("/api/auth/setup", json=OWNER)
    if r.status_code == 409:
        r = client.post("/api/auth/login", json={"username": OWNER["email"], "password": OWNER["password"]})
    assert r.status_code in (200, 201), r.text
    return client


def _csrf(client) -> dict:
    """Double-submit CSRF header echoing the agd_csrf cookie set at login."""
    return {"x-agd-csrf": client.cookies.get("agd_csrf", "")}


# ── Module id validation (path-traversal hardening) ──────────────────────────

_UNSAFE_IDS = [
    "..", "../evil", "a/b", "a\\b", ".hidden", "Uppercase", "", "foo..bar", "x" * 65,
    "mod.v2", "a.",  # dots are banned (no '..', no Windows trailing-dot alias)
    "nul", "con", "aux", "prn", "com1", "com9", "lpt1", "lpt9",  # Windows reserved
]
_SAFE_IDS = ["youtube-research", "n8n_proxy", "a", "modv2", "a1-b_2", "com", "lpt", "con1"]


@pytest.mark.parametrize("bad", _UNSAFE_IDS)
def test_is_valid_module_id_rejects_unsafe(bad):
    assert is_valid_module_id(bad) is False


@pytest.mark.parametrize("good", _SAFE_IDS)
def test_is_valid_module_id_accepts_safe(good):
    assert is_valid_module_id(good) is True


@pytest.mark.parametrize("bad", _UNSAFE_IDS)
def test_manifest_rejects_unsafe_id(bad):
    with pytest.raises(ValidationError):
        ModuleManifest(id=bad, name="X")


@pytest.mark.parametrize("good", _SAFE_IDS)
def test_manifest_accepts_safe_id(good):
    assert ModuleManifest(id=good, name="X").id == good


@pytest.mark.parametrize("bad", ["..", "../evil", "a/b", ".hidden", ""])
def test_safe_community_dir_blocks_traversal(bad):
    # The '..' case is the live bug: data/modules/.. == data/, whose rmtree would
    # wipe the DB, secret store, and vault. The resolver must refuse it.
    with pytest.raises(RuntimeError):
        installer._safe_community_dir(bad)


def test_safe_community_dir_returns_contained_path():
    target = installer._safe_community_dir("youtube-research")
    base = COMMUNITY_MODULES_DIR.resolve()
    assert base in target.parents
    assert target.name == "youtube-research"


def test_uninstall_rejects_traversal_id():
    # Reaches uninstall() with a crafted id and asserts it raises before any
    # filesystem delete (the id never resolves to a path it could rmtree).
    with pytest.raises(RuntimeError):
        installer.uninstall("..")


# ── Scanner unit tests (against fixtures) ─────────────────────────────────────


def _write_module(tmp_path, manifest: ModuleManifest, code: str):
    d = tmp_path
    (d / "mod.py").write_text(code)
    return d


def test_scanner_benign_clean(tmp_path):
    d = _write_module(tmp_path, ModuleManifest(id="benign", name="Benign"), "def add(a, b):\n    return a + b\n")
    report = scan_module(d, ModuleManifest(id="benign", name="Benign"))
    assert report.findings == []
    assert report.max_severity is None
    assert report.files_scanned == 1


def test_scanner_obfuscated_exec_is_critical(tmp_path):
    code = "import base64\n" "def run(blob):\n" "    exec(base64.b64decode(blob))\n"
    d = _write_module(tmp_path, None, code)
    report = scan_module(d, ModuleManifest(id="evil", name="Evil"))
    assert report.has("CRITICAL")
    assert any(f.category == "code-exec" for f in report.findings)


def test_scanner_os_system_is_critical(tmp_path):
    d = _write_module(tmp_path, None, "import os\ndef run():\n    os.system('rm -rf /')\n")
    report = scan_module(d, ModuleManifest(id="sh", name="Sh"))
    assert report.has("CRITICAL")
    assert any(f.category == "shell-exec" for f in report.findings)


def test_scanner_dynamic_import_is_critical(tmp_path):
    d = _write_module(tmp_path, None, "import importlib\ndef run(name):\n    importlib.import_module(name)\n")
    report = scan_module(d, ModuleManifest(id="di", name="DI"))
    assert report.has("CRITICAL")
    assert any(f.category == "dynamic-import" for f in report.findings)


def test_scanner_undeclared_network_is_high(tmp_path):
    code = "import httpx\ndef run():\n    return httpx.get('https://api.example.com/x')\n"
    d = _write_module(tmp_path, None, code)
    # No capabilities block -> declares nothing -> network use is undeclared.
    report = scan_module(d, ModuleManifest(id="net", name="Net"))
    assert report.has("HIGH")
    diff = report.declared_vs_detected["network"]
    assert diff["detected"] is True
    assert diff["declared"] is False


def test_scanner_declared_network_host_allowlist(tmp_path):
    code = "import httpx\ndef run():\n    httpx.get('https://evil.example.com/x')\n"
    d = _write_module(tmp_path, None, code)
    caps = Capabilities(network=NetworkCapability(enabled=True, hosts=["*.youtube.com"]))
    report = scan_module(d, ModuleManifest(id="net2", name="Net2", capabilities=caps))
    # Network is declared, but the targeted host is not in the allowlist.
    assert report.has("HIGH")
    assert any("not in the declared host allowlist" in f.detail for f in report.findings)


def test_scanner_allowed_host_passes(tmp_path):
    code = "import httpx\ndef run():\n    httpx.get('https://www.youtube.com/watch')\n"
    d = _write_module(tmp_path, None, code)
    caps = Capabilities(network=NetworkCapability(enabled=True, hosts=["*.youtube.com"]))
    report = scan_module(d, ModuleManifest(id="net3", name="Net3", capabilities=caps))
    assert not report.has("HIGH")
    assert report.declared_vs_detected["network"]["detected_hosts"] == ["www.youtube.com"]


def test_scanner_out_of_dir_write_is_high(tmp_path):
    code = "def run():\n    open('/etc/passwd', 'w').write('x')\n"
    d = _write_module(tmp_path, None, code)
    caps = Capabilities(filesystem=FilesystemCapability(write_paths=["research"]))
    report = scan_module(d, ModuleManifest(id="fw", name="FW", capabilities=caps))
    assert report.has("HIGH")
    assert any(f.category == "filesystem" for f in report.findings)


def test_scanner_declared_write_path_passes(tmp_path):
    code = "def run():\n    open('data/research/out.json', 'w').write('{}')\n"
    d = _write_module(tmp_path, None, code)
    caps = Capabilities(filesystem=FilesystemCapability(write_paths=["research"]))
    report = scan_module(d, ModuleManifest(id="fw2", name="FW2", capabilities=caps))
    assert not report.has("HIGH")


def test_scanner_undeclared_env_is_high(tmp_path):
    code = "import os\ndef run():\n    return os.environ['SECRET_KEY']\n"
    d = _write_module(tmp_path, None, code)
    report = scan_module(d, ModuleManifest(id="env", name="Env"))
    assert report.has("HIGH")
    assert any(f.category == "env" for f in report.findings)


def test_scanner_declared_env_and_secret_pass(tmp_path):
    code = "import os\ndef run():\n    return os.environ['API_KEY'], os.getenv('WHISPER_URL')\n"
    d = _write_module(tmp_path, None, code)
    caps = Capabilities(env=["WHISPER_URL"])
    mf = ModuleManifest(id="env2", name="Env2", capabilities=caps, secrets_required=[SecretRequirement(key="API_KEY")])
    report = scan_module(d, mf)
    assert not report.has("HIGH")


def test_scanner_secret_path_access_is_high(tmp_path):
    d = _write_module(tmp_path, None, "def run():\n    open('data/secrets.json').read()\n")
    report = scan_module(d, ModuleManifest(id="sp", name="SP"))
    assert report.has("HIGH")
    assert any(f.category == "secret-access" for f in report.findings)


def test_scanner_over_declaration_is_info(tmp_path):
    d = _write_module(tmp_path, None, "def add(a, b):\n    return a + b\n")
    caps = Capabilities(network=NetworkCapability(enabled=True, hosts=["x.com"]), subprocess=True)
    report = scan_module(d, ModuleManifest(id="over", name="Over", capabilities=caps))
    # Declared network + subprocess but uses neither.
    assert not report.has("HIGH")
    info = [f for f in report.findings if f.severity == "INFO"]
    assert len(info) >= 2
    assert all(f.category == "over-declared" for f in info)


def test_scanner_host_import_is_high(tmp_path):
    # Under the isolation contract, importing the host (backend.*) reaches host
    # internals and won't run isolated -> HIGH, not INFO.
    code = "from backend.modules.notes import storage\ndef run():\n    return storage\n"
    d = _write_module(tmp_path, None, code)
    report = scan_module(d, ModuleManifest(id="h", name="H"))
    assert report.has("HIGH")
    assert any(f.category == "host-import" and f.severity == "HIGH" for f in report.findings)


def test_scanner_literal_dynamic_import_backend_is_high(tmp_path):
    # MEDIUM-3: a literal dynamic import of the host must raise host-import HIGH.
    # The non-literal CRITICAL guard previously let __import__("backend") through
    # with no finding at all.
    code = '__import__("backend").config.decrypt_value("$X")\n'
    d = _write_module(tmp_path, None, code)
    report = scan_module(d, ModuleManifest(id="li", name="LI"))
    assert any(f.category == "host-import" and f.severity == "HIGH" for f in report.findings)


def test_scanner_bridge_assistant_requires_capability(tmp_path):
    code = (
        "import httpx\n"
        "async def run():\n"
        "    return await httpx.AsyncClient().post('http://x/api/_host/assistant/complete', json={})\n"
    )
    net = Capabilities(network=NetworkCapability(enabled=True, hosts=["x"]))

    # host.assistant NOT declared -> HIGH undeclared-host.
    da = tmp_path / "undeclared"
    da.mkdir()
    _write_module(da, None, code)
    rep = scan_module(da, ModuleManifest(id="bnod", name="B", capabilities=net))
    assert any(f.category == "undeclared-host" and f.severity == "HIGH" for f in rep.findings)

    # host.assistant declared -> downgraded to INFO transparency, no undeclared finding.
    caps = Capabilities(network=NetworkCapability(enabled=True, hosts=["x"]),
                        host=HostBridgeCapability(assistant=True))
    db = tmp_path / "declared"
    db.mkdir()
    _write_module(db, None, code)
    rep2 = scan_module(db, ModuleManifest(id="bdec", name="C", capabilities=caps))
    assert any(f.category == "host-bridge" and f.severity == "INFO" for f in rep2.findings)
    assert not any(f.category == "undeclared-host" for f in rep2.findings)


def test_scanner_cross_community_import_is_medium(tmp_path):
    # Reaching into another community module (data/modules/*) stays MEDIUM.
    code = "from data.modules.other import thing\ndef run():\n    return thing\n"
    d = _write_module(tmp_path, None, code)
    report = scan_module(d, ModuleManifest(id="c", name="C"))
    assert report.has("MEDIUM")
    assert any(f.category == "cross-module" for f in report.findings)


def test_scanner_reports_parse_errors(tmp_path):
    (tmp_path / "broken.py").write_text("def f(:\n")
    report = scan_module(tmp_path, ModuleManifest(id="b", name="B"))
    assert report.parse_errors


# ── Install-flow tests (synthetic tarball, no network) ────────────────────────


def _tarball(manifest_json: str, code: str = "def noop():\n    return 1\n", top="pkg") -> bytes:
    """Build a GitHub-style tar.gz: one top-level dir with manifest + a .py file."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, content in (("manifest.json", manifest_json), ("module.py", code)):
            data = content.encode()
            info = tarfile.TarInfo(name=f"{top}/{name}")
            info.size = len(data)
            info.mtime = 0
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _files_tarball(files: dict, top="pkg") -> bytes:
    """Build a GitHub-style tar.gz from a {relative_path: content} map under one
    top-level dir. Lets a single tarball carry a monorepo of modules."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for rel, content in files.items():
            data = content.encode()
            info = tarfile.TarInfo(name=f"{top}/{rel}")
            info.size = len(data)
            info.mtime = 0
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _patch_download(monkeypatch, tarball: bytes, sha: str):
    async def fake_dl(owner, repo, ref):
        return tarball, sha

    monkeypatch.setattr(installer, "_download_tarball", fake_dl)


def _audit_rows(module_id: str):
    from backend.config import DB_FILE

    con = sqlite3.connect(str(DB_FILE))
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "SELECT * FROM module_installs WHERE module_id = ? ORDER BY id DESC", (module_id,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


BENIGN_MANIFEST = '{"id": "%s", "name": "%s", "min_app_version": "0.0.0"}'


def test_inspect_returns_report_without_registering(client, monkeypatch):
    _auth(client)
    sha = "aaaa111122223333aaaa111122223333aaaa1111"
    _patch_download(monkeypatch, _tarball(BENIGN_MANIFEST % ("insp1", "Inspect One")), sha)

    r = client.post("/api/modules/inspect", json={"repo": "x/insp1", "ref": "main"}, headers=_csrf(client))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["resolved_sha"] == sha
    assert body["manifest"]["id"] == "insp1"
    assert "scan_report" in body and "declared_vs_detected" in body["scan_report"]

    # Nothing was registered or written to disk.
    assert client.get("/api/modules/insp1").status_code == 404
    assert not (installer.COMMUNITY_MODULES_DIR / "insp1").exists()


def test_install_benign_records_audit(client, monkeypatch):
    _auth(client)
    sha = "bbbb111122223333bbbb111122223333bbbb1111"
    _patch_download(monkeypatch, _tarball(BENIGN_MANIFEST % ("inst1", "Install One")), sha)

    r = client.post(
        "/api/modules/install",
        json={"repo": "x/inst1", "ref": "main", "resolved_sha": sha, "consent": {}},
        headers=_csrf(client),
    )
    assert r.status_code == 200, r.text
    assert r.json()["scan_max_severity"] == "none"
    assert (installer.COMMUNITY_MODULES_DIR / "inst1").exists()

    rows = _audit_rows("inst1")
    assert len(rows) == 1
    assert rows[0]["approved_by"] == OWNER["email"]
    assert rows[0]["resolved_sha"] == sha


def test_install_rejects_sha_mismatch(client, monkeypatch):
    _auth(client)
    sha = "cccc111122223333cccc111122223333cccc1111"
    _patch_download(monkeypatch, _tarball(BENIGN_MANIFEST % ("inst2", "Install Two")), sha)

    r = client.post(
        "/api/modules/install",
        json={"repo": "x/inst2", "ref": "main", "resolved_sha": "deadbeefdeadbeef", "consent": {}},
        headers=_csrf(client),
    )
    assert r.status_code == 400
    assert "different commit" in r.json()["detail"]
    assert not (installer.COMMUNITY_MODULES_DIR / "inst2").exists()


def test_install_critical_requires_typed_confirmation(client, monkeypatch):
    _auth(client)
    sha = "dddd111122223333dddd111122223333dddd1111"
    code = "def run():\n    exec('print(1)')\n"
    _patch_download(monkeypatch, _tarball(BENIGN_MANIFEST % ("inst3", "Install Three"), code), sha)

    # Without the typed id, a CRITICAL finding blocks install.
    r = client.post(
        "/api/modules/install",
        json={"repo": "x/inst3", "ref": "main", "resolved_sha": sha, "consent": {}},
        headers=_csrf(client),
    )
    assert r.status_code == 400
    assert "CRITICAL" in r.json()["detail"]
    assert not (installer.COMMUNITY_MODULES_DIR / "inst3").exists()

    # Typing the module id proceeds.
    r = client.post(
        "/api/modules/install",
        json={"repo": "x/inst3", "ref": "main", "resolved_sha": sha, "consent": {"typed_id": "inst3"}},
        headers=_csrf(client),
    )
    assert r.status_code == 200, r.text
    assert r.json()["scan_max_severity"] == "CRITICAL"


def test_install_high_requires_acknowledgement(client, monkeypatch):
    _auth(client)
    sha = "eeee111122223333eeee111122223333eeee1111"
    code = "import httpx\ndef run():\n    return httpx.get('https://api.example.com')\n"
    _patch_download(monkeypatch, _tarball(BENIGN_MANIFEST % ("inst4", "Install Four"), code), sha)

    r = client.post(
        "/api/modules/install",
        json={"repo": "x/inst4", "ref": "main", "resolved_sha": sha, "consent": {}},
        headers=_csrf(client),
    )
    assert r.status_code == 400
    assert "HIGH" in r.json()["detail"]

    r = client.post(
        "/api/modules/install",
        json={"repo": "x/inst4", "ref": "main", "resolved_sha": sha, "consent": {"acknowledged": True}},
        headers=_csrf(client),
    )
    assert r.status_code == 200, r.text
    assert r.json()["scan_max_severity"] == "HIGH"


def test_install_requires_auth(anon, monkeypatch):
    sha = "ffff111122223333ffff111122223333ffff1111"
    _patch_download(monkeypatch, _tarball(BENIGN_MANIFEST % ("inst5", "Install Five")), sha)
    r = anon.post(
        "/api/modules/install",
        json={"repo": "x/inst5", "ref": "main", "resolved_sha": sha, "consent": {}},
    )
    assert r.status_code == 401


# ── Monorepo discover + subdir install ────────────────────────────────────────

CODE = "def noop():\n    return 1\n"


def _monorepo_tarball():
    """A repo with two modules under modules/, no root manifest."""
    return _files_tarball(
        {
            "README.md": "# community modules\n",
            "modules/youtube-research/manifest.json": BENIGN_MANIFEST % ("youtube-research", "YouTube Research"),
            "modules/youtube-research/module.py": CODE,
            "modules/widget/manifest.json": BENIGN_MANIFEST % ("widget", "Widget"),
            "modules/widget/module.py": CODE,
        }
    )


def test_discover_lists_monorepo_modules(client, monkeypatch):
    _auth(client)
    sha = "a0a0111122223333a0a0111122223333a0a01111"
    _patch_download(monkeypatch, _monorepo_tarball(), sha)

    r = client.post("/api/modules/discover", json={"repo": "x/community", "ref": "main"}, headers=_csrf(client))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["resolved_sha"] == sha
    paths = {m["path"]: m["id"] for m in body["modules"]}
    assert paths == {"modules/widget": "widget", "modules/youtube-research": "youtube-research"}


def test_discover_single_root_module(client, monkeypatch):
    _auth(client)
    sha = "b0b0111122223333b0b0111122223333b0b01111"
    _patch_download(monkeypatch, _tarball(BENIGN_MANIFEST % ("solo", "Solo")), sha)

    r = client.post("/api/modules/discover", json={"repo": "x/solo", "ref": "main"}, headers=_csrf(client))
    assert r.status_code == 200, r.text
    mods = r.json()["modules"]
    assert len(mods) == 1 and mods[0]["path"] == "" and mods[0]["id"] == "solo"


def test_inspect_and_install_subdir(client, monkeypatch):
    _auth(client)
    sha = "c0c0111122223333c0c0111122223333c0c01111"
    _patch_download(monkeypatch, _monorepo_tarball(), sha)

    # Inspect a specific module by path.
    r = client.post(
        "/api/modules/inspect",
        json={"repo": "x/community", "ref": "main", "path": "modules/youtube-research"},
        headers=_csrf(client),
    )
    assert r.status_code == 200, r.text
    assert r.json()["manifest"]["id"] == "youtube-research"
    assert r.json()["path"] == "modules/youtube-research"

    # Install that subdir; only it lands on disk, the sibling does not.
    r = client.post(
        "/api/modules/install",
        json={
            "repo": "x/community",
            "ref": "main",
            "path": "modules/youtube-research",
            "resolved_sha": sha,
            "consent": {},
        },
        headers=_csrf(client),
    )
    assert r.status_code == 200, r.text
    assert r.json()["id"] == "youtube-research"
    assert (installer.COMMUNITY_MODULES_DIR / "youtube-research").exists()
    assert (installer.COMMUNITY_MODULES_DIR / "youtube-research" / "module.py").exists()
    assert not (installer.COMMUNITY_MODULES_DIR / "widget").exists()
    # Staging dir was cleaned up (no leftover .stage-* dirs).
    assert not list(installer.COMMUNITY_MODULES_DIR.glob(".stage-*"))


def test_static_serves_from_static_subdir(client):
    from backend.modules.modules import static_router

    static_dir = installer.COMMUNITY_MODULES_DIR / "statictest" / "static"
    static_dir.mkdir(parents=True, exist_ok=True)
    (static_dir / "view.html").write_text("<div>hello-static</div>")

    r = client.get("/modules/statictest/static/view.html")
    assert r.status_code == 200
    assert "hello-static" in r.text

    # The community loader probes the script URL with HEAD before injecting it,
    # so HEAD must succeed too (not just GET).
    assert client.head("/modules/statictest/static/view.html").status_code == 200

    # A file at the module root (outside static/) is not served.
    (installer.COMMUNITY_MODULES_DIR / "statictest" / "secret.py").write_text("x = 1\n")
    assert client.get("/modules/statictest/static/secret.py").status_code == 404

    # Traversal out of the static dir is blocked at resolve time.
    import fastapi

    try:
        static_router._safe_resolve("statictest", "../secret.py")
        raised = False
    except fastapi.HTTPException:
        raised = True
    assert raised


def test_install_rejects_path_traversal(client, monkeypatch):
    _auth(client)
    sha = "d0d0111122223333d0d0111122223333d0d01111"
    _patch_download(monkeypatch, _monorepo_tarball(), sha)
    r = client.post(
        "/api/modules/install",
        json={
            "repo": "x/community",
            "ref": "main",
            "path": "../../../etc",
            "resolved_sha": sha,
            "consent": {},
        },
        headers=_csrf(client),
    )
    assert r.status_code == 400
    assert "path" in r.json()["detail"].lower()
