"""Application, source and archive DTOs; collection rules live in collections.py."""

from pathlib import PurePosixPath
from typing import Annotated, Literal

from pydantic import Field

from .base import ConfigModel
from .collections import BackupPolicy, BackupProfile, CollectionView, LocalCollection, next_item
from .field_types import (
    AbsolutePath, ConfigFilePath, Identifier, IgnoredExtensions, NonEmpty,
    PositiveCount, RegistryId, RelativePath, ReserveBytes, SingleFolder, Timezone,
)


class AppSettings(ConfigModel):
    timezone: Timezone
    declared_config_file: Annotated[ConfigFilePath, Field(validation_alias="config_file")]
    database_file: AbsolutePath
    reports_dir: AbsolutePath
    min_free_bytes: ReserveBytes
    report_keep_runs: PositiveCount
    database_keep_generations: PositiveCount
    archive_state_dir: SingleFolder


class LocalVolume(ConfigModel):
    alias: Identifier
    root: AbsolutePath
    uuid: Identifier


class LocalSettings(ConfigModel):
    volumes: tuple[LocalVolume, ...]
    staging_root: AbsolutePath
    undated_root: AbsolutePath


class PhoneDevice(ConfigModel):
    transport: Literal["adb"]
    serial: Identifier
    camera_dir: AbsolutePath
    model: str = ""
    android_version: str = ""
    build: str = ""


class PhoneProfile(ConfigModel):
    # Persistent source identity; label is presentation only.
    alias: Identifier
    label: NonEmpty
    collection_id: RegistryId
    initial_policy: Literal["import_missing", "baseline_skip_missing"] = "import_missing"
    device: PhoneDevice | None = None

    @property
    def enabled(self) -> bool:
        return self.device is not None


class CameraCard(ConfigModel):
    alias: Identifier
    uuid: Identifier | None = None


class CameraProfile(ConfigModel):
    alias: Identifier
    label: NonEmpty
    collection_id: RegistryId
    model: NonEmpty
    camera_folder: SingleFolder
    source_relative: RelativePath
    source_example_subdir: RelativePath | None = None
    ignore_extensions: IgnoredExtensions = ()
    cards: tuple[CameraCard, ...]


class ArchiveTarget(ConfigModel):
    alias: Identifier
    mount: AbsolutePath
    uuid: Identifier
    state: Literal["active", "read_only"] = "active"


class ArchiveGroup(ConfigModel):
    id: RegistryId
    root_relative: RelativePath
    targets: tuple[ArchiveTarget, ...]

    def target(self, alias: str) -> ArchiveTarget:
        for target in self.targets:
            if target.alias == alias:
                return target
        raise KeyError(alias)


class Configuration(ConfigModel, CollectionView):
    source_file: AbsolutePath
    app: AppSettings
    local: LocalSettings
    phones: tuple[PhoneProfile, ...]
    cameras: tuple[CameraProfile, ...]
    camera_cards: tuple[CameraCard, ...]
    archive_groups: tuple[ArchiveGroup, ...]
    policies: tuple[BackupPolicy, ...]
    collections: tuple[LocalCollection, ...]
    backup_profiles: tuple[BackupProfile, ...]

    def archive_group(self, group_id: str) -> ArchiveGroup:
        return next_item(self.archive_groups, group_id)

    def phone(self, alias: str) -> PhoneProfile:
        for profile in self.phones:
            if profile.alias == alias:
                return profile
        raise KeyError(alias)

    def camera(self, alias: str) -> CameraProfile:
        for profile in self.cameras:
            if profile.alias == alias:
                return profile
        raise KeyError(alias)

    def archive_mapping(self, collection_id: str, group_id: str) -> str:
        """Compose the group's root and the collection's relative destination.

        Reading configuration does not persist or enforce historical mappings.
        A mount change for the same disk leaves this mapping unchanged.
        """
        group = self.archive_group(group_id)
        collection = self.collection(collection_id)
        return str(PurePosixPath(group.root_relative) / collection.archive_relative)
