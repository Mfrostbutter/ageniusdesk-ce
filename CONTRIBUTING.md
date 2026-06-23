# Contributing to AgeniusDesk Community Edition

Thank you for your interest in contributing. We welcome pull requests, bug reports, and feature suggestions.

## Development Setup

### Local Development

Clone the repo and create a dev environment:

```bash
git clone https://github.com/Mfrostbutter/ageniusdesk-ce.git
cd ageniusdesk-ce
cp .env.example .env
cp docker-compose.override.example.yml docker-compose.override.yml
docker compose up -d --build
```

The `docker-compose.override.yml` bind-mounts the backend and frontend directories and runs uvicorn with `--reload`, so changes to Python or JavaScript are reflected immediately without rebuilding the image.

Visit http://localhost:3000 to access the dev instance.

### Without Docker

If you prefer bare-metal development:

```bash
python -m venv venv
source venv/bin/activate  # or . venv/Scripts/activate on Windows
pip install -e '.[assistant]'
python -m uvicorn backend.main:app --reload --host 0.0.0.0 --port 3000
```

## Code Style

**Python:**
- Type hints on all public functions
- Ruff is configured for line-length 120, rules E/F/I/W
- Run `ruff check .` to lint, `ruff format .` to format

**JavaScript:**
- Vanilla ES modules, no transpilation
- Use clear variable names and comments where logic is non-obvious
- No external dependencies (Monaco is loaded from CDN)

**Commits:**
- Use conventional style: `feat:`, `fix:`, `docs:`, `chore:`
- Keep commits atomic and focused
- Reference issues where applicable

## Adding Features

### Adding a Backend Module

Modules are auto-discovered. To add a new module:

1. Create `backend/modules/{module_id}/` with:
   - `__init__.py` - import and expose `router` (FastAPI APIRouter)
   - `manifest.json` - metadata (name, version, description)
   - `router.py` - API route definitions

2. The module's routes are mounted at `/api/{module_id}/...` automatically.

3. Follow the pattern of existing modules (e.g., `docker_mgr/`, `assistant/`) for consistency.

### Adding a Frontend View

Views are ES modules in `frontend/js/views/`:

1. Create `frontend/js/views/my-feature.js` exporting an async `render(container)` function
2. Add a sidebar button in `frontend/index.html` with `data-view="my-feature"`
3. The router in `app.js` will load and render your view on click
4. Use CSS custom properties from `base.css` for theming

### Adding a Community Docker Template

Community container templates are JSON files at `data/templates/{name}.json`. Drop a JSON file there (or upload via the UI) and it appears in the Containers tab.

Template format matches the schema in `backend/modules/docker_mgr/templates.py`. See existing templates for examples.

## Testing

AgeniusDesk does not have a bundled test suite yet. Contributions adding pytest tests to a `tests/` directory are welcome.

Run a manual test of critical paths before submitting a PR:
- Add an n8n instance and list workflows
- View the Errors tab and verify grouping
- Test the AI Assistant (if you have a provider key set)
- Create a secret and verify it's encrypted in `data/secrets.json`

## Pull Request Guidelines

1. Fork the repo and create a branch from `main`
2. Make your changes and test locally
3. Push to your fork and create a PR with a clear description
4. Link any related issues
5. Ensure your code follows the style guidelines above

PRs are reviewed by maintainers and merged if they:
- Solve a real problem or add a clearly useful feature
- Do not break existing functionality
- Follow the established code patterns and style
- Include reasonable commit messages

## Reporting Issues

If you find a bug or have a feature request, please open a GitHub issue with:
- A clear title and description
- Steps to reproduce (for bugs)
- Expected and actual behavior
- Your environment (OS, Docker version, n8n version)

## License

By contributing, you agree that your contributions are licensed under the MIT License.
