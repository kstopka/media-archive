# Camera card import

Use a registered camera profile, a card assigned to it, and the card's verified mount point. The staging directory must be on a registered local volume approved for writing. To preview and then execute an import:

```bash
media-archive import --source hero8 --card a --mount /example/card --dry-run
media-archive import --source hero8 --card a --mount /example/card
```

The same [planner](import-planning.md) selects and hashes files for both commands. JPG, JPEG and MP4 are eligible; configured exclusions such as THM and LRV are skipped. Unsupported entries are listed in the report and stay on the card. History is scoped to the camera profile: the same content is recognized after registering a reformatted card or seeing a different filename. The same filename with different content can be imported as a distinct copy.

Execution holds the source and staging locks across planning and copying. It records one journal operation per destination path on the staging volume before the [transfer](transfer.md) starts. A retry for that destination reuses the operation even if the source moved to another card directory, and checks that its content still matches. The transfer checks the source again, uses rsync to stage the bytes, verifies the staged copy independently and publishes without overwriting. Source files are never changed. Only a verified publication can commit both the journal attempt and the import record. Files remain in staging until a later SORT operation.

Execution saves a text and JSON report with the manifest and per-file outcomes. Exit code `0` includes a batch with no new files. A blocked plan or a failed file returns `3`; the batch stops at the first transfer failure and keeps previous verified imports. Ctrl+C returns `130`. Failed parts are preserved according to the transfer contract. The report includes operation IDs when they were assigned. See [import recovery](import-recovery.md) for interrupted publication and missing staging.

`media_archive.camera_import.import_camera(config, profile, *, card, mount, reimport=(), announce=..., cancelled=None)` returns the manifest and execution results. It uses the current SQLite schema. [Integration tests](../tests/test_import_planning.py) use synthetic cards and staging volumes with real rsync. They cover execution using the preview's paths, excluded and unsupported files, content history after reformatting, and a same-size source change between planning and transfer.

An authorized hardware trial on 2026-11-21 imported one video from an exFAT card into an isolated test directory on NTFS using a temporary database. The source manifest was restricted to one file. Preview, publication, independent SHA-256 comparison, unchanged source content and a repeated import without an additional copy all passed. The temporary output and database were removed. This trial did not cover the complete card, physical disconnection or power loss.
