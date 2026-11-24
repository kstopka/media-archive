# Catalog and conflict review

Run `media-archive migrate` to upgrade an existing database to revision `0010`. The catalog uses saved inventory and media history.

```bash
media-archive search --name 'GX01' --month 2025-02 --source camera:hero
media-archive search --source phone:example
media-archive conflicts
media-archive conflicts --keep-both '/media/working/Photos/2025/02.2025/clip.mp4'
media-archive catalog-move 42 '/media/working/Photos/2025/02.2025/renamed.mp4'
```

## Search

Filters combine with AND. Name matches a substring of the stored filename, ignoring case and Unicode normalization differences. Month matches a recorded SORT month for the same content, using the manual date when set, or an explicit `MM.YYYY` or `YYYY-MM-DD` folder in the stored path. Files without matching date information are omitted when a month is selected. A source filter uses the stable profile alias and kind. Other locations link to that profile through matching content; inventory entries without SHA-256 remain searchable by filename or month.

The catalog covers source, collection, unresolved-date and archive locations. Each result identifies a saved occurrence and its device binding, when recorded. Archive paths are relative to the saved volume. The command checks whether a known local or archive volume is accessible and marks inaccessible volumes as last-known locations. Search reads SQLite and checks mounts without scanning media or updating presence. A previous observation or hash is historical evidence even when the volume is mounted.

## Review decisions

`conflicts` shows the durable group key, original name, each recorded path and its SHA-256. The listing includes reviewed groups and retains recorded variants after a manual move, local removal or loss of access. Copy the exact displayed group key when recording a decision. `--keep-both` records the review in SQLite and leaves files untouched. Adding a new member reopens the group for review.

## Reconcile a manual move

After moving or renaming a local file, use `search` to find its previous occurrence ID and provide the new absolute file path to `catalog-move`. The destination must belong to the same collection, including after an intentional change to its configured local root. Both volume bindings must be accessible, the old path must be absent, and the new file must have the saved SHA-256 and size. The command rejects ambiguous names, symbolic links, missing evidence and pending SORT cleanup. For a case-only rename on a case-insensitive volume, first move to a distinct temporary name and record that move. Then rename the file and record the second move.

On success SQLite records the verified new occurrence and links every unresolved local record for the same old path, content and volume to it in one transaction. This covers multiple imports that reused one physical copy. Previous import placements, archive paths and conflict membership remain in history. Changes to archive destinations and moves between collections require a separate workflow. The command reads media and updates SQLite; the operator performs the move itself.

The Python entry points are `search_catalog`, `list_conflicts`, `keep_both` and `record_manual_move` in `media_archive.catalog`. Invalid or unverifiable operations raise `StateError` or `ConfigError`. Automated tests use a synthetic offline archive and temporary local media to cover filters, conflict decisions, historical visibility and rejection of a changed file.
