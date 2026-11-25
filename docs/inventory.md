# Initial inventory of existing data

Run this after [setup](cli.md). It records what already exists in the configured local collections and archive roots. It writes observations to the system SQLite database and produces a text and JSON report. It does not change media or call ADB.

```bash
media-archive inventory
media-archive inventory --hash-local
```

The default is a metadata scan: it records the presence, path, size and modification time of regular files without opening their contents. Each top-level collection is scanned once; a file inside a configured child belongs to the deepest matching collection. Archive scopes cover each configured target, including read-only targets, and their complete configured group roots. Files outside backup profiles are included. Local paths are absolute and archive paths are relative to the disk. Each observation is associated with the registered filesystem episode. Directory links are not followed and files on nested mounts are not accepted as part of the scope. The scan records the current configured layout.

`--hash-local` also reads local files to compute SHA-256 using the source adapter's stability checks. For archive contents use [full audit](audit.md#full-sha-256-audit), which has per-file checkpoints and can read one disk at a time. Matching SHA-256 and size link separate local and archive occurrences to the same content record, even under different names. A metadata observation alone does not establish that link. Neither inventory nor audit creates a successful import, placement, or backup copy record.

The terminal reports the number of scanned directories and regular files during metadata traversal, then announces when a scope is being saved. Hashing progress shows completed files and bytes read in decimal KB, MB, GB or TB. Spaces group thousands in file counts. JSON reports retain exact numerical values.

## Resume and errors

Connect whichever disks are available and run the same command again after attaching the others. Every completed scope, including an empty directory, has a stable observation run in SQLite. Its observations and completion are committed together and survive report rotation. An unmounted volume, missing scope root or scan error leaves the scope pending. Every configured child collection must also be read as a directory. A child that is missing, is a symbolic link, or lies behind one leaves the enclosing scope pending. An unfinished scope is read again; completed scopes are reused as historical evidence even when their disks are offline. The optional local hash pass has scope rather than per-file checkpoints and retries unfinished collections in full. Changing the registered volume episode or applicable local paths and collection ownership changes the scope identity. Moving the mount point of the same archive episode does not.

Exit code `0` means all requested scopes have completed, `3` means at least one is still pending, and `130` means the command was interrupted. Configuration, state access, and report errors follow the [CLI conventions](cli.md#reports-and-exit-status). A completed scope is evidence from the time it was read, not an atomic snapshot or a current guarantee of presence. This command does not finish per-profile phone initialization.

[The workflow](../src/media_archive/inventory.py) reuses verified local reads and resource locks. [The SQLite adapter](../src/media_archive/state/inventory.py) stores scope runs in the existing history schema; no new revision is required. [Synthetic tests](../tests/test_inventory.py) exercise sequential attachment, nested ownership, interruption, read errors, and content shared with a full archive audit.
