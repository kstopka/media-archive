# SORT metadata and preview

Run `media-archive migrate` once if the existing database predates revision `0008`. SORT planning needs the registered staging volume and ExifTool on `PATH`. It reads staging without requiring the original phone, card or archive drives.

```bash
media-archive sort --dry-run
media-archive sort --dry-run --report
media-archive sort-date 42 2026-11-22
media-archive sort --dry-run
```

The preview uses the latest successful import for each content identity in each source profile and excludes imports with a recorded collection placement. It checks that staging is on the recorded registered volume and re-reads each candidate's SHA-256 before extracting metadata. Missing, unsafe or changed staging is reported as blocked. Use the [explicit reimport workflow](import-recovery.md#missing-staging) when a staging copy is missing.

ExifTool reads the same open file that was hashed. For photos, the EXIF `DateTimeOriginal` supplies the calendar date as recorded. For MP4 and MOV, ExifTool decodes the QuickTime movie header's `CreateDate` in a UTC process timezone with `QuickTimeUTC` enabled. The planner converts that timestamp once to `APP_TIMEZONE`. Each segment supplies its own date. The preview reports the selected tag value as returned by ExifTool, the interpreted timestamp, the tool version and diagnostics. Missing or invalid timestamps and per-file metadata errors send the proposal to the unresolved location. An unavailable ExifTool, timeout or unusable tool response is an execution error. The planner uses neither filenames nor filesystem or import timestamps to infer the date.

| Material | Proposed path relative to the configured root |
| --- | --- |
| Phone photo or video | `YYYY/YYYY.MM/original-name.ext` |
| Camera photo or video | `Różne YYYY/YYYY-MM-DD/<CAMERA_FOLDER>/original-name.ext` |
| Without a valid date | `<kind>/<registered profile ID>/<SHA-256>/original-name.ext` under `LOCAL_UNDATED_ROOT` |

`sort-date ID YYYY-MM-DD` stores or replaces a calendar-date decision for an import awaiting SORT. The saved date takes precedence in the next preview and the embedded metadata remains visible. The decision belongs to that import ID. Preview exit code `3` indicates blocked files or proposed unresolved paths. Exit code `0` indicates a complete preview, including an empty batch. `--report` saves the report as text and JSON.

The preview opens the database read-only and creates no media directories or files. `sort-date` writes the date to SQLite without editing media. SORT blocks proposed paths that traverse symbolic links. It also checks the unresolved root against the resolved paths of collections, staging and archives. Destination collision handling and moving or cleaning up staging belong to SORT execution in the next task. The Python entry points are `media_archive.sort_planning.plan_sort(config)` and `set_sort_date(config, import_id, value)`.

[SORT planning tests](../tests/test_sort_planning.py) cover synthetic photos and movie headers with actual ExifTool, year and timezone boundaries, invalid or unavailable metadata, changed staging, and stored date overrides.
