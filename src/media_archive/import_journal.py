"""Resolve the durable source and target of a journaled import."""

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path

from sqlalchemy.engine import Connection, RowMapping

from media_archive.state import StateError, history, journal


@dataclass(frozen=True)
class ImportOperation:
    operation: RowMapping
    target: RowMapping
    source: RowMapping
    profile: RowMapping
    last: RowMapping | None


def import_operation_key(kind: str, volume_id: int, destination: Path) -> str:
    fingerprint = json.dumps([volume_id, destination.as_posix()])
    return f"{kind}-import-" + sha256(fingerprint.encode("utf-8")).hexdigest()


def load_import_operation(connection: Connection, operation_id: int) -> ImportOperation:
    operation = journal.load_operation(connection, operation_id)
    target = history.load_occurrence(connection, operation.target_occurrence_id)
    try:
        intent = json.loads(operation.intent)
        kind = intent["kind"]
        origin_id = intent["source_occurrence_id"]
    except (ValueError, KeyError, TypeError):
        raise StateError("Wznowienie obsługuje wyłącznie zapisane operacje importu.") from None
    if target.kind != "staging" or kind not in ("camera_import", "phone_import") or type(origin_id) is not int:
        raise StateError("Wznowienie obsługuje wyłącznie zapisane operacje importu.")
    origin = history.load_occurrence(connection, origin_id)
    if origin.kind != "source" or origin.content_id != target.content_id:
        raise StateError("Zapisane źródło nie pasuje do celu importu.")
    profile = history.load_source_profile(connection, origin.source_id)
    if kind != f"{profile.kind}_import":
        raise StateError("Zapisany profil nie pasuje do rodzaju importu.")

    attempts = journal.load_attempts(connection, operation_id)
    for attempt in reversed(attempts):
        origins = history.load_observed_contents(connection, attempt.run_id, "source")
        if origins:
            if len(origins) != 1 or origins[0].source_id != origin.source_id or origins[0].content_id != target.content_id:
                raise StateError("Zapis odczytu źródła nie pasuje do próby importu.")
            origin = origins[0]
            break
    return ImportOperation(operation, target, origin, profile, attempts[-1] if attempts else None)


def commit_import(connection: Connection, operation_id: int, verification_id: int) -> None:
    operation = load_import_operation(connection, operation_id)
    history.record_import(connection, operation.source.id, verification_id)
