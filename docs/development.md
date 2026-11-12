# Development and verification

Read [the technical index](README.md) before opening the entire source tree. This page describes current development commands and where to change existing behavior.

## Environment and commands

Python >=3.11 is required. Runtime dependency ranges are declared in [pyproject.toml](../pyproject.toml); no additional test runner is required.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
python -m media_archive --help
python -m unittest discover -s tests -v
```

The project uses `src/media_archive/`; editable installation is required before running the CLI or tests. After changing package layout or build metadata, rerun `python -m pip install -e .`. Do not add `src` to `PYTHONPATH` to bypass installation.

If an environment already exists, reuse it. From the repository root on Linux, `.venv/bin/python` can be used directly instead of activation. Do not replace the system interpreter. Project VS Code defaults are in [.vscode/settings.json](../.vscode/settings.json).

Targeted examples:

```bash
.venv/bin/python -m unittest tests.test_collection_config -v
.venv/bin/python -m unittest tests.test_cli -v
.venv/bin/python -W error -m unittest discover -s tests -q
git diff --check
```

The SQLite foundation verification passed **122 tests on Python 3.14.4 with SQLite 3.46.1**, SQLAlchemy 2.1.8 and Alembic 1.20.1. The run used `-W error` and included 36 state tests and installed-package subprocess checks. Python 3.11 syntax compatibility was checked with `ast.parse(feature_version=(3, 11))`. The runtime test suite was not executed on Python 3.11.

## Test map

| File | Coverage |
| --- | --- |
| [test_cli.py](../tests/test_cli.py) | Installed-package subprocess in isolated Python mode (`-I`), help, the registry configuration, argument errors, exit codes, private-value diagnostics and absence of new runtime files |
| [test_collection_config.py](../tests/test_collection_config.py) | Current generic model: root/month scopes, nearest policy, new profiles/volumes, unassigned collections, reference/tree/storage rejection, explicit deletion eligibility, no-write behavior and archive names with `_TARGET_`/ambiguous prefixes |
| [test_config_identities.py](../tests/test_config_identities.py) | Display label edits/defaults , unknown IDs and explicit registration, retained offline identities, hardware changes and no-write checks |
| [test_state_database.py](../tests/test_state_database.py) | Explicit SQLite lifecycle, read-only behavior, migration transactions, constraints, publication failures, concurrent creation/migration and recovery after process termination |
| [test_state_registry.py](../tests/test_state_registry.py) | Persistent IDs and bindings, retained archive reservations, explicit hardware changes, concurrent approvals and registration rollback |

`registry_values(root)` reads `.env.examples` and substitutes temporary paths and synthetic identifiers. `write_config` writes only temporary test configuration files. Parser and model tests cover required values, immutable/private DTOs and obsolete-key rejection. Flexibility tests cover source replacement, extra cards/volumes and archive continuation.

Use synthetic data and temporary directories. Do not turn `.env`, connected phones, cards or archives into required test fixtures. State tests exercise SQLite transaction rollback and migration recovery on temporary files, including process termination. Actual transfers, full archive hashes, physical identity verification and hardware power-loss durability require later verification.

## Change navigation

| Change | Start with | Also update |
| --- | --- | --- |
| CLI option/output | `src/media_archive/cli.py` | CLI tests, root README, architecture CLI table |
| New `.env` field | `config/env.py`, appropriate DTO, `config/registry.py` | Field/relationship validators, `.env.examples`, relevant tests, configuration reference |
| New reusable field constraint | `config/field_types.py` | Tests of valid/invalid values and field-validation reference |
| Cross-model invariant | `config/validation.py` | Generic collection tests and relationship-validation table |
| Collection lookup/scope | `config/collections.py`, `config/models.py` | API tests and lookup-contract table |
| Display labels and identity registration | `config/identities.py`, source DTOs and `state/registry.py` | Identity and registry tests, `docs/identities.md`, `docs/registration.md` and configuration reference |
| SQLite lifecycle or migration | `state/database.py`, `state/migrations.py`, `state/revisions/` | State tests, `docs/state.md`, architecture and capability status. Append Alembic revisions while preserving applied revision files. |
| Dependency | `pyproject.toml` | Explain purpose and obtain the user's decision before introducing a new library; update dependency documentation |
| New subsystem | New focused package/module | Its own technical page linked from `docs/README.md`, tests and capability status |

Before adding a setting, decide which object owns it. Local paths belong to collections; import profiles refer to collection IDs; policies describe behavior; backup profiles select roots and archive groups. Keep fixed category names out of the generic core. Use `builders.build()` for loader-facing DTO construction so private input is not exposed by Pydantic errors.

Before implementing a feature, review the standard library, existing dependencies and suitable ready-made libraries. If a new library can replace a common mechanism, explain its name, purpose and benefit and wait for the user's decision before adding it. Finish feature changes by updating their technical documentation and the feature index in `docs/README.md`; keep user-facing README changes concise.

Keep the existing read-only boundary of `load_config()` and help. Do not introduce database creation, media scanning or registrations as a side effect of configuration checks. Validate physical devices in a future runtime subsystem, not by making configuration loading depend on plugged-in hardware.

## Documentation and repository boundaries

`docs/` is public technical documentation of the implementation. The root README is the user-facing overview, and `.env.examples` is the sanitized configuration template. Update these with each relevant code change, following [the documentation maintenance contract](README.md#documentation-maintenance-contract).

Private `.env`, local planning notes in `archive_docs/`, reports, databases, media and generated artifacts remain excluded by [.gitignore](../.gitignore). The technical documentation must work without those private files. Do not link public pages to unavailable local planning notes or copy their actual paths/identifiers. Do not force-add ignored files.

No commit is required to run checks. Repository staging, commits and pushes are performed by the user in the current collaboration workflow.
