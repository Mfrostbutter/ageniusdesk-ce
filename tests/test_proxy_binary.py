"""Reverse proxy: binary response proxying + identity header forwarding.

Drives proxy.py against a fake worker (httpx MockTransport) so XLSX/ZIP
downloads, multi-megabyte streaming, safe-header forwarding, and upstream
stream closure are covered without spawning a subprocess.
"""

import asyncio

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from backend.modules._runtime import proxy, supervisor

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
BIG = b"\x00PK" + b"z" * (3 * 1024 * 1024)


class _Stream(httpx.AsyncByteStream):
    """Upstream body stream that records whether it was closed."""

    closed = False

    def __init__(self, chunks, fail_after=None):
        self._chunks, self._fail_after = chunks, fail_after

    async def __aiter__(self):
        for i, c in enumerate(self._chunks):
            if self._fail_after is not None and i >= self._fail_after:
                raise RuntimeError("upstream died")
            yield c

    async def aclose(self):
        _Stream.closed = True


def _handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/api/fake/export.xlsx":
        return httpx.Response(200, stream=_Stream([b"PK\x03\x04", b"\x00" * 10]), headers={
            "content-type": XLSX,
            "content-disposition": 'attachment; filename="cluster-inventory.xlsx"',
            "set-cookie": "agd_session=evil",
            "clear-site-data": '"*"',
            "www-authenticate": "Basic",
            "authorization": "Bearer leak",
        })
    if path == "/api/fake/export.zip":
        return httpx.Response(200, stream=_Stream([BIG[i:i + 65536] for i in range(0, len(BIG), 65536)]),
                              headers={"content-type": "application/zip",
                                       "content-disposition": 'attachment; filename="export.zip"'})
    if path == "/api/fake/broken":
        return httpx.Response(200, stream=_Stream([b"a", b"b", b"c"], fail_after=1),
                              headers={"content-type": "application/zip"})
    if path == "/api/fake/headers":
        import json
        body = json.dumps({"headers": dict(request.headers)}).encode()
        return httpx.Response(200, stream=_Stream([body]), headers={"content-type": "application/json"})
    return httpx.Response(404)


class _FakeWorker:
    proxy_secret = "s3cret"

    def __init__(self):
        self._client = None
        self._loop = None

    def is_alive(self):
        return True

    @property
    def client(self):
        loop = asyncio.get_running_loop()
        if self._client is None or self._loop is not loop:
            self._client = httpx.AsyncClient(transport=httpx.MockTransport(_handler), base_url="http://worker")
            self._loop = loop
        return self._client


@pytest.fixture
def app(monkeypatch):
    worker = _FakeWorker()
    monkeypatch.setattr(supervisor, "get", lambda mid: worker if mid == "fake" else None)
    app = FastAPI()

    @app.middleware("http")
    async def stamp_identity(request: Request, call_next):
        # Mimics identity.apply for the /stamped prefix only.
        if request.headers.get("x-test-stamp"):
            from backend.modules._runtime import identity
            identity.strip_trusted_headers(request.scope)
            identity.inject_identity(request.scope, {"username": "op", "role": "operator", "source": "session"})
            request.scope["agd_identity"] = {"username": "op"}
        return await call_next(request)

    proxy.register_proxy_route(app, "fake")
    _Stream.closed = False
    return app


def test_xlsx_download_forwards_type_and_disposition_not_auth_headers(app):
    with TestClient(app) as c:
        r = c.get("/api/fake/export.xlsx")
    assert r.status_code == 200
    assert r.headers["content-type"] == XLSX
    assert r.headers["content-disposition"] == 'attachment; filename="cluster-inventory.xlsx"'
    assert r.content.startswith(b"PK\x03\x04") and len(r.content) == 14
    lower = {k.lower() for k in r.headers}
    assert not ({"set-cookie", "clear-site-data", "www-authenticate"} & lower)
    assert "agd_session" not in r.cookies
    assert _Stream.closed is True  # upstream closed on success


def test_multi_megabyte_zip_streams_intact(app):
    with TestClient(app) as c:
        r = c.get("/api/fake/export.zip")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    assert len(r.content) == len(BIG) and r.content == BIG
    assert _Stream.closed is True


def test_upstream_failure_mid_stream_still_closes(app):
    with TestClient(app) as c:
        with pytest.raises(Exception):
            c.get("/api/fake/broken")
    assert _Stream.closed is True


def test_unstamped_x_agd_headers_are_dropped(app):
    with TestClient(app) as c:
        r = c.get("/api/fake/headers", headers={"X-AGD-User": "spoof", "X-AGD-Role": "admin", "X-Keep": "1"})
    got = r.json()["headers"]
    assert "x-agd-user" not in got and "x-agd-role" not in got
    assert got["x-keep"] == "1" and got["x-agd-proxy-secret"] == "s3cret"


def test_stamped_identity_is_forwarded_and_spoof_replaced(app):
    with TestClient(app) as c:
        r = c.get("/api/fake/headers", headers={"X-Test-Stamp": "1", "X-AGD-User": "spoof", "X-AGD-Role": "admin"})
    got = r.json()["headers"]
    assert got["x-agd-user"] == "op" and got["x-agd-role"] == "operator"
    assert "x-test-stamp" in got and "cookie" not in got and "authorization" not in got
