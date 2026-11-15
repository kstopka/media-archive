# Read-only sources

The source adapters expose directory entries, attributes and byte streams through Python. They provide the input for later inventory, hashing and transfer operations. They do not write media, create copies, or change the registry.

## Open a source

Use these context managers from `media_archive.sources` and keep all reads inside the context:

| Entry point | Source |
| --- | --- |
| `open_local_source(config, key, *, target=None, scope=None)` | A verified local or archive volume, or a registered card. An optional absolute scope restricts the source to a directory on that volume. Supply the mount as target for a card. |
| `open_camera_source(config, camera_alias, card_alias, mount)` | The specified registered camera card, restricted to the camera profile's `SOURCE_RELATIVE` directory. |
| `open_adb_source(config, phone_alias)` | The registered phone matching the configured serial, restricted to the profile's `CAMERA_DIR`. |

A source holds a shared installation lock for its lifetime. A confirmed replacement cannot change the device binding while that source is open. The local adapter uses the mount and directory checks described in [Linux volume identification](devices.md). The ADB adapter requires `adb` on `PATH` and an authorized Android device that supports `shell_v2`. It selects the configured serial on every remote command, including when other phones are connected. Android's `find`, `stat` and `cat` provide directory entries, attributes and bytes. The adapter passes paths as quoted arguments and uses NUL-delimited listing records. [ADB command reference](https://android.googlesource.com/platform/packages/modules/adb/+/refs/heads/main/docs/user/adb.1.md)

## Read from a source

Both adapters expose the same methods:

| Method | Result |
| --- | --- |
| `list_directory(path=Path("."))` | Immediate entries, sorted by their relative paths. An empty tuple means the directory was read successfully and contained no entries. |
| `attributes(path)` | A `SourceEntry` containing the relative path, POSIX mode, size in bytes, modification and status-change times in nanoseconds, and filesystem device and inode numbers. |
| `read_chunks(path)` | A context manager exposing an iterator of bytes for a regular file. Iterate to EOF to complete the read and check the ADB exit status. Leaving early closes the stream. |
| `capabilities` | Reported timestamp resolution and availability of hashing on the source device and byte-offset resume. |

Paths are relative to the configured source root. Names must be UTF-8 and may contain spaces, quotes, shell characters and newlines. Symlinks and other special entries can appear in listings and attributes. Only regular files can be read, and directory traversal below the root rejects symlinks. The local adapter also rejects crossing into a different mount. The ADB launch may start the local `adb` server. An Android system alias such as `/sdcard` can be part of the configured root.

ADB reports timestamps to the second. The local adapter exposes nanosecond timestamps from the filesystem, whose actual resolution can be coarser. A directory listing and a subsequent read are separate observations. The ADB adapter checks path components on Android using its shell and cannot make those checks atomic with a concurrent rename on the phone.

A disabled or mismatched profile raises `ConfigError`. An unavailable source, authorization failure, unsafe path, malformed listing or interrupted read raises `StateError`. ADB metadata calls and idle reads have a 30-second timeout. Error messages omit private device identifiers and paths. A stream can yield bytes before a later ADB failure, so consumers must treat the read as complete only after successful exhaustion.

## Hash a source file

Call `hash_source_file(source, path, *, progress=None, cancelled=None)` from `media_archive.sources` while the source is open. It returns `SourceDigest(sha256, size)` after reading the whole file with `hashlib.sha256`, reaching a successful end of stream and matching its byte count and attributes. The comparison checks mode, size, timestamps and filesystem identity before and after the read. Both adapters also compare the opened file's attributes with the initial observation before and after streaming bytes from the same descriptor. The ADB shell uses `/proc/self/fd/0` with the opened file attached to standard input. The adapters calculate the digest on the host, using streamed bytes.

`progress(bytes_read, total_bytes)` receives zero at the start and the cumulative count after each chunk. A count at 100% can precede a final stream or attribute error. A `cancelled()` callback returning `True` raises `ReadCancelled` at the next checkpoint. Checkpoints run before I/O, between chunks and before returning the digest. A blocked I/O call must return before a callback can be checked. An error or cancellation yields no digest. This function does not write evidence to SQLite. Audit and import workflows decide when to record it.

Attribute checks detect observed changes. An in-place edit on Android can still go undetected if the size and timestamps reported at one-second precision remain the same. A digest describes the bytes actually received during that read.

[Source tests](../tests/test_sources.py) use temporary files and an ADB stand-in that runs the actual generated shell command locally. They cover source boundaries, quoted and non-UTF-8 names, device selection and authorization, empty listings, disconnection and reads with more than 1,024 file descriptors in use. They also verify digests, progress, cancellation, incomplete streams and source replacement. A physical Android phone was not used in these tests.
