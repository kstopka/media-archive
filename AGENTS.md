# Repository Guidelines

## Project Structure & Module Organization

Start with [docs/README.md](docs/README.md), then the relevant feature reference: [CLI](docs/architecture.md#cli-execution), [configuration](docs/configuration.md), [identities](docs/identities.md) or [SQLite state](docs/state.md). Read affected source afterward.

- `src/media_archive/`: Python package; `cli.py` handles arguments, `config/` handles configuration.
- `tests/`: unittest suites and synthetic fixtures.
- `docs/`: public technical documentation, organized by feature as the application grows.
- `archive_docs/`: ignored local plans, when present; distinguish planned behavior from implemented features.

CLI help and configuration validation work. The [SQLite foundation](docs/state.md) provides an explicit Python API for database lifecycle and migrations. Persistent registration, media history and media operations remain unimplemented.

## Build, Test, and Development Commands

The project currently uses Python 3.14.4 in `.venv`. The package minimum is Python 3.11, as declared in `pyproject.toml`. Reuse `.venv` when available. Adopt newer stable Python releases for development after verifying dependency installation and the full test suite.

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

Prefer current, documented practices that are proven with the project's supported Python and library versions. Use recommended replacements for deprecated APIs and typing patterns when available.

## Testing Guidelines

Use `unittest`, `test_*.py` files and `test_*` methods. Cover changed contracts, invalid inputs, privacy and unintended writes using temporary directories and synthetic identities. Use only the registry configuration schema; never require connected media. No coverage threshold is configured.

## Feature Workflow

This is a new application in its first MVP iteration. Before implementing a feature, check the standard library, existing dependencies and established libraries. Use suitable existing tools for common mechanisms whenever they simplify implementation. For a new dependency, explain its name, purpose and benefit and wait for the user's decision before adding it. Approved dependencies need no renewed approval.

When a new solution replaces an MVP implementation, remove the superseded code and its obsolete compatibility paths. Add compatibility for earlier prototypes only when the user explicitly requires it.

Finish each feature change by documenting behavior, API, errors, limitations and tests in `docs/`. Add new feature pages to `docs/README.md`. Keep README brief with essentials and links. Link feature-specific AI instructions here when needed instead of expanding this guide.

## Commit & Pull Request Guidelines

Use short English subjects, following history such as `docs: prepare repository for implementation`. PRs describe behavior, validation and limitations, linking relevant issues. The user performs staging, commits and pushes.

## Security & Agent Instructions

Keep `.env`, reports, databases and media private; never force-add ignored files. Do not modify real media without explicit authorization. Communicate in Polish; public documentation is English. Work one authorized task at a time. Before task 1.3, ask whether archive reorganization is complete and wait for an explicit answer.
