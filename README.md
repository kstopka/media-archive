# Media Archive CLI

A local Linux CLI for importing, organizing, and archiving photos, videos, and documents. Paths, devices, collections and backup policies are configured in a private `.env` file.

The planned workflow keeps four operations separate:

- **IMPORT** — copy media to staging while preserving source originals.
- **SORT** — organize verified imports into local collections.
- **BACKUP** — manually copy selected collections to external archives and verify them with SHA-256.
- **AUDIT** — compare archive inventories or verify complete file hashes.

## Status

CLI help and read-only configuration validation are available. Python APIs provide [SQLite lifecycle and migrations](docs/state.md) and [explicit configuration registration](docs/registration.md). Media history and the four media operations remain planned.

## Quick start

Requires Linux and Python **3.11+**. From the repository root:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
python -m media_archive --help
```

Source code lives in `src/media_archive/`. Install the project before running the CLI or tests; editable installation picks up source changes.

For a new configuration, copy the template and replace example paths, UUIDs and device identifiers. Keep an existing `.env`; use the template as a reference instead of overwriting it.

```bash
# Only when you do not already have .env:
cp .env.examples .env
chmod 600 .env
```

Use literal absolute paths rather than `~` or `$HOME`. Settings are read as data; variable interpolation and shell execution are disabled.

## Useful commands

With the virtual environment active:

```bash
python -m media_archive --check-config
python -m media_archive --config "/absolute/path/to/custom.env"
python -m unittest discover -s tests -v
```

Configuration checks do not create a database, change files or contact devices. They validate settings, not physical disk availability or identity.

## Documentation

- [Technical documentation](docs/README.md) — feature index and starting point for developers.
- [Roadmap and planned designs](docs/README.md#roadmap-and-planned-designs) — implementation stages and future workflows.
- [Configuration](docs/configuration.md) — settings, models, validation and compatibility.
- [Architecture](docs/architecture.md) — modules and execution flow.
- [Development](docs/development.md) — tests and contribution workflow.
- [AI contributor instructions](AGENTS.md) — repository guidelines.

Feature details live in `docs/`; this README stays focused on the project overview and getting started.

## Privacy

Keep `.env`, reports, databases and media out of Git. The repository includes the sanitized `.env.examples`; private configuration and local planning notes remain ignored.
