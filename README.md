# Media Archive CLI

A planned local Linux command-line tool for importing, organizing, and archiving photos, videos, and documents.

## Project status

The project is in the planning stage. The implementation plan is under review; the CLI and configuration loader have not been implemented yet. The features below describe the intended behavior.

## Planned operations

- **IMPORT** — incrementally copy media from Android devices and memory cards, remembering previously imported content even after local selection and deletion. Originals remain on the source device.
- **SORT** — organize verified media using metadata dates, preserve conflicting files, and flag missing dates for manual review.
- **BACKUP** — manually copy selected collections to external archives, verify each destination independently, and track one or two available copies.
- **AUDIT** — compare archive contents using either a quick path/size check or a full SHA-256 verification, with progress and resume support.

Additional planned features include file search, conflict review, and restoring the local state database from a snapshot.

## Configuration

Machine-specific paths, device identifiers, collection folders, and limits belong in a private `.env` file. They must not be hardcoded in the application. Configuration will use `.env`; a separate TOML configuration layer is not planned.

Create your local configuration from the public template:

```bash
cp .env.examples .env
chmod 600 .env
```

If you already have a `.env` file, keep it and use the template as a reference instead of overwriting it.

Edit `.env` before use:

- Replace the example absolute paths, volume UUIDs, and ADB serial number with your own values.
- Set your timezone, local collections, archive destinations, and exact workflow folder names.
- Keep staging and files with unresolved dates outside collections included in a full backup.
- Leave the future secondary phone unconfigured until needed.

Values in the template are examples, not detected devices. Use literal paths; do not rely on shell expansion or variable interpolation. Do not execute `.env` as a shell script. Copying the template does not run any operation, and configuration loading will be added during implementation.

The template uses `import_missing` for an ordinary first phone import. The special `baseline_skip_missing` policy is intended only for an explicitly selected baseline of a previously curated collection: phone-only content is recorded as skipped, without downloading it or marking it as archived.

## Design principles

- Verify new transfers by independently reading and comparing SHA-256 hashes.
- Preserve original media bytes and embedded metadata.
- Keep IMPORT, SORT, BACKUP, and AUDIT separate and manually invoked.
- Resume interrupted work without treating partial files as completed copies.
- Preserve intentional copies under different collection paths.
- Require approval of an exact deletion list for designated workflow folders.
- Distinguish quick attribute comparisons from verified content integrity.
- Store the database and configuration on the system disk, with consistent snapshots on archive drives during backup.

## Planned technology

Python 3, SQLite, ExifTool, rsync, and ADB for supported Android sources.

## Repository contents

The repository is intended to contain application code, tests, essential project files, user documentation, and the sanitized `.env.examples` template. The private `.env`, working notes and old planning documents in `archive_docs/`, reports, databases, media, and generated artifacts stay local and are excluded from Git.
