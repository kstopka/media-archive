"""Queries and records for catalog inspection and verified local moves."""

from sqlalchemy import column, func, literal, select, table
from sqlalchemy.engine import Connection, RowMapping

from . import _history_tables as h, _registry_tables as r
from ._history_queries import require_write_transaction
from .errors import StateError
from .journal import operations
from .migrations import schema_version
from .sort_moves import moves as sort_moves


_moves = table("catalog_moves", column("previous_id"), column("current_id"), schema="main")


def require_catalog(connection: Connection) -> None:
    if schema_version(connection) < "0010":
        raise StateError("Katalog wymaga aktualnej bazy. Użyj migrate.")


def load_locations(connection: Connection, source: tuple[str, str] | None,
                   month: str | None) -> tuple[RowMapping, ...]:
    require_catalog(connection)
    occurrence = h.occurrences
    original = occurrence.alias("original")
    observation = h.observations
    verification = h.verifications
    latest = (select(observation.c.id).where(observation.c.occurrence_id == occurrence.c.id)
              .order_by(observation.c.observed_at.desc(), observation.c.id.desc()).limit(1).correlate(occurrence))
    matched = (select(verification.c.verified_at).where(
        verification.c.occurrence_id == occurrence.c.id, verification.c.result == "match",
    ).order_by(verification.c.verified_at.desc(), verification.c.id.desc()).limit(1).scalar_subquery())
    linked_month = literal(False)
    if month is not None:
        target = occurrence.alias("sort_target")
        capture = func.coalesce(func.json_extract(operations.c.intent, "$.manual_date"),
                                func.json_extract(operations.c.intent, "$.metadata.timestamp"))
        linked_month = (select(1).select_from(sort_moves)
                        .join(operations, operations.c.id == sort_moves.c.operation_id)
                        .join(target, target.c.id == operations.c.target_occurrence_id)
                        .where(sort_moves.c.verification_id.is_not(None), target.c.content_id == occurrence.c.content_id,
                               func.substr(capture, 1, 7) == month).correlate(occurrence).exists())
    query = (select(*occurrence.c, h.contents.c.sha256, func.coalesce(h.contents.c.size, observation.c.size).label("size"),
                    observation.c.present, observation.c.observed_at, matched.label("verified_at"),
                    r.device_slots.c.role, r.device_slots.c.alias, r.device_slots.c.group_id,
                    linked_month.label("matches_sort_month"))
             .select_from(occurrence)
             .outerjoin(h.contents, h.contents.c.id == occurrence.c.content_id)
             .outerjoin(observation, observation.c.id == latest.scalar_subquery())
             .outerjoin(r.devices, r.devices.c.id == occurrence.c.device_id)
             .outerjoin(r.device_slots, r.device_slots.c.id == r.devices.c.slot_id)
             .where((observation.c.id.is_not(None) | matched.is_not(None)), occurrence.c.kind != "staging")
             .order_by(occurrence.c.path, occurrence.c.id))
    if source is not None:
        kind, alias = source
        linked = (select(1).select_from(original).join(r.sources, r.sources.c.id == original.c.source_id)
                  .where(r.sources.c.kind == kind, r.sources.c.alias == alias,
                         (original.c.id == occurrence.c.id) | (
                             (occurrence.c.kind != "source") & (original.c.content_id == occurrence.c.content_id)),
                         ).correlate(occurrence).exists())
        query = query.where(linked)
    return tuple(connection.execute(query).mappings())


def load_moves(connection: Connection) -> dict[int, int]:
    return {previous: current for previous, current in connection.execute(select(_moves.c.previous_id, _moves.c.current_id))}


def record_move(connection: Connection, previous_id: int, current_id: int) -> None:
    require_write_transaction(connection)
    connection.execute(_moves.insert().values(previous_id=previous_id, current_id=current_id))


def unmoved_location_ids(connection: Connection, original: RowMapping) -> tuple[int, ...]:
    occurrences = h.occurrences
    query = (select(occurrences.c.id)
             .filter_by(kind="local", path=original.path, content_id=original.content_id, volume_id=original.volume_id)
             .where(occurrences.c.id.not_in(select(_moves.c.previous_id))))
    return tuple(connection.scalars(query))


def require_sort_cleanup(connection: Connection, occurrence_ids: tuple[int, ...]) -> None:
    pending = (select(sort_moves.c.operation_id).select_from(sort_moves)
               .join(operations, operations.c.id == sort_moves.c.operation_id)
               .where(sort_moves.c.cleanup_done.is_(False),
                      (sort_moves.c.source_id.in_(occurrence_ids))
                      | (operations.c.target_occurrence_id.in_(occurrence_ids))).limit(1))
    if connection.scalar(pending) is not None:
        raise StateError("Przed uzgodnieniem lokalizacji dokończ oczekującą operację SORT.")
