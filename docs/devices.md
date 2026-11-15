# Linux volume identification and safe paths

The application checks volumes mounted by Linux. Device commands read mount and block-device metadata on demand. The system remains responsible for mounting. The commands make no changes to media or to `.env`.

## Commands

From an existing installation with a current schema:

```bash
media-archive device check local:working
media-archive device check archive:example:a
media-archive device check card:a --to /media/operator/EXAMPLE
media-archive device replace card:a --to /media/operator/EXAMPLE
media-archive device replace card:a --to /media/operator/EXAMPLE --yes
```

- `check` confirms the current mount in read mode using the configured UUID and any approved hardware evidence.
- `register` approves the hardware evidence for a configured, previously registered binding. It also records the first UUID of a registered card that had none. Setup records configured bindings without inspecting hardware.
- `replace` records a different device for a binding with a known UUID. The previous device can be offline. The new device gets a new ID.
- `reformat` records a new filesystem UUID for the same device. If the registry contains hardware evidence, the attached device must match it. For a device with only UUID evidence, the operator confirms that it is the same hardware.

For a new UUID, update the existing binding in the selected `.env` before running the command. The candidate mount and detected UUID must agree with those settings. Use an absolute `--to` path, required for cards and for `replace` and `reformat`. A local or archive path supplied with `--to` must match its configured path. The preview shows the previous device ID and both UUID endings. Repeat the command with `--yes` to inspect the candidate again and explicitly apply the change. The transaction checks the current binding again and leaves other configuration registrations alone. An earlier UUID remains reserved for its historical episode. These commands emit terminal results. They do not create report files.

The CLI rejects an overlap between the state directory and the resolved `--to` path before opening the database, including a card mount supplied only on the command line. A confirmed binding change requires exclusive access to the installation. If verification is active, retry the change after it ends.

## Runtime verification

`findmnt` reads the kernel mount table. For cards and archives it checks the specified mountpoint. A working local volume root can be a directory inside the mounted filesystem. `lsblk` supplies the filesystem UUID and the physical disk for a directly mounted block disk or a partition on one disk. JSON parsers use explicit output columns. The application rejects missing mounts, unexpected UUIDs, duplicate visible UUIDs, conflicting registered hardware, and ambiguous block-device layouts. An existing directory on the system drive cannot stand in for a disconnected archive. See the [findmnt manual](https://github.com/util-linux/util-linux/blob/master/misc-utils/findmnt.8.adoc) and [lsblk manual](https://kernel.googlesource.com/pub/scm/utils/util-linux/util-linux/+/refs/tags/v2.41.1/misc-utils/lsblk.8.adoc).

Hardware evidence uses a WWN or serial when exposed and unique in the current block-device inventory. An identifier is trusted only after explicit registration. USB adapters and readers can hide or report their own identifiers. Read access can use an unambiguous registered UUID when no hardware evidence has been approved. A lone clone with the same UUID can therefore match a UUID-only registration. The writable check requires approved hardware evidence and write access; cards and archive targets configured as `read_only` cannot pass it. The writable check does not make a trial write.

The MVP expects ordinary Linux block-device mounts and supports the distinct filesystem device number used by FUSE block mounts such as ntfs-3g. Bind mounts rooted in a subtree and storage layouts with no single directly identifiable physical parent are outside this scope. The feature needs Linux procfs and the `findmnt` and `lsblk` utilities from util-linux.

## Python API

`media_archive.devices.verify_volume(config, key, target=None, *, writable=False)` is a context manager. It holds a shared installation lock from the registry read until the context exits and keeps the verified Linux mount open. Use the returned `MountedVolume` inside the context. It includes the mounted root, UUID and current block and physical device numbers. Runtime device numbers can change after reconnecting. They are useful for comparing two volumes during one run. The registry's device and filesystem episode IDs supply durable history.

`open_media_file(volume, relative, *, scope=None)` returns a binary file handle for the caller to close. Supply a configured collection or source directory under the verified root as `scope` for a narrower boundary. The function reopens the expected root, checks the mount and directory identity, and opens the relative path through directory descriptors. It rejects parent traversal, symbolic links, crossings into other mounts, special files and invalid UTF-8. Opening a regular file does not read or hash it. If a disk disappears while a file is being read, the reader must handle the resulting error.

Input and binding errors use `ConfigError`; probe, mount, permission and safe-open failures use `StateError`. Diagnostic errors omit paths and device identifiers. Application commands retain the existing CLI status convention.

[Test coverage](../tests/test_devices.py) uses synthetic command output, temporary files and a temporary SQLite registry. It covers mount and identity checks, read-only roles, safe opens, explicit binding changes, replacement attempts during verification, and rejection of state directories inside a card mount. Physical disks and card readers have not been verified with this implementation. Source inventory and transfers are separate features.
