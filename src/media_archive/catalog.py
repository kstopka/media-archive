"""Browse saved media locations and record explicit catalog decisions."""

from contextlib import ExitStack
from datetime import date, datetime, timezone
from pathlib import Path
import unicodedata

from media_archive._encoding import require_utf8
from media_archive._transfer_files import Destination, TransferConflict
from media_archive.config import ConfigError, Configuration
from media_archive.devices import inspect_volume, registered_volume, verify_volume
from media_archive.operations import check_application_paths
from media_archive.sort_conflicts import require_destination_name
from media_archive.sources import SourceDigest
from media_archive.state import DeviceKey, StateError, history, open_database, sort_moves, transaction
from media_archive.state import catalog as state
from media_archive.state.locking import Resource, canonical_path, collection_resource, lock_resources
from media_archive.state.reconciliation import require_reconciled
from media_archive.state.registry import load_registered_devices, validate_registered_configuration


def _name(value: str) -> str:
    return unicodedata.normalize("NFC", value).casefold()


def _path_month(path: Path, month: str) -> bool:
    year, number = month.split("-")
    for part in path.parent.parts:
        if part == f"{number}.{year}":
            return True
        try:
            day = date.fromisoformat(part)
        except ValueError:
            continue
        if day.isoformat() == part and part[:7] == month:
            return True
    return False


def search_catalog(config: Configuration, *, name: str | None = None, month: str | None = None,
                   source: tuple[str, str] | None = None) -> list[dict[str, object]]:
    if name is not None:
        require_utf8(name, StateError)
    if month is not None:
        try:
            valid = date.fromisoformat(f"{month}-01").isoformat() == f"{month}-01"
        except ValueError:
            valid = False
        if not valid:
            raise StateError("Podaj poprawny miesiąc w formacie RRRR-MM.")
    if source is not None:
        require_utf8(source[1], StateError)
    database = config.app.database_file
    with open_database(database) as connection:
        rows = state.load_locations(connection, source, month)
        moved = state.load_moves(connection)
    current_devices = {device.volume_id: device for device in load_registered_devices(database)
                       if device.volume_id is not None}
    available: dict[int, bool] = {}
    results = []
    for row in rows:
        path = Path(row.path)
        if name is not None and _name(name) not in _name(path.name):
            continue
        if month is not None and not (row.matches_sort_month or _path_month(path, month)):
            continue
        label = ":".join(part for part in (row.role, row.group_id, row.alias) if part)
        status = "nie sprawdzano nośnika"
        if row.role in ("local", "archive"):
            if row.volume_id not in available:
                binding = current_devices.get(row.volume_id)
                accessible = False
                if binding is not None:
                    try:
                        with inspect_volume(config, binding.key) as mounted:
                            accessible = (binding.uuid is not None and mounted.uuid.casefold() == binding.uuid.casefold()
                                          and (binding.hardware_id is None or binding.hardware_id in mounted.hardware_ids))
                    except (ConfigError, StateError):
                        pass
                available[row.volume_id] = accessible
            status = "nośnik dostępny" if available[row.volume_id] else "ostatnia znana lokalizacja (nośnik niedostępny)"
        results.append({
            "id": row.id, "rodzaj": row.kind, "ścieżka": row.path, "nośnik": label, "id_woluminu": row.volume_id,
            "dostępność": status, "sha256": row.sha256, "bajty": row.size,
            "obecność": ("przeniesiono" if row.id in moved else "brak przy obserwacji" if row.present == 0
                         else "ostatnio potwierdzona"),
            "obserwowano": row.observed_at, "zweryfikowano": row.verified_at,
            "następna_lokalizacja": moved.get(row.id),
        })
    return results


def list_conflicts(config: Configuration) -> list[dict[str, object]]:
    with open_database(config.app.database_file) as connection:
        state.require_catalog(connection)
        groups = {}
        moves = state.load_moves(connection)
        for row in sort_moves.load_conflicts(connection):
            group = groups.setdefault(row.group_path, {
                "grupa": row.group_path, "nazwa": row.original_name, "zachowaj_oba_od": row.kept_both_at, "warianty": [],
            })
            current = row.occurrence_id
            while current in moves:
                current = moves[current]
            group["warianty"].append({
                "ścieżka": row.path, "id_woluminu": row.volume_id, "sha256": row.sha256, "bajty": row.size,
                "id_lokalizacji": current,
                "przeniesiono_do": history.load_occurrence(connection, current).path if current != row.occurrence_id else None,
            })
    return list(groups.values())


def keep_both(config: Configuration, group: str) -> None:
    require_utf8(group, StateError)
    check_application_paths(config)
    with open_database(config.app.database_file, writable=True) as connection, transaction(connection):
        state.require_catalog(connection)
        require_reconciled(connection)
        sort_moves.keep_conflict_variants(connection, group, datetime.now(timezone.utc).isoformat())


def _volume_key(config: Configuration, path: Path) -> DeviceKey:
    for volume in config.local.volumes:
        if path != volume.root and path.is_relative_to(volume.root):
            return DeviceKey("local", volume.alias)
    raise StateError("Obie ścieżki muszą wskazywać pliki na skonfigurowanych woluminach lokalnych.")


def _require_previous_absent(destination: Destination) -> None:
    try:
        destination.ensure_absent()
    except TransferConflict:
        raise StateError("Pod poprzednią ścieżką nadal znajduje się wpis. Uzgodnienie wymaga faktycznego przeniesienia.") from None


def record_manual_move(config: Configuration, occurrence_id: int, target: Path) -> int:
    """Verify a move already made by the operator and link the matching historical locations."""
    check_application_paths(config)
    database = config.app.database_file
    with open_database(database) as connection:
        state.require_catalog(connection)
        original = history.load_occurrence(connection, occurrence_id)
    if original.kind != "local" or original.content_id is None or original.volume_id is None:
        raise StateError("Uzgodnienie wymaga zapisanej lokalizacji w zbiorze i pełnego SHA-256.")
    previous = Path(original.path)
    require_utf8(str(target), StateError)
    if ("\x00" in str(target) or not target.is_absolute() or ".." in target.parts or target == previous
            or canonical_path(target) != target or canonical_path(previous) != previous):
        raise StateError("Podaj inną bezwzględną ścieżkę pliku bez dowiązań symbolicznych.")
    try:
        collection = config.collection(original.collection_id)
        owner = config.collection_for_path(collection.id, target)
    except (KeyError, ValueError):
        raise StateError("Nowa ścieżka musi należeć do zapisanego zbioru.") from None
    if owner.id != collection.id or any(item.local_root.is_relative_to(target) for item in config.collections):
        raise StateError("Nowa ścieżka musi wskazywać plik w tym samym zbiorze.")
    if any(target.is_relative_to(canonical_path(root)) for root in (config.local.staging_root, config.local.undated_root)):
        raise StateError("Nowa ścieżka nie może leżeć w katalogu roboczym.")
    from_key, to_key = _volume_key(config, previous), _volume_key(config, target)
    resources = {collection_resource(config, collection.id), Resource("collection", str(previous.parent))}
    try:
        with lock_resources(database, resources), ExitStack() as stack:
            validate_registered_configuration(config)
            with open_database(database) as connection:
                require_reconciled(connection)
                previous_ids = state.unmoved_location_ids(connection, original)
                if occurrence_id not in previous_ids:
                    raise StateError("Ta lokalizacja została już uzgodniona. Użyj jej następcy z katalogu.")
                state.require_sort_cleanup(connection, previous_ids)
                content = history.load_content(connection, original.content_id)
            from_binding, to_binding = registered_volume(config, from_key), registered_volume(config, to_key)
            if from_binding.volume_id != original.volume_id or to_binding.volume_id is None:
                raise StateError("Wolumin nie odpowiada zapisanej lokalizacji.")
            from_volume = stack.enter_context(verify_volume(config, from_key))
            to_volume = from_volume if from_key == to_key else stack.enter_context(verify_volume(config, to_key))
            old = Destination(from_volume, previous.relative_to(from_volume.root))
            new = Destination(to_volume, target.relative_to(to_volume.root))
            _require_previous_absent(old)
            require_destination_name(new)
            digest = new.recover_digest(sync=False)
            if digest != SourceDigest(content.sha256, content.size):
                raise StateError("Nowa ścieżka nie zawiera pliku o zapisanym SHA-256 i rozmiarze.")
            _require_previous_absent(old)
            with open_database(database, writable=True) as connection, transaction(connection):
                run = history.create_run(connection)
                now = datetime.now(timezone.utc)
                current_id = history.record_occurrence(connection, "local", target.as_posix(),
                                                       content_id=content.id, collection_id=collection.id,
                                                       volume_id=to_binding.volume_id)
                history.record_observation(connection, current_id, run, observed_at=now, present=True, size=digest.size)
                history.record_verification(connection, current_id, run, verified_at=now, scope="full",
                                            read_sha256=digest.sha256, bytes_read=digest.size)
                for previous_id in previous_ids:
                    history.record_observation(connection, previous_id, run, observed_at=now, present=False)
                    state.record_move(connection, previous_id, current_id)
    except OSError:
        raise StateError("Nie można bezpiecznie sprawdzić obu lokalizacji.") from None
    return current_id
