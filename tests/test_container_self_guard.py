"""The dashboard must not destroy / stop / restart / recreate its OWN container
from inside the app — that is a Docker-Desktop / host operation. The router
guards those actions on the self-container; harmless actions (start/unpause) and
other containers are unaffected.

is_self_container is stubbed (real detection needs a live Docker socket); we test
the routing/guard behavior around it.
"""

import pytest

# docker_mgr (and the /api/containers routes) only load when aiodocker is present.
# The base test venv omits it, so skip there; this runs in full-dependency envs.
pytest.importorskip("aiodocker")

import backend.auth_gate as auth_gate  # noqa: E402
import backend.modules.docker_mgr.client as dc  # noqa: E402


def _operator(monkeypatch):
    async def _fake(_request):
        return {"username": "op", "source": "session", "role": "operator", "email": None}

    monkeypatch.setattr(auth_gate, "current_user", _fake)


def _self(monkeypatch, value: bool):
    async def _fake(_cid):
        return value

    monkeypatch.setattr(dc, "is_self_container", _fake)


def test_destroy_self_blocked(anon, monkeypatch):
    _operator(monkeypatch)
    _self(monkeypatch, True)
    r = anon.delete("/api/containers/selfid")
    assert r.status_code == 403
    assert "docker desktop" in r.json()["detail"].lower()


def test_stop_self_blocked(anon, monkeypatch):
    _operator(monkeypatch)
    _self(monkeypatch, True)
    assert anon.post("/api/containers/selfid/stop").status_code == 403


def test_restart_self_blocked(anon, monkeypatch):
    _operator(monkeypatch)
    _self(monkeypatch, True)
    assert anon.post("/api/containers/selfid/restart").status_code == 403


def test_pause_self_blocked(anon, monkeypatch):
    _operator(monkeypatch)
    _self(monkeypatch, True)
    assert anon.post("/api/containers/selfid/pause").status_code == 403


def test_recreate_self_blocked(anon, monkeypatch):
    _operator(monkeypatch)
    _self(monkeypatch, True)
    assert anon.post("/api/containers/selfid/recreate").status_code == 403


def test_start_self_not_guarded(anon, monkeypatch):
    # start can't take the dashboard down (it's already up) -> not guarded; it
    # fails later on the unavailable test daemon, not with 403.
    _operator(monkeypatch)
    _self(monkeypatch, True)
    assert anon.post("/api/containers/selfid/start").status_code != 403


def test_destroy_other_container_not_blocked(anon, monkeypatch):
    _operator(monkeypatch)
    _self(monkeypatch, False)
    # passes the self-guard; docker is unavailable in tests -> some non-403 error
    assert anon.delete("/api/containers/otherid").status_code != 403
