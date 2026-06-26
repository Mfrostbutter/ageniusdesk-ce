"""Shared pytest fixtures.

Isolation: the app uses module-level relative paths (DATA_DIR = Path("data")),
so we chdir into a throwaway directory *before* anything under `backend` is
imported. Every secret key, SQLite DB, and users.json the tests create then
lands in the temp tree, never the developer's real `data/`.
"""

import os
import sys
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# `backend` lives at the repo root. Pin it on sys.path with an absolute path
# captured before we chdir, so imports resolve regardless of the working dir or
# whether the project is installed editable in the active interpreter.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# --- Hermetic working directory (must happen before backend is imported) ------
_TMP_ROOT = tempfile.mkdtemp(prefix="agd-tests-")
os.chdir(_TMP_ROOT)
(Path(_TMP_ROOT) / "data").mkdir(exist_ok=True)
(Path(_TMP_ROOT) / "data" / "modules").mkdir(exist_ok=True)
(Path(_TMP_ROOT) / "data" / "themes").mkdir(exist_ok=True)

# Controlled auth posture: login enforced (default), no proxy trust, no tokens.
for _k in ("AGD_DISABLE_LOGIN", "AGD_REQUIRE_AUTH", "AGD_ADMIN_TOKEN",
           "AGD_TRUST_EDGE_AUTH", "AGD_TRUST_FORWARDED_FOR", "AGD_WEBHOOK_TOKEN"):
    os.environ.pop(_k, None)


@pytest.fixture(scope="session")
def client():
    """One app instance for the whole session.

    Entering the TestClient context runs the FastAPI lifespan once, which creates
    the SQLite tables (sessions, auth_resets, etc.) the auth tests rely on.
    """
    from backend.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture
def anon(client):
    """A client guaranteed to carry no session cookie for this test."""
    client.cookies.clear()
    return client
