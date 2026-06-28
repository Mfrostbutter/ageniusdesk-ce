"""Phase 3: the host capability bridge (notes.* namespace + token/scoping).

Drives bridge_app directly with a minted per-module token and asserts the
security contract: token required, browser cookies rejected, and every notes
operation validated + scoped to the module's declared vault paths on the host
(never trusted from the caller).
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.module_registry import Capabilities, FilesystemCapability, HostBridgeCapability
from backend.modules._runtime import bridge
from backend.modules.notes import storage


def _try_symlink(link: Path, target: Path, *, dir_link: bool = True) -> None:
    """Create a symlink or skip the test (Windows needs privilege/dev mode)."""
    if link.is_symlink() or link.exists():
        link.unlink()
    try:
        link.symlink_to(target, target_is_directory=dir_link)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted on this platform")


@pytest.fixture
def bridge_client():
    storage.ensure_vault()
    client = TestClient(bridge.bridge_app)
    issued: list[str] = []

    def _mint(write_paths=None, read_paths=None, host_assistant=False):
        caps = Capabilities(
            filesystem=FilesystemCapability(write_paths=write_paths or [], read_paths=read_paths or []),
            host=HostBridgeCapability(assistant=host_assistant),
        )
        token = bridge.mint("testmod", caps)
        issued.append(token)
        return token

    yield client, _mint
    for t in issued:
        bridge.revoke(t)


def _h(token):
    return {"authorization": f"Bearer {token}"}


# ── Auth ──────────────────────────────────────────────────────────────────────


def test_requires_token(bridge_client):
    client, _ = bridge_client
    assert client.post("/api/_host/notes/read", json={"path": "research/x"}).status_code == 401
    assert client.post("/api/_host/notes/read", json={"path": "research/x"},
                       headers={"authorization": "Bearer nope"}).status_code == 401


def test_rejects_cookie_bearing_request(bridge_client):
    client, mint = bridge_client
    token = mint(write_paths=["research"])
    r = client.post("/api/_host/notes/read", json={"path": "research/x"},
                    headers={**_h(token), "cookie": "agd_session=abc"})
    assert r.status_code == 403


# ── notes write/read scoping ──────────────────────────────────────────────────


def test_write_and_read_in_scope(bridge_client):
    client, mint = bridge_client
    token = mint(write_paths=["research"])
    w = client.post("/api/_host/notes/write",
                    json={"path": "research/demo/note.md", "content": "hello"}, headers=_h(token))
    assert w.status_code == 200, w.text
    r = client.post("/api/_host/notes/read", json={"path": "research/demo/note.md"}, headers=_h(token))
    assert r.status_code == 200
    assert r.json()["content"] == "hello"


def test_write_outside_scope_forbidden(bridge_client):
    client, mint = bridge_client
    token = mint(write_paths=["research"])
    r = client.post("/api/_host/notes/write",
                    json={"path": "user/secret.md", "content": "x"}, headers=_h(token))
    assert r.status_code == 403


def test_scope_is_segment_aware(bridge_client):
    client, mint = bridge_client
    token = mint(write_paths=["research"])
    # 'research-evil' must NOT be treated as under 'research'.
    r = client.post("/api/_host/notes/write",
                    json={"path": "research-evil/x.md", "content": "x"}, headers=_h(token))
    assert r.status_code == 403


def test_traversal_rejected(bridge_client):
    client, mint = bridge_client
    token = mint(write_paths=["research"])
    for bad in ["../../etc/passwd", "research/../../../etc/passwd", "research\\..\\x"]:
        r = client.post("/api/_host/notes/write", json={"path": bad, "content": "x"}, headers=_h(token))
        assert r.status_code in (400, 403), f"{bad!r} -> {r.status_code}"


def test_read_includes_write_scope_but_not_others(bridge_client):
    client, mint = bridge_client
    token = mint(write_paths=["research"])  # no separate read_paths
    client.post("/api/_host/notes/write",
                json={"path": "research/a.md", "content": "a"}, headers=_h(token))
    # readable because it's writable
    assert client.post("/api/_host/notes/read", json={"path": "research/a.md"}, headers=_h(token)).status_code == 200
    # not readable outside any declared path
    assert client.post("/api/_host/notes/read", json={"path": "user/a.md"}, headers=_h(token)).status_code == 403


def test_read_only_path(bridge_client):
    client, mint = bridge_client
    token = mint(write_paths=["research"], read_paths=["shared"])
    # can read 'shared' but not write it
    assert client.post("/api/_host/notes/write",
                       json={"path": "shared/x.md", "content": "x"}, headers=_h(token)).status_code == 403
    # reading a missing note in-scope is 404 (in scope), not 403
    assert client.post("/api/_host/notes/read",
                       json={"path": "shared/missing.md"}, headers=_h(token)).status_code == 404


# ── notes.append (write-scoped) ───────────────────────────────────────────────


def test_append_creates_and_grows(bridge_client):
    client, mint = bridge_client
    token = mint(write_paths=["research"])
    assert client.post("/api/_host/notes/append",
                       json={"path": "research/app.md", "content": "line1"}, headers=_h(token)).status_code == 200
    client.post("/api/_host/notes/append", json={"path": "research/app.md", "content": "line2"}, headers=_h(token))
    body = client.post("/api/_host/notes/read", json={"path": "research/app.md"}, headers=_h(token)).json()["content"]
    assert "line1" in body and "line2" in body


def test_append_outside_scope_forbidden(bridge_client):
    client, mint = bridge_client
    token = mint(write_paths=["research"])
    assert client.post("/api/_host/notes/append",
                       json={"path": "user/x.md", "content": "x"}, headers=_h(token)).status_code == 403


def test_append_size_limit(bridge_client):
    client, mint = bridge_client
    token = mint(write_paths=["research"])
    big = "a" * (bridge.MAX_NOTE_BYTES + 1)
    assert client.post("/api/_host/notes/append",
                       json={"path": "research/big.md", "content": big}, headers=_h(token)).status_code == 413


# ── notes.search (results scoped to read paths) ───────────────────────────────


def test_search_scoped_to_read_paths(bridge_client):
    client, mint = bridge_client
    seed = mint(write_paths=["research", "user"])
    term = "zzquniquesearchterm"
    client.post("/api/_host/notes/write",
                json={"path": "research/find.md", "content": f"hello {term} world"}, headers=_h(seed))
    client.post("/api/_host/notes/write",
                json={"path": "user/secret.md", "content": f"private {term} data"}, headers=_h(seed))
    token = mint(read_paths=["research"])  # cannot see user/
    r = client.post("/api/_host/notes/search", json={"query": term}, headers=_h(token))
    assert r.status_code == 200, r.text
    paths = [x["path"] for x in r.json()["results"]]
    assert "research/find.md" in paths
    assert not any(p.startswith("user/") for p in paths)


def test_search_requires_read_scope(bridge_client):
    client, mint = bridge_client
    token = mint()  # no read paths
    r = client.post("/api/_host/notes/search", json={"query": "anything"}, headers=_h(token))
    assert r.status_code == 200
    assert r.json()["results"] == []


# ── folders ───────────────────────────────────────────────────────────────────


def test_make_and_list_folders_and_files(bridge_client):
    client, mint = bridge_client
    token = mint(write_paths=["research"])
    assert client.post("/api/_host/notes/make-folder",
                       json={"rel": "research/topicX"}, headers=_h(token)).status_code == 200
    client.post("/api/_host/notes/write",
                json={"path": "research/file1.md", "content": "f"}, headers=_h(token))
    folders = client.post("/api/_host/notes/list-folders", json={"rel": "research"}, headers=_h(token)).json()
    assert "topicX" in folders["folders"]
    files = client.post("/api/_host/notes/list-files", json={"rel": "research"}, headers=_h(token)).json()
    assert "file1.md" in files["files"]


def test_make_folder_outside_scope_forbidden(bridge_client):
    client, mint = bridge_client
    token = mint(write_paths=["research"])
    assert client.post("/api/_host/notes/make-folder",
                       json={"rel": "user/evil"}, headers=_h(token)).status_code == 403


# ── HIGH-1: a symlink inside the vault must not defeat scoping ─────────────────


def test_symlink_escape_blocked(bridge_client):
    client, mint = bridge_client
    token = mint(write_paths=["research"], read_paths=["research"])
    research = storage.VAULT_DIR / "research"
    research.mkdir(parents=True, exist_ok=True)
    (storage.VAULT_DIR / "user").mkdir(parents=True, exist_ok=True)
    # research/evil -> ../user : an in-scope-looking path that lands out of scope.
    link = research / "evil"
    _try_symlink(link, Path("..") / "user")
    try:
        # Write THROUGH the symlink would land in user/symesc.md (out of scope).
        w = client.post("/api/_host/notes/write",
                        json={"path": "research/evil/symesc.md", "content": "x"}, headers=_h(token))
        assert w.status_code == 403, w.text
        assert not (storage.VAULT_DIR / "user" / "symesc.md").exists()  # nothing written
        # Read and make-folder through the symlink are likewise refused.
        assert client.post("/api/_host/notes/read",
                           json={"path": "research/evil/symesc.md"}, headers=_h(token)).status_code == 403
        assert client.post("/api/_host/notes/make-folder",
                           json={"rel": "research/evil/sub"}, headers=_h(token)).status_code == 403
    finally:
        if link.is_symlink() or link.exists():
            link.unlink()


def test_list_excludes_symlinks(bridge_client):
    client, mint = bridge_client
    token = mint(read_paths=["research"])
    research = storage.VAULT_DIR / "research"
    research.mkdir(parents=True, exist_ok=True)
    (research / "realdir").mkdir(exist_ok=True)
    (storage.VAULT_DIR / "user").mkdir(parents=True, exist_ok=True)
    link = research / "linkdir"
    _try_symlink(link, Path("..") / "user")
    try:
        folders = client.post("/api/_host/notes/list-folders",
                              json={"rel": "research"}, headers=_h(token)).json()["folders"]
        assert "realdir" in folders
        assert "linkdir" not in folders  # symlink dir omitted
    finally:
        if link.is_symlink() or link.exists():
            link.unlink()


# ── LOW-1: per-write size cap ─────────────────────────────────────────────────


def test_write_size_limit(bridge_client):
    client, mint = bridge_client
    token = mint(write_paths=["research"])
    big = "a" * (bridge.MAX_NOTE_BYTES + 1)
    r = client.post("/api/_host/notes/write",
                    json={"path": "research/big.md", "content": big}, headers=_h(token))
    assert r.status_code == 413


# ── Token lifecycle: revoke + empty-caps deny ─────────────────────────────────


def test_revoked_token_rejected(bridge_client):
    client, mint = bridge_client
    token = mint(write_paths=["research"])
    assert client.post("/api/_host/notes/write",
                       json={"path": "research/a.md", "content": "x"}, headers=_h(token)).status_code == 200
    bridge.revoke_module("testmod")  # the fixture mints under module_id "testmod"
    assert client.post("/api/_host/notes/write",
                       json={"path": "research/a.md", "content": "x"}, headers=_h(token)).status_code == 401


def test_empty_caps_deny_every_endpoint(bridge_client):
    client, mint = bridge_client
    token = mint()  # no write/read paths, host.assistant False
    assert client.post("/api/_host/notes/write",
                       json={"path": "research/a.md", "content": "x"}, headers=_h(token)).status_code == 403
    assert client.post("/api/_host/notes/read",
                       json={"path": "research/a.md"}, headers=_h(token)).status_code == 403
    assert client.post("/api/_host/notes/list-folders",
                       json={"rel": "research"}, headers=_h(token)).status_code == 403
    assert client.post("/api/_host/assistant/complete",
                       json={"user": "hi"}, headers=_h(token)).status_code == 403


# ── assistant.complete (capability gate + max_tokens clamp) ──────────────────


def test_assistant_complete_requires_capability(bridge_client):
    client, mint = bridge_client
    token = mint(write_paths=["research"])  # host.assistant NOT declared
    r = client.post("/api/_host/assistant/complete", json={"user": "hi"}, headers=_h(token))
    assert r.status_code == 403


def test_assistant_complete_calls_executor_and_clamps(bridge_client, monkeypatch):
    client, mint = bridge_client
    token = mint(write_paths=["research"], host_assistant=True)

    captured = {}

    async def _fake_complete(system, user, *, model="", max_tokens=8000):
        captured.update(system=system, user=user, model=model, max_tokens=max_tokens)
        return "completion-text"

    from backend.modules.assistant import completion
    monkeypatch.setattr(completion, "complete", _fake_complete)

    r = client.post("/api/_host/assistant/complete",
                    json={"system": "s", "user": "u", "model": "m", "max_tokens": 10_000_000}, headers=_h(token))
    assert r.status_code == 200
    assert r.json()["text"] == "completion-text"
    # bridge clamps the caller's max_tokens to the hard ceiling before dispatch
    assert captured["max_tokens"] == completion.HARD_MAX_TOKENS
    assert captured["user"] == "u"


# ── End-to-end: a real worker subprocess calls back through the bridge ─────────

_BRIDGE_MOD = '''
import os
import httpx
from fastapi import APIRouter

router = APIRouter(prefix="/api/bridgemod")


@router.get("/write")
async def do_write(path: str = "research/from-worker.md"):
    url = os.environ["AGD_BRIDGE_URL"]
    tok = os.environ["AGD_BRIDGE_TOKEN"]
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.post(f"{url}/api/_host/notes/write",
                         json={"path": path, "content": "via-bridge"},
                         headers={"authorization": f"Bearer {tok}"})
    return {"status": r.status_code}
'''


def test_worker_calls_bridge_end_to_end(tmp_path_factory):
    import threading
    import time

    import httpx
    import uvicorn

    from backend.module_registry import Capabilities, FilesystemCapability
    from backend.modules._runtime import supervisor

    storage.ensure_vault()
    port = bridge._ensure_port()
    server = uvicorn.Server(uvicorn.Config(bridge.bridge_app, host="127.0.0.1", port=port, log_level="warning"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)

    parent = tmp_path_factory.mktemp("bridgemods")
    moddir = parent / "bridgemod"
    moddir.mkdir()
    (moddir / "__init__.py").write_text(_BRIDGE_MOD)
    caps = Capabilities(filesystem=FilesystemCapability(write_paths=["research"]))
    worker = supervisor.start_worker("bridgemod", parent, capabilities=caps)
    try:
        transport = httpx.HTTPTransport(uds=worker.uds_path) if supervisor.USE_UDS else httpx.HTTPTransport()
        with httpx.Client(transport=transport, base_url=worker.base_url, timeout=15) as c:
            sec = {"x-agd-proxy-secret": worker.proxy_secret}
            # In-scope write succeeds end to end and lands in the host vault.
            ok = c.get("/api/bridgemod/write", headers=sec)
            assert ok.status_code == 200, ok.text
            assert ok.json()["status"] == 200
            assert storage.read("research/from-worker.md") == "via-bridge"
            # Out-of-scope write is refused by the bridge (403), not written.
            bad = c.get("/api/bridgemod/write", params={"path": "user/evil.md"}, headers=sec)
            assert bad.json()["status"] == 403
    finally:
        worker.stop()
        server.should_exit = True
