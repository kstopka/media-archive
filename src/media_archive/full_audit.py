"""Full SHA-256 archive audit with per-file SQLite checkpoints."""

from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
import stat
from time import monotonic

from sqlalchemy.engine import Connection, RowMapping

from media_archive._progress import format_count, format_size
from media_archive.audit import QuickAudit, _Scan, _compare, _scan, _select_targets
from media_archive.config import ArchiveTarget, ConfigError, Configuration
from media_archive.devices import VolumeUnavailable, registered_volume
from media_archive.sources import LocalSource, SourceEntry, hash_source_file, open_local_source
from media_archive.state import DeviceKey, StateError, open_database, validate_registered_configuration
from media_archive.state.audit import (
    Checkpoint, confirm_distinct_disks, discard_checkpoints, load_checkpoints, load_full_audit,
    save_checkpoint, start_full_audit, update_full_audit_target,
)
from media_archive.state.locking import Resource, lock_resources


@dataclass
class _Side:
    state: RowMapping
    scan: _Scan = field(default_factory=_Scan)
    hashes: dict[Path, Checkpoint] = field(default_factory=dict)
    hardware: tuple[str, ...] = ()
    source: LocalSource | None = None
    scanned_at: str | None = None
    previous_reads: int = 0
    unavailable: str | None = None


def _save_inventory(side: _Side) -> str:
    return json.dumps({path.as_posix(): entry.volume_fingerprint() for path, entry in side.scan.entries.items()})


def _load_inventory(side: _Side, stored: dict[Path, Checkpoint]) -> bool:
    if side.state.inventory is None:
        return False
    entries = {}
    for name, attributes in json.loads(side.state.inventory).items():
        mode, size, modified, changed, inode = json.loads(attributes)
        path = Path(name)
        entries[path] = SourceEntry(path, mode, size, modified, changed, 0, inode)
    if any(path not in stored or stored[path].attributes != entry.volume_fingerprint()
           for path, entry in entries.items() if stat.S_ISREG(entry.mode)):
        raise StateError("Zapis pełnego audytu nie zawiera wszystkich wymaganych skrótów.")
    side.scan.entries = entries
    side.scan.pending.clear()
    side.hashes = {path: stored[path] for path, entry in entries.items() if stat.S_ISREG(entry.mode)}
    side.scanned_at = side.state.scanned_at
    return True


def load_completed_target(connection: Connection, run_id: str, target: RowMapping,
                          ) -> tuple[dict[Path, SourceEntry], dict[Path, Checkpoint]]:
    """Read only checkpoints belonging to the completed inventory of this target."""
    side = _Side(target)
    if target.scanned_at is None or not _load_inventory(side, load_checkpoints(connection, run_id, target.position)):
        raise StateError("Pełny audyt nie obejmuje jeszcze obu wymaganych nośników.")
    return side.scan.entries, side.hashes


def _invalidate(connection: Connection, run_id: str, side: _Side) -> None:
    update_full_audit_target(connection, run_id, side.state.position,
                             inventory=None, scanned_at=None, hardware=json.dumps(side.hardware))


def _hash_side(connection: Connection, run_id: str, root: Path, side: _Side,
               stored: dict[Path, Checkpoint], announce: Callable[[str], None]) -> None:
    assert side.source is not None
    source = side.source
    _scan(source, side.scan)
    files = {path: entry for path, entry in side.scan.entries.items() if stat.S_ISREG(entry.mode)}
    discard_checkpoints(connection, run_id, side.state.position, (
        path for path, checkpoint in stored.items()
        if (path in files and files[path].volume_fingerprint() != checkpoint.attributes)
        or (path not in files and side.scan.covers(path))
    ))
    total_bytes = sum(entry.size for entry in files.values())
    committed_bytes = 0
    last_message = monotonic()

    def progress(read: int, _size: int) -> None:
        nonlocal last_message
        now = monotonic()
        if now - last_message >= 1:
            announce(f"Nośnik {side.state.alias} · pliki {format_count(len(side.hashes))}/{format_count(len(files))} · "
                     f"odczytano {format_size(committed_bytes + read)} / {format_size(total_bytes)}")
            last_message = now

    announce(f"Nośnik {side.state.alias} · do sprawdzenia {format_count(len(files))} plików, {format_size(total_bytes)}.")
    for path, entry in files.items():
        old = stored.get(path)
        if old is not None and old.attributes != entry.volume_fingerprint():
            old = None  # Already discarded after listing.
        if old is not None:
            try:
                current = source.attributes(path)
            except StateError as error:
                side.scan.errors[path] = str(error)
                discard_checkpoints(connection, run_id, side.state.position, (path,))
                continue
            if current != entry:
                discard_checkpoints(connection, run_id, side.state.position, (path,))
                old = None
                if not stat.S_ISREG(current.mode):
                    side.scan.errors[path] = "Plik źródłowy zmienił typ od inwentaryzacji."
                    continue
                total_bytes += current.size - entry.size
                side.scan.entries[path] = entry = current
        if old is not None:
            side.hashes[path] = old
        else:
            try:
                digest = hash_source_file(source, path, progress=progress, expected=entry)
            except StateError as error:
                side.scan.errors[path] = str(error)
                continue
            side.hashes[path] = save_checkpoint(
                connection, run_id, side.state.position, side.state.volume_id, root, path,
                entry.volume_fingerprint(), digest.size, entry.modified_ns, digest.sha256, datetime.now(timezone.utc),
            )
        committed_bytes += entry.size
        progress(0, 0)

    if not side.scan.errors:
        scanned_at = datetime.now(timezone.utc).isoformat()
        update_full_audit_target(connection, run_id, side.state.position, inventory=_save_inventory(side),
                                 scanned_at=scanned_at, hardware=json.dumps(side.hardware))
        side.scanned_at = scanned_at
    announce(f"Nośnik {side.state.alias} · sprawdzono {format_count(len(side.hashes))}/{format_count(len(files))} plików.")


def _distinct(sides: list[_Side], previously_confirmed: bool) -> bool:
    first, second = sides
    if first.source is not None and second.source is not None:
        if first.source.physical_device == second.source.physical_device:
            raise StateError("Audyt wymaga dwóch różnych fizycznych nośników.")
        return True
    if set(first.hardware) & set(second.hardware):
        raise StateError("Odczyty kolejno podłączanych woluminów wskazują ten sam fizyczny nośnik.")
    return previously_confirmed or bool({value.partition(":")[0] for value in first.hardware}
                                        & {value.partition(":")[0] for value in second.hardware})


def _result(run_id: str, group_id: str, root: Path, sides: list[_Side], *,
            distinct: bool, interrupted: bool, write_error: str | None) -> QuickAudit:
    first, second = sides
    _, differences = _compare(first.scan, second.scan)
    matches = 0
    for path in sorted(first.hashes.keys() & second.hashes.keys()):
        if first.hashes[path].sha256 == second.hashes[path].sha256:
            matches += 1
        else:
            differences.append({"rodzaj": "inna treść", "ścieżka": path.as_posix()})
    errors = [
        {"nośnik": side.state.alias, "ścieżka": path.as_posix(), "błąd": error}
        for side in sides for path, error in sorted(side.scan.errors.items())
    ]
    if interrupted:
        code, message = 130, "Przerwano pełny audyt. Ukończone odczyty pozostają zapisane."
    elif write_error is not None:
        code, message = 3, "Nie zapisano dalszego postępu w SQLite. Raport zawiera zapisane odczyty i ID do wznowienia."
    elif not any(side.source for side in sides):
        code, message = 3, "Nie odczytano żadnego nośnika w tym wywołaniu. Podłącz dysk i wznów audyt."
    elif not all(side.scanned_at for side in sides):
        code, message = 3, "Pełny audyt ma niepełny zakres. Wznów go po podłączeniu wymaganych nośników."
    elif not distinct:
        code, message = 3, "Nie potwierdzono odrębności fizycznej dysków. Podłącz oba jednocześnie i wznów audyt."
    elif differences:
        code, message = 4, "Pełny audyt wykrył różnice."
    else:
        code, message = 0, "Treść plików jest zgodna według SHA-256 w zakresach i chwilach odczytu obu dysków."
    details = {
        "ID audytu": run_id, "Grupa archiwalna": group_id, "Korzeń archiwum": root.as_posix(),
        "Potwierdzone różne dyski": distinct,
        "Nośniki": [
            {"alias": side.state.alias, "pełny skan": side.scanned_at is not None,
             "zamontowany w tym wywołaniu": side.source is not None, "skan ukończono": side.scanned_at,
             "zweryfikowane pliki": len(side.hashes), "punkty wznowienia na odłączonym dysku": side.previous_reads,
             "katalogi do przeskanowania": len(side.scan.pending) if side.source is not None else 0,
             "pliki do odczytu": sum(stat.S_ISREG(entry.mode) and path not in side.hashes
                                     and path not in side.scan.errors for path, entry in side.scan.entries.items()),
             **({"uwaga": side.unavailable} if side.unavailable is not None else {})}
            for side in sides
        ],
        "Zgodne pliki SHA-256": matches, "Liczba różnic": len(differences),
        "Liczba błędów skanowania": len(errors), "Różnice": differences, "Błędy skanowania": errors,
        "Odczyty SHA-256": [
            {"nośnik": side.state.alias, "ścieżka": path.as_posix(), "odczytano": checkpoint.read_at,
             "sha256": checkpoint.sha256}
            for side in sides for path, checkpoint in sorted(side.hashes.items())
        ],
    }
    if write_error is not None:
        details["Błąd zapisu stanu"] = write_error
    return QuickAudit(code, message, details)


def full_audit(config: Configuration, group_id: str | None, targets: tuple[str, str] | None, *,
               resume: str | None, announce: Callable[[str], None]) -> QuickAudit:
    database = config.app.database_file
    if resume is not None:
        with open_database(database) as connection:
            run, saved = load_full_audit(connection, resume)
        group_id = run.group_id
        targets = (saved[0].alias, saved[1].alias)
    if group_id is None or targets is None:
        raise ConfigError("Pełny audyt wymaga grupy i pary nośników.")
    group, selected = _select_targets(config, group_id, *targets)
    root = Path(group.root_relative)
    keys = [DeviceKey("archive", target.alias, group.id) for target in selected]

    with lock_resources(database, [Resource("archive", group.id)]):
        validate_registered_configuration(config)
        episodes = [registered_volume(config, key).volume_id for key in keys]
        if any(episode is None for episode in episodes) or episodes[0] == episodes[1]:
            raise StateError("Pełny audyt wymaga dwóch zarejestrowanych woluminów.")
        with open_database(database, writable=True) as connection:
            run_id = resume if resume is not None else start_full_audit(
                connection, group.id, root, zip(targets, episodes, strict=True),
            )
            run, saved = load_full_audit(connection, run_id)
            if list(episodes) != [row.volume_id for row in saved] or run.root != root.as_posix():
                raise ConfigError("Zakres lub woluminy audytu uległy zmianie. Rozpocznij nowy pełny audyt.")
            return _run_audit(config, connection, run, saved, selected, announce)


def _run_audit(config: Configuration, connection: Connection, run: RowMapping, saved: tuple[RowMapping, ...],
               selected: tuple[ArchiveTarget, ArchiveTarget], announce: Callable[[str], None]) -> QuickAudit:
    run_id, group_id, root = run.id, run.group_id, Path(run.root)
    sides = [_Side(row, hardware=tuple(json.loads(row.hardware))) for row in saved]
    distinct = run.distinct_disks
    interrupted = False
    write_error = None
    try:
        announce(f"ID pełnego audytu · {run_id}")
        with ExitStack() as stack:
            for side, target in zip(sides, selected, strict=True):
                key = DeviceKey("archive", target.alias, group_id)
                try:
                    source = stack.enter_context(open_local_source(config, key, scope=target.mount / root))
                    old_ids = dict(value.split(":", 1) for value in side.hardware)
                    current_ids = dict(value.split(":", 1) for value in source.hardware_ids)
                    if any(current_ids[kind] != old_ids[kind] for kind in old_ids.keys() & current_ids.keys()):
                        raise StateError("Zmieniła się tożsamość sprzętowa woluminu zapisanego w audycie.")
                    side.source = source
                    side.hardware = tuple(f"{kind}:{value}" for kind, value in (old_ids | current_ids).items())
                except VolumeUnavailable as error:
                    stored = load_checkpoints(connection, run_id, side.state.position)
                    if not _load_inventory(side, stored):
                        side.previous_reads = len(stored)
                        side.unavailable = str(error)
                except StateError as error:
                    _invalidate(connection, run_id, side)
                    side.scan.errors[Path(".")] = str(error)
                else:
                    _invalidate(connection, run_id, side)
            distinct = _distinct(sides, distinct)
            if distinct and not run.distinct_disks:
                confirm_distinct_disks(connection, run_id)
            for side in sides:
                if side.source is not None:
                    stored = load_checkpoints(connection, run_id, side.state.position)
                    try:
                        _hash_side(connection, run_id, root, side, stored, announce)
                    except StateError as error:
                        write_error = str(error)
                        break
    except KeyboardInterrupt:
        interrupted = True
    return _result(run_id, group_id, root, sides, distinct=distinct, interrupted=interrupted, write_error=write_error)
