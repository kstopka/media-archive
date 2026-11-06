"""Local collections, reusable policies and explicit backup membership."""

from pathlib import Path
from collections.abc import Sequence
from typing import Literal, Protocol, TypeVar

from .base import ConfigModel
from .field_types import AbsolutePath, NonEmpty, RegistryId, RelativePath


class BackupPolicy(ConfigModel):
    id: RegistryId
    changes: Literal["keep_both", "replace_verified"]
    deletions: Literal["keep", "confirm_each_run"]


class LocalCollection(ConfigModel):
    id: RegistryId
    label: NonEmpty
    local_root: AbsolutePath
    archive_relative: RelativePath
    policy_id: RegistryId
    # A child explicitly overrides its parent policy. Its archive path must
    # preserve the same relative suffix; this prevents double processing.
    parent_id: RegistryId | None = None
    layout: Literal["plain", "year_month", "camera_day"] = "plain"
    # Each child is an explicit grant, never inherited from a parent.
    allow_delete: bool = False


class BackupProfile(ConfigModel):
    id: RegistryId
    label: NonEmpty
    archive_group_id: RegistryId
    # Only top-level collections: all their children are included by "All".
    collection_ids: tuple[RegistryId, ...]
    month_filter: Literal["pending_selected_targets"] = "pending_selected_targets"


class CollectionView:
    """Pure configuration lookups. No scans, transfers or history writes."""

    collections: tuple[LocalCollection, ...]
    policies: tuple[BackupPolicy, ...]
    backup_profiles: tuple[BackupProfile, ...]

    def collection(self, collection_id: str) -> LocalCollection:
        return next_item(self.collections, collection_id)

    def policy(self, policy_id: str) -> BackupPolicy:
        return next_item(self.policies, policy_id)

    def backup_profile(self, profile_id: str) -> BackupProfile:
        return next_item(self.backup_profiles, profile_id)

    def profile_collections(self, profile_id: str) -> tuple[LocalCollection, ...]:
        """Top-level scopes to scan once; child rules are resolved by path."""
        return tuple(self.collection(key) for key in self.backup_profile(profile_id).collection_ids)

    def month_collections(self, collection_id: str) -> tuple[LocalCollection, ...]:
        """Configured month roots only; availability needs future disk scans."""
        root = self.collection(collection_id).local_root
        return tuple(item for item in self.collections
                     if item.local_root.is_relative_to(root) and item.layout != "plain")

    def collection_for_path(self, collection_id: str, path: Path) -> LocalCollection:
        """Nearest explicit child wins, independently of the selected scope."""
        root = self.collection(collection_id).local_root
        if not path.is_absolute() or ".." in path.parts or not path.is_relative_to(root):
            raise ValueError("Ścieżka poza zakresem zbioru.")
        matches = (item for item in self.collections
                   if item.local_root.is_relative_to(root) and path.is_relative_to(item.local_root))
        return max(matches, key=lambda item: len(item.local_root.parts))

    def policy_for_path(self, collection_id: str, path: Path) -> BackupPolicy:
        return self.policy(self.collection_for_path(collection_id, path).policy_id)


class _Identified(Protocol):
    @property
    def id(self) -> str: ...


Item = TypeVar("Item", bound=_Identified)


def next_item(items: Sequence[Item], item_id: str) -> Item:
    for item in items:
        if item.id == item_id:
            return item
    raise KeyError(item_id)
