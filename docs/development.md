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

The last recorded code verification, after separating display labels and adding the in-memory identity guard, passed **84 tests on Python 3.14.4**, including `-W error` and installed-package CLI checks. Both module and console entry points were also checked from outside the repository during the src-layout migration. This is a past result, not a substitute for running relevant tests after a new code change. Python 3.11 syntax compatibility was checked with `ast.parse(feature_version=(3, 11))`; execution of the test suite on Python 3.11 has not been established by that check.

## Test map

| File | Coverage |
| --- | --- |
| [test_cli.py](../tests/test_cli.py) | Installed-package subprocess in isolated Python mode (`-I`), help, the registry configuration, argument errors, exit codes, private-value diagnostics and absence of new runtime files |
| [test_collection_config.py](../tests/test_collection_config.py) | Current generic model: root/month scopes, nearest policy, new profiles/volumes, unassigned collections, reference/tree/storage rejection, explicit deletion eligibility, no-write behavior and archive names with `_TARGET_`/ambiguous prefixes |
| [test_config_identities.py](../tests/test_config_identities.py) | Display label edits/defaults , unknown IDs and explicit registration, retained offline identities, hardware changes and no-write checks |

`registry_values(root)` reads `.env.examples` and substitutes temporary paths and synthetic identifiers. `write_config` writes only temporary test configuration files. Parser and model tests cover required values, immutable/private DTOs and obsolete-key rejection. Flexibility tests cover source replacement, extra cards/volumes and archive continuation.

Use synthetic data and temporary directories. Do not turn `.env`, connected phones, cards or archives into required test fixtures. Current tests do not demonstrate actual transfers, full archive hashes, physical identity verification, interruption recovery or database durability.

## Change navigation

| Change | Start with | Also update |
| --- | --- | --- |
| CLI option/output | `src/media_archive/cli.py` | CLI tests, root README, architecture CLI table |
| New `.env` field | `config/env.py`, appropriate DTO, `config/registry.py` | Field/relationship validators, `.env.examples`, relevant tests, configuration reference |
| New reusable field constraint | `config/field_types.py` | Tests of valid/invalid values and field-validation reference |
| Cross-model invariant | `config/validation.py` | Generic collection tests and relationship-validation table |
| Collection lookup/scope | `config/collections.py`, `config/models.py` | API tests and lookup-contract table |
| Display labels/identity registration contract | `config/identities.py`, source DTOs and loaders | Identity tests, `docs/identities.md`, configuration reference and future state-layer integration |
| Dependency | `pyproject.toml` | Explain purpose and obtain the user's decision before introducing a new library; update dependency documentation |
| New subsystem | New focused package/module | Its own technical page linked from `docs/README.md`, tests and capability status |

Before adding a setting, decide which object owns it. Local paths belong to collections; import profiles refer to collection IDs; policies describe behavior; backup profiles select roots and archive groups. Keep fixed category names out of the generic core. Use `builders.build()` for loader-facing DTO construction so private input is not exposed by Pydantic errors.

Before implementing a feature, review the standard library, existing dependencies and suitable ready-made libraries. If a new library can replace a common mechanism, explain its name, purpose and benefit and wait for the user's decision before adding it. Finish feature changes by updating their technical documentation and the feature index in `docs/README.md`; keep user-facing README changes concise.

Keep the existing read-only boundary of `load_config()` and help. Do not introduce database creation, media scanning or registrations as a side effect of configuration checks. Validate physical devices in a future runtime subsystem, not by making configuration loading depend on plugged-in hardware.

## Documentation and repository boundaries

`docs/` is public technical documentation of the implementation. The root README is the user-facing overview, and `.env.examples` is the sanitized configuration template. Update these with each relevant code change, following [the documentation maintenance contract](README.md#documentation-maintenance-contract).

Private `.env`, local planning notes in `archive_docs/`, reports, databases, media and generated artifacts remain excluded by [.gitignore](../.gitignore). The technical documentation must work without those private files. Do not link public pages to unavailable local planning notes or copy their actual paths/identifiers. Do not force-add ignored files.

No commit is required to run checks. Repository staging, commits and pushes are performed by the user in the current collaboration workflow.
