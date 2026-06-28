"""Pristine harness seed docs (README.md / AGENTS.md) are refreshed to the
current seed on boot, but operator-edited ones are never clobbered.

Tests the mechanism with patched known-prior hash sets, so they don't couple to
specific historical seed content.
"""

import hashlib

import pytest

from backend.modules.assistant.baseline import loader, seed
from backend.modules.notes import storage
from backend.modules.notes.parser import parse_frontmatter


@pytest.fixture
def vault(tmp_path, monkeypatch):
    v = tmp_path / "workspace"
    monkeypatch.setattr(storage, "VAULT_DIR", v)
    v.mkdir(parents=True)
    return v


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _agents(version: int, body: str) -> str:
    return f"---\nversion: {version}\nupdated: 2026-01-01T00:00:00Z\noverrideable_sections: []\n---\n{body}"


# ── README ───────────────────────────────────────────────────────────────────


def test_readme_refreshed_when_pristine(vault, monkeypatch):
    old = "OLD PRISTINE README\n"
    (vault / "README.md").write_text(old, encoding="utf-8")
    monkeypatch.setattr(storage, "_PRIOR_README_SEED_HASHES", frozenset({_sha(old)}))

    storage.ensure_vault()

    assert (vault / "README.md").read_text(encoding="utf-8") == storage._SEED_README


def test_readme_untouched_when_edited(vault, monkeypatch):
    mine = "MY OWN README, hands off\n"
    (vault / "README.md").write_text(mine, encoding="utf-8")
    # The edited content's hash is not a known prior seed -> no refresh.
    monkeypatch.setattr(storage, "_PRIOR_README_SEED_HASHES", frozenset({_sha("something else")}))

    storage.ensure_vault()

    assert (vault / "README.md").read_text(encoding="utf-8") == mine


# ── AGENTS.md (constitution) ─────────────────────────────────────────────────


async def test_agents_refreshed_when_pristine(vault, monkeypatch):
    body = "OLD PRISTINE CONSTITUTION BODY\n"
    raw = _agents(1, body)
    (vault / "AGENTS.md").write_text(raw, encoding="utf-8")
    monkeypatch.setattr(loader, "_PRIOR_BASELINE_BODY_HASHES", frozenset({_sha(parse_frontmatter(raw)[1])}))

    await loader.ensure_baseline()

    _, after_body = parse_frontmatter((vault / "AGENTS.md").read_text(encoding="utf-8"))
    _, seed_body = parse_frontmatter(seed.render_seed())
    assert after_body == seed_body  # upgraded to the current seed body


async def test_agents_untouched_when_version_bumped(vault, monkeypatch):
    body = "OLD PRISTINE CONSTITUTION BODY\n"
    raw = _agents(2, body)  # version > 1 => saved via the editor
    (vault / "AGENTS.md").write_text(raw, encoding="utf-8")
    monkeypatch.setattr(loader, "_PRIOR_BASELINE_BODY_HASHES", frozenset({_sha(parse_frontmatter(raw)[1])}))

    await loader.ensure_baseline()

    assert (vault / "AGENTS.md").read_text(encoding="utf-8") == raw


async def test_agents_untouched_when_body_edited(vault, monkeypatch):
    raw = _agents(1, "I edited the constitution directly in Obsidian\n")
    (vault / "AGENTS.md").write_text(raw, encoding="utf-8")
    # Body hash is not a known prior seed -> leave it alone.
    monkeypatch.setattr(loader, "_PRIOR_BASELINE_BODY_HASHES", frozenset({_sha("unrelated")}))

    await loader.ensure_baseline()

    assert (vault / "AGENTS.md").read_text(encoding="utf-8") == raw
