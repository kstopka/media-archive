"""Translate named .env entries into the universal configuration models."""

from pathlib import Path
import re

from .builders import build, fields
from .collections import BackupPolicy, BackupProfile, LocalCollection
from .env import (
    ARCHIVE_GROUP_KEY, ARCHIVE_TARGET_KEY, BACKUP_KEY, CAMERA_CARD_KEY, COLLECTION_KEY,
    CAMERA_KEY, PHONE_KEY, POLICY_KEY, VOLUME_KEY, entry_names,
)
from .errors import ConfigError
from .models import (
    AppSettings, ArchiveGroup, ArchiveTarget, Configuration, CameraCard,
    CameraProfile, LocalSettings, LocalVolume, PhoneDevice, PhoneProfile,
)


def names(values: dict[str, str], list_key: str, pattern) -> tuple[str, ...]:
    inferred = {match[1] for key in values if (match := pattern.fullmatch(key))}
    return entry_names(values, list_key, inferred)


def load_registry(values: dict[str, str], source: Path) -> Configuration:
    volumes = tuple(build(LocalVolume, {
        "alias": name.lower(), **fields(values, f"LOCAL_VOLUME_{name}", ("root", "uuid")),
    }, f"LOCAL_VOLUME_{name}") for name in names(values, "LOCAL_VOLUMES", VOLUME_KEY))
    cards = {name: build(CameraCard, {
        "alias": name.lower(), "uuid": values.get(f"CAMERA_CARD_{name}_UUID") or None,
    }, f"CAMERA_CARD_{name}") for name in names(values, "CAMERA_CARDS", CAMERA_CARD_KEY)}

    return build(Configuration, {
        "source_file": source,
        "app": build(AppSettings, fields(values, "APP", (
            "timezone", "config_file", "database_file", "reports_dir", "min_free_bytes",
            "report_keep_runs", "database_keep_generations", "archive_state_dir",
        )), "APP"),
        "local": build(LocalSettings, {"volumes": volumes, **fields(values, "LOCAL", ("staging_root", "undated_root"))}, "LOCAL"),
        "policies": _policies(values),
        "collections": _collections(values),
        "archive_groups": _archives(values),
        "backup_profiles": _backups(values),
        "phones": _phones(values),
        "cameras": _cameras(values, cards),
        "camera_cards": tuple(cards.values()),
    })


def _policies(values: dict[str, str]) -> tuple[BackupPolicy, ...]:
    return tuple(build(BackupPolicy, {
        "id": name.lower(), **fields(values, f"POLICY_{name}", ("changes", "deletions")),
    }, f"POLICY_{name}") for name in names(values, "POLICIES", POLICY_KEY))


def _collections(values: dict[str, str]) -> tuple[LocalCollection, ...]:
    result = []
    for name in names(values, "COLLECTIONS", COLLECTION_KEY):
        prefix = f"COLLECTION_{name}"
        raw = fields(values, prefix, ("label", "local_root", "archive_relative", "layout", "allow_delete"))
        result.append(build(LocalCollection, {
            "id": name.lower(), "label": name.lower(), **raw,
            "parent_id": values.get(f"{prefix}_PARENT") or None,
            "policy_id": values.get(f"{prefix}_POLICY"),
        }, prefix))
    return tuple(result)


def _archives(values: dict[str, str]) -> tuple[ArchiveGroup, ...]:
    result = []
    group_names = names(values, "ARCHIVE_GROUPS", ARCHIVE_GROUP_KEY)
    target_names = _archive_target_names(values, group_names)
    for name in group_names:
        prefix = f"ARCHIVE_{name}"
        targets = tuple(build(ArchiveTarget, {
            "alias": target.lower(),
            **fields(values, f"{prefix}_TARGET_{target}", ("mount", "uuid", "state")),
        }, f"{prefix}_TARGET_{target}") for target in target_names[name])
        result.append(build(ArchiveGroup, {
            "id": name.lower(), "targets": targets,
            **fields(values, prefix, ("root_relative",)),
        }, prefix))
    return tuple(result)


def _archive_target_names(
    values: dict[str, str], group_names: tuple[str, ...],
) -> dict[str, tuple[str, ...]]:
    """Assign each target key to exactly one known group/target pair."""
    listed = {name: entry_names(values, f"ARCHIVE_{name}_TARGETS", set()) for name in group_names}
    inferred: dict[str, set[str]] = {name: set() for name in group_names}
    for key in values:
        if not ARCHIVE_TARGET_KEY.fullmatch(key):
            continue
        candidates = []
        for name in group_names:
            prefix = f"ARCHIVE_{name}_TARGET_"
            if not key.startswith(prefix):
                continue
            target, _ = key[len(prefix):].rsplit("_", 1)
            if not re.fullmatch(r"[A-Z][A-Z0-9_]*", target):
                continue
            if f"ARCHIVE_{name}_TARGETS" in values and target not in listed[name]:
                continue
            candidates.append((name, target))
        if not candidates:
            raise ConfigError("Ustawienia celu wskazują nieznaną grupę lub cel archiwum.")
        if len(candidates) != 1:
            raise ConfigError(f"Niejednoznaczne przypisanie {key} do grupy i celu archiwum.")
        group, target = candidates[0]
        inferred[group].add(target)
    return {
        name: entry_names(values, f"ARCHIVE_{name}_TARGETS", inferred[name])
        for name in group_names
    }


def _backups(values: dict[str, str]) -> tuple[BackupProfile, ...]:
    result = []
    for name in names(values, "BACKUP_PROFILES", BACKUP_KEY):
        prefix = f"BACKUP_{name}"
        result.append(build(BackupProfile, {
            "id": name.lower(), "label": name.lower(),
            **fields(values, prefix, ("label", "month_filter")),
            "archive_group_id": values.get(f"{prefix}_ARCHIVE_GROUP"),
            "collection_ids": tuple(item.lower() for item in entry_names(values, f"{prefix}_COLLECTIONS", set())),
        }, prefix))
    return tuple(result)


def _phones(values: dict[str, str]) -> tuple[PhoneProfile, ...]:
    result = []
    for name in names(values, "PHONE_PROFILES", PHONE_KEY):
        prefix = f"PHONE_{name}"
        alias = values.get(f"{prefix}_ALIAS", name.lower())
        connection = fields(values, prefix, PhoneDevice.model_fields)
        device = build(PhoneDevice, connection, prefix) if any(connection.get(key) for key in ("transport", "serial", "camera_dir")) else None
        result.append(build(PhoneProfile, {
            "alias": alias, "label": values.get(f"{prefix}_LABEL", alias),
            **fields(values, prefix, ("initial_policy",)),
            "collection_id": values.get(f"{prefix}_COLLECTION"), "device": device,
        }, prefix))
    return tuple(result)


def _cameras(values: dict[str, str], cards: dict[str, CameraCard]) -> tuple[CameraProfile, ...]:
    result = []
    for name in names(values, "CAMERA_PROFILES", CAMERA_KEY):
        prefix = f"CAMERA_{name}"
        alias = values.get(f"{prefix}_ALIAS", name.lower())
        selected = entry_names(values, f"{prefix}_CARDS", set())
        if any(card not in cards for card in selected):
            raise ConfigError(f"{prefix}_CARDS zawiera niezarejestrowaną kartę.")
        result.append(build(CameraProfile, {
            "alias": alias, "label": values.get(f"{prefix}_LABEL", alias),
            **fields(values, prefix, ("model", "camera_folder", "source_relative", "ignore_extensions")),
            "source_example_subdir": values.get(f"{prefix}_SOURCE_EXAMPLE_SUBDIR") or None,
            "collection_id": values.get(f"{prefix}_COLLECTION"),
            "cards": tuple(cards[card] for card in selected),
        }, prefix))
    return tuple(result)
