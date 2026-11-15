"""Compare metadata across two registered archive volumes."""

from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
import stat
import unicodedata

from media_archive.config import ConfigError, Configuration
from media_archive.devices import registered_volume
from media_archive.sources import LocalSource, SourceEntry, open_local_source
from media_archive.state import DeviceKey, StateError, open_database, transaction, validate_registered_configuration
from media_archive.state._installation import installation_lock
from media_archive.state.history import create_run, record_observation, record_occurrence
from media_archive.state.locking import Resource, lock_resources

_TYPES = {
    stat.S_IFREG: "plik", stat.S_IFDIR: "katalog", stat.S_IFLNK: "dowiązanie",
    stat.S_IFIFO: "potok", stat.S_IFSOCK: "gniazdo", stat.S_IFCHR: "urządzenie znakowe",
    stat.S_IFBLK: "urządzenie blokowe",
}


@dataclass
class _Scan:
    entries: dict[Path, SourceEntry] = field(default_factory=dict)
    errors: dict[Path, str] = field(default_factory=dict)
    observed_at: dict[Path, datetime] = field(default_factory=dict)
    pending: list[Path] = field(default_factory=lambda: [Path(".")])

    def covers(self, path: Path) -> bool:
        return not any(part in self.errors for part in (path, *path.parents))


@dataclass(frozen=True)
class QuickAudit:
    code: int
    message: str
    details: dict[str, object]


def _scan(source: LocalSource, scan: _Scan) -> None:
    while scan.pending:
        directory = scan.pending[-1]
        try:
            children = source.list_directory(directory)
        except StateError as error:
            scan.errors[directory] = str(error)
            scan.pending.pop()
            continue
        scan.observed_at[directory] = datetime.now(timezone.utc)
        scan.entries.update((entry.path, entry) for entry in children)
        scan.pending[-1:] = [entry.path for entry in children if stat.S_ISDIR(entry.mode)]


def _compare(first: _Scan, second: _Scan) -> tuple[int, list[dict[str, object]]]:
    differences: list[dict[str, object]] = []
    equal = 0
    names: dict[str, list[str]] = {}
    for path in sorted(first.entries.keys() | second.entries.keys()):
        name = path.as_posix()
        names.setdefault(unicodedata.normalize("NFC", name).casefold(), []).append(name)
        a, b = first.entries.get(path), second.entries.get(path)
        if a is None or b is None:
            if a is None and first.covers(path):
                differences.append({"rodzaj": "tylko drugi nośnik", "ścieżka": name})
            if b is None and second.covers(path):
                differences.append({"rodzaj": "tylko pierwszy nośnik", "ścieżka": name})
            continue
        type_a, type_b = stat.S_IFMT(a.mode), stat.S_IFMT(b.mode)
        if type_a != type_b:
            differences.append({"rodzaj": "inny typ", "ścieżka": name,
                                "pierwszy": _TYPES.get(type_a, "inny"), "drugi": _TYPES.get(type_b, "inny")})
        elif stat.S_ISREG(a.mode) and a.size != b.size:
            differences.append({"rodzaj": "inny rozmiar", "ścieżka": name, "pierwszy": a.size, "drugi": b.size})
        else:
            equal += 1
    for paths in names.values():
        if len(paths) > 1:
            differences.append({"rodzaj": "kolizja nazw", "ścieżki": paths})
    return equal, differences


def _write_observations(config: Configuration, root: Path, targets: dict[DeviceKey, _Scan]) -> str:
    episodes = {key: registered_volume(config, key).volume_id for key, scan in targets.items() if scan.entries}
    with open_database(config.app.database_file, writable=True) as connection, transaction(connection):
        run = create_run(connection)
        for key, episode in episodes.items():
            scan = targets[key]
            for entry in scan.entries.values():
                if not stat.S_ISREG(entry.mode):
                    continue
                occurrence = record_occurrence(connection, "archive", (root / entry.path).as_posix(), volume_id=episode)
                record_observation(connection, occurrence, run, observed_at=scan.observed_at[entry.path.parent],
                                   present=True, size=entry.size, mtime_ns=entry.modified_ns)
    return run


def quick_audit(config: Configuration, group_id: str, first: str, second: str, *,
                dry_run: bool = False) -> QuickAudit:
    """Run a metadata-only comparison with explicit coverage and presence observations."""
    try:
        group = config.archive_group(group_id)
        selected = (group.target(first), group.target(second))
    except KeyError:
        raise ConfigError("Wybierz zarejestrowaną grupę i dwa cele archiwalne w tej grupie.") from None
    if first == second:
        raise ConfigError("Audyt wymaga dwóch różnych celów archiwalnych.")

    root = Path(group.root_relative)
    targets = {DeviceKey("archive", target.alias, group.id): _Scan() for target in selected}
    database = config.app.database_file
    lock = installation_lock(database) if dry_run else lock_resources(database, [Resource("archive", group.id)])
    run_id = None
    write_error = None
    interrupted = False
    try:
        with lock, ExitStack() as stack:
            validate_registered_configuration(config)
            sources = {}
            for key, target in zip(targets, selected, strict=True):
                try:
                    sources[key] = stack.enter_context(open_local_source(config, key, scope=target.mount / root))
                except StateError as error:
                    targets[key].errors[Path(".")] = str(error)
                    targets[key].pending.clear()
            if len(sources) == 2 and len({source.physical_device for source in sources.values()}) != 2:
                raise StateError("Audyt wymaga dwóch różnych fizycznych nośników.")
            for key, source in sources.items():
                _scan(source, targets[key])
            if not dry_run:
                try:
                    run_id = _write_observations(config, root, targets)
                except StateError as error:
                    write_error = str(error)
    except KeyboardInterrupt:
        interrupted = True
        for scan in targets.values():
            for path in scan.pending:
                scan.errors.setdefault(path, "Przerwano przed zakończeniem skanowania tego katalogu.")

    scan_a, scan_b = targets.values()
    equal, differences = _compare(scan_a, scan_b)
    errors = [
        {"nośnik": key.alias, "ścieżka": path.as_posix(), "błąd": error}
        for key, scan in targets.items() for path, error in sorted(scan.errors.items())
    ]
    if interrupted:
        code, message = 130, "Przerwano szybki audyt. Raport zawiera dotychczasowe wyniki."
    elif write_error is not None:
        code, message = 3, "Nie zapisano obserwacji w SQLite. Raport zawiera wyniki skanowania."
    elif errors:
        code, message = 3, "Szybki audyt niepełny. Części zakresu nie udało się odczytać."
    elif differences:
        code, message = 4, "Szybki audyt wykrył różnice w ścieżkach lub atrybutach."
    else:
        code, message = 0, "Ścieżki, typy i rozmiary plików są zgodne."
    details = {
        "Grupa archiwalna": group.id,
        "Korzeń archiwum": root.as_posix(),
        "Weryfikacja treści": "nie wykonano SHA-256",
        "ID obserwacji": run_id,
        "Nośniki": [
            {"alias": key.alias, "pełny skan": not scan.errors, "wpisy": len(scan.entries),
             "pliki": sum(stat.S_ISREG(entry.mode) for entry in scan.entries.values()),
             "bajty plików": sum(entry.size for entry in scan.entries.values() if stat.S_ISREG(entry.mode))}
            for key, scan in targets.items()
        ],
        "Zgodne wpisy": equal,
        "Liczba różnic": len(differences),
        "Liczba błędów skanowania": len(errors),
        "Różnice": differences,
        "Błędy skanowania": errors,
    }
    if interrupted:
        details["Odczytane katalogi"] = [
            {"nośnik": key.alias, "ścieżka": path.as_posix()}
            for key, scan in targets.items() for path in sorted(scan.observed_at) if path not in scan.errors
        ]
    if write_error is not None:
        details["Błąd zapisu obserwacji"] = write_error
    return QuickAudit(code, message, details)
