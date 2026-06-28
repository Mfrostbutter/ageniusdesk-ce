"""First-run seeding of the harness n8n skill library (data/workspace/skills/).

ensure_skills() vendors backend/skills_seed/ into the vault once. It must seed
the full set on a cold vault, never clobber an existing skills/ folder, and
honor the AGD_SEED_SKILLS opt-out.
"""

import pytest

from backend.modules.notes import skills_seed, storage


@pytest.fixture
def isolated_vault(tmp_path, monkeypatch):
    """Point the vault at a throwaway dir so seeding never touches the session
    vault (which the app lifespan already seeded)."""
    vault = tmp_path / "workspace"
    monkeypatch.setattr(storage, "VAULT_DIR", vault)
    return vault


async def test_ensure_skills_seeds_library(isolated_vault, monkeypatch):
    monkeypatch.delenv("AGD_SEED_SKILLS", raising=False)
    await skills_seed.ensure_skills()

    skills = isolated_vault / "skills"
    assert skills.is_dir()
    # The AgeniusDesk router/index note and the upstream router skill.
    assert (skills / "README.md").is_file()
    assert (skills / "using-n8n-mcp-skills" / "SKILL.md").is_file()
    assert (skills / "n8n-workflow-patterns" / "SKILL.md").is_file()
    # MIT attribution travels with the vendored content.
    assert (skills / "LICENSE-n8n-skills.txt").is_file()
    # All 15 skills (each a dir carrying a SKILL.md).
    skill_dirs = [d for d in skills.iterdir() if d.is_dir() and (d / "SKILL.md").is_file()]
    assert len(skill_dirs) == 15


async def test_ensure_skills_is_non_destructive(isolated_vault, monkeypatch):
    monkeypatch.delenv("AGD_SEED_SKILLS", raising=False)
    skills = isolated_vault / "skills"
    skills.mkdir(parents=True)
    sentinel = skills / "my-note.md"
    sentinel.write_text("mine", encoding="utf-8")

    await skills_seed.ensure_skills()

    # skills/ pre-existed -> seeding is skipped; operator content is untouched.
    assert sentinel.read_text(encoding="utf-8") == "mine"
    assert not (skills / "using-n8n-mcp-skills").exists()


async def test_ensure_skills_opt_out(isolated_vault, monkeypatch):
    monkeypatch.setenv("AGD_SEED_SKILLS", "false")
    await skills_seed.ensure_skills()
    assert not (isolated_vault / "skills").exists()
