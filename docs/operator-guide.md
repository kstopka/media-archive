# Operator guide

## Setup and device checks

Install Media Archive in a Python 3.11+ virtual environment on Linux. Install `rsync` for local transfers and `exiftool` for SORT. Phone import also needs `adb` and authorized USB debugging. Linux identifies mounted volumes through `findmnt` and `lsblk`. [Installation](../README.md#quick-start)

Use `.env.examples` as a reference for your private `.env`. Check collection roots, backup policies, archive aliases and filesystem UUIDs. Commands below use `.env` in the current directory. To select another configuration, put `--config FILE` before the command.

```sh
media-archive --check-config
media-archive setup --dry-run
```

Use `setup --yes` for a new installation. For an existing database that needs an upgrade, use `migrate --dry-run` followed by `migrate`. Verify the configured disks before approving their hardware identities. For example, preview registration with `device register archive:video:a`, then repeat with `--yes` after checking the disk. Register the working volume and the intended writable archives individually. `device check` can read the UUID before hardware approval, which BACKUP requires for writing. [Device registration](devices.md#commands)

## First small batch

Start with material already in one local collection. Choose a month with a few known files and an append-only policy such as the example `gopro` collection. Have both intended archive disks mounted and confirm that they are the two expected physical disks. Use only the writable archives you intend to receive snapshots. The example uses `video`, `a`, `b` and `2025-01` as placeholders for the confirmed scope.

```sh
media-archive device check local:working
media-archive device check archive:video:a
media-archive device check archive:video:b
media-archive backup --dry-run --profile video --collection gopro --month 2025-01 --targets a b
```

Check the profile, collection, month, file and byte counts, effective policies, selected disks and space reserve in the preview. Check that the counts agree with the local files intended for this batch. If the scope is larger than intended, select a smaller configured scope and preview it again. The preview covers the selected media targets. Execution also checks space for snapshot recipients, which include other connected, registered writable archives that pass preflight. [Scope and capacity](backup-planning.md), [Snapshot recipients](backup-snapshots.md)

After the operator approves the exact scope and destinations, run the same selection with `--yes`:

```sh
media-archive backup --yes --profile video --collection gopro --month 2025-01 --targets a b
```

Monthly runs skip cleanup. In an All-scope run, eligible cleanup displays the exact files and asks for `USUŃ` in the terminal. Press Enter to decline. For the first batch, keep the local files and source originals. Check the exit code and the text or JSON report in `APP_REPORTS_DIR`. Expect `2/2` fresh copies for every file in this two-disk trial. The snapshot entries should show the same generation on both disks. Run `verify-snapshot GENERATION_DIRECTORY` for each generation directory reported under `APP_ARCHIVE_STATE_DIR` on those archive disks. Record the approved scope, copy results and snapshot validation totals. Keep reports and snapshot contents private. [BACKUP reports and evidence](backup-coordination.md#evidence-and-manual-space-release)

The first authorized production batch verified the two existing NTFS copies of every selected file and published the same validated snapshot on both archives. Transfer and database restore were exercised separately in isolated trials. Run a full archive AUDIT separately when you intend to read the complete registered archive roots. [Verification scope](development.md#end-to-end-verification), [Full audit](audit.md#full-sha-256-audit)

## Initial inventory, import and SORT

Use `inventory` for the first inventory of existing material. For a new phone profile, first capture its baseline with `init-phone PROFILE`. Complete its comparison with a hashed local inventory and a completed full audit of the appropriate archive pair. Review the saved initialization policy before filling gaps or starting incremental import. [Inventory](inventory.md), [Phone initialization](phone-initialization.md)

Preview a camera import with `import --source PROFILE --card ALIAS --mount DIRECTORY --dry-run` or a phone import with `import --source PROFILE --dry-run`. Compare the manifest with the intended batch before repeating without `--dry-run`. Each import considers the eligible files in its configured source scope. Run `sort --dry-run` to review pending placements, then `sort` to apply them. Use `sort-date ID YYYY-MM-DD` for an import awaiting a date. Use `conflicts` to inspect name collisions and `catalog-move` to record a verified manual rename or move within a collection. [Camera import](camera-import.md), [Phone import](phone-import.md), [SORT](sort.md), [Catalog](catalog.md)

## Progress and recovery

- IMPORT and a full AUDIT show progress while reading. BACKUP reports planning, transfer and snapshot phases and saves per-file outcomes. Exit code `3` indicates partial or blocked work, `4` reports differences in a completed audit, and `130` indicates interruption. [Exit status](cli.md#reports-and-exit-status)
- After an interruption, use `pending` and `resume ID --dry-run`. Resume an IMPORT or SORT with `resume ID`, supplying the camera arguments if an import needs to read the card again. For BACKUP, rerun `backup --yes` with the same profile, scope and intended targets. Resume a full audit with `audit --resume AUDIT_ID`. [Import recovery](import-recovery.md), [BACKUP retry](backup-coordination.md#failures-and-retry), [Audit resume](audit.md#full-sha-256-audit)
- Save a local state copy with `snapshot DIRECTORY` in an existing private directory outside the application state, reports and media paths. To recover, stop other operations, run `verify-snapshot GENERATION_DIRECTORY`, then `media-archive --config FILE restore GENERATION_DIRECTORY --yes`. Run `media-archive --config FILE reconcile` before writing media again. An archive-only phone baseline match can require a new full audit after restore. [State restore](snapshots.md), [Reconciliation](import-recovery.md#after-restoring-sqlite)

Transfers verify file size and SHA-256. Embedded metadata stays with the file bytes. Copies request the source modification time, subject to the precision of the destination filesystem and the timestamps exposed by ADB. Treat file ownership, permissions, ACLs and extended attributes as outside the verified copy. [Transfer attributes](transfer.md#copy-and-publication), [Source attributes](sources.md)
