# Repository Guidelines

## Project Structure & Module Organization

Start with [docs/README.md](docs/README.md), then the relevant feature reference: [CLI](docs/architecture.md#cli-execution), [configuration](docs/configuration.md) or [identities](docs/identities.md). Read affected source afterward.

- `src/media_archive/`: Python package; `cli.py` handles arguments, `config/` handles configuration.
- `src/media_archive/config/legacy/`: compatibility adapter; keep historical category assumptions isolated here.
- `tests/`: unittest suites and synthetic/legacy fixtures.
- `docs/`: public technical documentation, organized by feature as the application grows.
- `archive_docs/`: ignored local plans, when present; distinguish planned behavior from implemented features.

Only CLI help and configuration validation work; SQLite and media operations remain unimplemented.

## Build, Test, and Development Commands

Use Python >=3.11 and reuse `.venv` when available:

```bash
python3 -m venv .venv                      # first-time environment
.venv/bin/python -m pip install -e .       # editable setuptools installation
.venv/bin/python -m media_archive --help
.venv/bin/python -m media_archive --check-config
.venv/bin/python -W error -m unittest discover -s tests -q
git diff --check
```

Configuration checks read `.env` without writing state or contacting devices.

## Coding Style & Naming Conventions

Use four-space indentation, type hints, `snake_case` functions and `PascalCase` classes. Follow immutable Pydantic models and safe `ConfigError` diagnostics. Collections own paths; sources reference IDs. Never infer behavior from names like `main`. No formatter/linter is configured.

## Testing Guidelines

Use `unittest`, `test_*.py` files and `test_*` methods. Cover changed contracts, invalid inputs, privacy and unintended writes using temporary directories and synthetic identities. Preserve legacy regressions; never require connected media. No coverage threshold is configured.

## Feature Workflow

Before implementing a feature, check the standard library, existing dependencies and ready-made libraries instead of rebuilding common mechanisms. Propose new libraries with name, purpose and benefit; wait for the user's decision before adding them. Approved dependencies need no renewed approval.

Finish each feature change by documenting behavior, API, errors, limitations and tests in `docs/`. Add new feature pages to `docs/README.md`. Keep README brief with essentials and links. Link feature-specific AI instructions here when needed instead of expanding this guide.

## Commit & Pull Request Guidelines

Use short English subjects, following history such as `docs: prepare repository for implementation`. PRs describe behavior, validation and limitations, linking relevant issues. The user performs staging, commits and pushes.

## Security & Agent Instructions

Keep `.env`, reports, databases and media private; never force-add ignored files. Do not modify real media without explicit authorization. Communicate in Polish; public documentation is English. Work one authorized task at a time. Before task 1.3, ask whether archive reorganization is complete and wait for an explicit answer.
