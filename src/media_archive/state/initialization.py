"""Persistent initialization progress. Inventory workflows supply the evidence."""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import select
from sqlalchemy.engine import Connection, RowMapping

from media_archive._encoding import require_utf8

from . import _history_tables as history, _registry_tables as registry
from ._history_queries import find, require_row, require_write_transaction
from .errors import StateError
from .history import load_occurrence

Policy = Literal["import_missing", "baseline_skip_missing"]
Phase = Literal["comparison", "processing"]


@dataclass(frozen=True)
class Initialization:
    status: Literal["pending", "in_progress", "completed"]
    policy: Policy | None = None
    boundary: str | None = None
    baseline_device_id: int | None = None
    started_run_id: str | None = None
    completed_run_id: str | None = None
    scopes: tuple[RowMapping, ...] = ()


def _scope_names(values: Iterable[str]) -> tuple[str, ...]:
    if isinstance(values, str):
        raise StateError("Wymagany jest zbiór nazw zakresów inicjalizacji.")
    names = tuple(values)
    if not names or any(not isinstance(name, str) or not name.strip() for name in names):
        raise StateError("Inicjalizacja wymaga nazwanych zakresów porównania i przetwarzania.")
    if len(set(names)) != len(names):
        raise StateError("Powtórzony zakres inicjalizacji.")
    for name in names:
        require_utf8(name, StateError)
    return names


def start_initialization(
    connection: Connection, source_id: int, run_id: str, *, policy: Policy, boundary: str,
    comparison_scopes: Iterable[str], processing_scopes: Iterable[str], baseline_device_id: int | None = None,
) -> None:
    """Pin the policy, inventory boundary and required scopes for a registered source."""
    require_write_transaction(connection)
    require_utf8(run_id, StateError)
    source = require_row(connection, registry.sources, id=source_id)
    if policy not in ("import_missing", "baseline_skip_missing") or not isinstance(boundary, str) or not boundary.strip():
        raise StateError("Inicjalizacja wymaga obsługiwanej polityki i granicy spisu.")
    require_utf8(boundary, StateError)
    if policy == "baseline_skip_missing":
        if source.kind != "phone" or baseline_device_id is None:
            raise StateError("Zachowanie selekcji wymaga urządzenia przypisanego do profilu telefonu.")
        device = require_row(connection, registry.devices, id=baseline_device_id)
        slot = require_row(connection, registry.device_slots, id=device.slot_id)
        if slot.role != "phone" or slot.source_id != source_id:
            raise StateError("Urządzenie początkowego spisu nie należy do profilu.")
    elif baseline_device_id is not None:
        raise StateError("Urządzenie początkowej selekcji wymaga odpowiedniej polityki.")
    comparison = _scope_names(comparison_scopes)
    processing = _scope_names(processing_scopes)
    if find(connection, history.initializations, source_id=source_id) is not None:
        raise StateError("Profil ma już zapisaną inicjalizację. Wczytaj jej stan.")
    with connection.begin_nested():
        connection.execute(history.initializations.insert().values(
            source_id=source_id, policy=policy, boundary=boundary, baseline_device_id=baseline_device_id,
            started_run_id=run_id,
        ))
        connection.execute(history.scopes.insert(), [
            dict(source_id=source_id, phase=phase, scope=scope, completed=False)
            for phase, scopes in (("comparison", comparison), ("processing", processing))
            for scope in scopes
        ])


def _require_in_progress(connection: Connection, source_id: int) -> RowMapping:
    state = require_row(connection, history.initializations, source_id=source_id)
    if state.completed_run_id is not None:
        raise StateError("Inicjalizacja profilu jest już ukończona.")
    return state


def _has_pending_scope(connection: Connection, source_id: int, phase: Phase | None = None) -> bool:
    scopes = select(history.scopes.c.scope).filter_by(source_id=source_id, completed=False)
    if phase is not None:
        scopes = scopes.filter_by(phase=phase)
    return connection.execute(scopes.limit(1)).first() is not None


def record_initialization_progress(
    connection: Connection, source_id: int, run_id: str, *, phase: Phase, scope: str,
    cursor: str | None = None, completed: bool = False,
) -> None:
    """Save a resumable checkpoint or record evidence of a scope's completion."""
    require_write_transaction(connection)
    _require_in_progress(connection, source_id)
    if (phase not in ("comparison", "processing") or not isinstance(scope, str)
            or type(completed) is not bool or (cursor is not None and not isinstance(cursor, str))):
        raise StateError("Niepoprawny zapis postępu inicjalizacji.")
    require_utf8(run_id, StateError)
    require_utf8(scope, StateError)
    if cursor is not None:
        require_utf8(cursor, StateError)
    checkpoint = require_row(connection, history.scopes, source_id=source_id, phase=phase, scope=scope)
    if checkpoint.completed:
        if completed and (checkpoint.run_id, checkpoint.cursor) == (run_id, cursor):
            return
        raise StateError("Zakończony zakres inicjalizacji ma już zapisany wynik.")
    if phase == "processing" and _has_pending_scope(connection, source_id, "comparison"):
        raise StateError("Przetwarzanie wymaga zakończenia porównania.")
    require_row(connection, history.runs, id=run_id)
    connection.execute(
        history.scopes.update().filter_by(source_id=source_id, phase=phase, scope=scope)
        .values(cursor=cursor, completed=completed, run_id=run_id)
    )


def record_baseline_skip(connection: Connection, occurrence_id: int, run_id: str) -> None:
    """Record the workflow's decision to skip a known phone occurrence in the baseline."""
    require_write_transaction(connection)
    require_utf8(run_id, StateError)
    occurrence = load_occurrence(connection, occurrence_id)
    if occurrence.kind != "source" or occurrence.content_id is None:
        raise StateError("Pominięcie wymaga rozpoznanej treści źródłowej.")
    state = _require_in_progress(connection, occurrence.source_id)
    if state.policy != "baseline_skip_missing" or state.baseline_device_id != occurrence.device_id:
        raise StateError("Wystąpienie nie należy do początkowego spisu telefonu.")
    if _has_pending_scope(connection, occurrence.source_id, "comparison"):
        raise StateError("Pominięcie wymaga zakończenia porównania.")
    previous = find(connection, history.baseline_skips, occurrence_id=occurrence.id)
    if previous is not None:
        if previous.run_id != run_id:
            raise StateError("Wystąpienie ma już zapisane pominięcie.")
        return
    connection.execute(history.baseline_skips.insert().values(
        occurrence_id=occurrence.id, source_id=occurrence.source_id, run_id=run_id,
    ))


def finish_initialization(connection: Connection, source_id: int, run_id: str) -> None:
    """Mark initialization complete after every required scope has recorded completion."""
    require_write_transaction(connection)
    require_utf8(run_id, StateError)
    state = require_row(connection, history.initializations, source_id=source_id)
    if state.completed_run_id is not None:
        if state.completed_run_id == run_id:
            return
        raise StateError("Inicjalizacja profilu jest już ukończona.")
    if _has_pending_scope(connection, source_id):
        raise StateError("Ukończenie inicjalizacji wymaga zakończenia wszystkich zakresów.")
    require_row(connection, history.runs, id=run_id)
    connection.execute(history.initializations.update().filter_by(source_id=source_id).values(
        completed_run_id=run_id,
    ))


def load_initialization(connection: Connection, source_id: int) -> Initialization:
    require_row(connection, registry.sources, id=source_id)
    state = find(connection, history.initializations, source_id=source_id)
    if state is None:
        return Initialization("pending")
    scopes = tuple(connection.execute(
        history.scopes.select().filter_by(source_id=source_id)
        .order_by(history.scopes.c.phase, history.scopes.c.scope)
    ).mappings())
    return Initialization(
        "completed" if state.completed_run_id is not None else "in_progress",
        state.policy, state.boundary, state.baseline_device_id, state.started_run_id, state.completed_run_id, scopes,
    )


def load_baseline_skips(connection: Connection, source_id: int) -> tuple[RowMapping, ...]:
    return tuple(connection.execute(
        history.baseline_skips.select().filter_by(source_id=source_id)
        .order_by(history.baseline_skips.c.occurrence_id)
    ).mappings())
