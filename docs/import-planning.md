# Import planning

Use `import --dry-run` to see what a selected source would import:

```bash
media-archive import --source phone-a --dry-run
media-archive import --source camera-a --card card-a --mount /example/mount --dry-run
media-archive import --source phone-a --dry-run --reimport 'event/example.jpg' --report
```

Phone aliases use the registered ADB source and its configured camera directory. Camera aliases require a registered card assigned to the profile and its mount point. The planner checks every regular file of a supported format by independently reading its source SHA-256. It keeps the original filename and separates staging directories by source kind, registered profile ID and content hash. Camera previews accept JPG, JPEG and MP4, and honor the camera profile's ignored extensions. Phone previews and the phone baseline share JPG, JPEG, PNG, HEIC, HEIF, MP4 and MOV. Other entries are reported separately and remain on the source. A failed source listing or supported file read blocks the batch.

## Decisions and capacity

Content identity is the pair of SHA-256 and byte count. A successful import in the same profile remains part of history when a local copy has been moved or removed. A hash known only to another profile does not suppress a copy. Equal content inside a batch gets one staging destination. Import planning does not assert that an earlier staging copy is still present; recovery of missing staging material is part of the later import and SORT workflow.

A phone needs a completed [baseline comparison](phone-initialization.md). Recognized copies and saved skips remain excluded under that profile. While `import_missing` is still processing, a file can enter the plan only if its relative path, SHA-256 and size match a recorded gap on the same device and root. A new path with identical content waits for incremental import after initialization. An explicit `--reimport` selects a supported file by its exact path relative to the source root, and must match content already imported or resolved by the selected profile. It uses a separate staging path, preserves previous history and cannot overwrite a staged file. A phone's reimport is available after initialization completes.

With copies planned, the planner checks the registered writable staging volume. For each destination it checks write and search permission on the existing directory that would receive the first missing parent or the temporary work directory. It creates nothing. The space estimate rounds each file up to the filesystem allocation unit and budgets one unit for each unique missing parent and each planned copy's work directory. The configured `APP_MIN_FREE_BYTES` reserve must remain. Existing parts already occupy reported space. Insufficient capacity, missing permissions, an unsafe path or a destination conflict blocks all transfers. Free space and permissions are measured at planning time; the space estimate does not reserve blocks or predict exact filesystem metadata costs.

## API and reports

`media_archive.import_planning.plan_import(config, profile, *, card=None, mount=None, reimport=(), record=False, announce=..., cancelled=None)` returns an immutable manifest. `plan.transfers` exposes eligible items only when the batch is ready. With `record=True`, the same planner records the stable source reads of a ready batch in one SQLite transaction, returning the observation run and source occurrence IDs for use by later import execution. It does not publish copies or record successful imports. The CLI uses the read-only mode, writes no observations or media, and saves the text and JSON plan only with `--report`. A blocked plan returns code `3`, while source or configuration errors follow the [CLI conventions](cli.md#reports-and-exit-status).

[Synthetic planning tests](../tests/test_import_planning.py) verify profile isolation, repeated filenames, exact reimport, source failures, capacity and the read-only preview. [Phone initialization tests](../tests/test_inventory.py) verify how the planner uses both baseline policies.
