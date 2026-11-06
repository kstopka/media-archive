"""Validate collection trees, references and safe storage boundaries."""

from pathlib import Path, PurePosixPath

from .errors import ConfigError
from .models import Configuration


def _overlap(a: Path, b: Path) -> bool:
    return a.is_relative_to(b) or b.is_relative_to(a)


def _disjoint(paths: list[Path], message: str) -> None:
    for index, path in enumerate(paths):
        if any(_overlap(path, other) for other in paths[index + 1:]):
            raise ConfigError(message)


def _unique(values: list[str], message: str) -> None:
    if len({value.casefold() for value in values}) != len(values):
        raise ConfigError(message)


def validate_relationships(config: Configuration) -> None:
    _registries(config)
    _collections(config)
    _backups(config)
    _sources(config)
    _storage(config)


def _registries(config: Configuration) -> None:
    for items in (config.collections, config.policies, config.archive_groups, config.backup_profiles):
        _unique([item.id for item in items], "Powtórzony identyfikator w rejestrze konfiguracji.")
    if not config.collections or not config.policies:
        raise ConfigError("COLLECTIONS i POLICIES nie mogą być puste.")
    if not config.local.volumes:
        raise ConfigError("LOCAL_VOLUMES wymaga co najmniej jednego woluminu.")
    _unique([volume.alias for volume in config.local.volumes], "Woluminy lokalne wymagają różnych aliasów.")


def _collections(config: Configuration) -> None:
    collection_ids = {item.id for item in config.collections}
    policy_ids = {item.id for item in config.policies}
    if len({item.local_root for item in config.collections}) != len(config.collections):
        raise ConfigError("Zbiory wymagają różnych katalogów lokalnych.")
    for item in config.collections:
        if item.policy_id not in policy_ids:
            raise ConfigError("COLLECTION_POLICY wskazuje nieznaną politykę.")
        if not any(item.local_root.is_relative_to(volume.root) for volume in config.local.volumes):
            raise ConfigError("Katalog zbioru musi należeć do LOCAL_VOLUMES.")
        containers = [parent for parent in config.collections
                      if parent.id != item.id and item.local_root.is_relative_to(parent.local_root)]
        nearest = max(containers, key=lambda parent: len(parent.local_root.parts), default=None)
        if item.parent_id is not None and item.parent_id not in collection_ids:
            raise ConfigError("COLLECTION_PARENT wskazuje nieznany zbiór.")
        if item.parent_id != (nearest.id if nearest else None):
            raise ConfigError("Zagnieżdżony zbiór wymaga jawnego wskazania najbliższego rodzica.")
        if nearest:
            suffix = item.local_root.relative_to(nearest.local_root)
            if PurePosixPath(item.archive_relative) != PurePosixPath(nearest.archive_relative) / suffix:
                raise ConfigError("Cel podzbioru musi zachowywać ścieżkę względną względem rodzica.")
        policy = config.policy(item.policy_id)
        if policy.deletions == "confirm_each_run" and not item.allow_delete:
            raise ConfigError("Usuwanie wymaga jawnego COLLECTION_ALLOW_DELETE; zgoda nie jest dziedziczona.")


def _backups(config: Configuration) -> None:
    groups = {group.id for group in config.archive_groups}
    ids = {item.id for item in config.collections}
    assigned = []
    for profile in config.backup_profiles:
        if profile.archive_group_id not in groups:
            raise ConfigError("BACKUP_ARCHIVE_GROUP wskazuje nieznaną grupę archiwów.")
        if not profile.collection_ids:
            raise ConfigError("Profil backupu wymaga co najmniej jednego zbioru.")
        for key in profile.collection_ids:
            if key not in ids:
                raise ConfigError("BACKUP_COLLECTIONS wskazuje nieznany zbiór.")
            if config.collection(key).parent_id is not None:
                raise ConfigError("Profil backupu wskazuje korzenie zbiorów, bez powtarzania podzbiorów.")
        assigned.extend(profile.collection_ids)
    _unique(assigned, "Zbiór może należeć tylko do jednego profilu backupu.")
    for group in config.archive_groups:
        if not group.targets:
            raise ConfigError("Grupa archiwów wymaga co najmniej jednego celu.")
        _unique([target.alias for target in group.targets], "Cele grupy wymagają różnych aliasów.")
        roots = [config.collection(key) for profile in config.backup_profiles
                 if profile.archive_group_id == group.id for key in profile.collection_ids]
        _disjoint([Path(item.archive_relative) for item in roots], "Nakładające się cele zbiorów w grupie archiwów.")
        if _overlap(Path(config.app.archive_state_dir), Path(group.root_relative)):
            raise ConfigError("APP_ARCHIVE_STATE_DIR musi leżeć poza korzeniami audytu.")


def _sources(config: Configuration) -> None:
    ids = {item.id for item in config.collections}
    _unique([item.alias for item in config.phones], "Profile telefonów wymagają różnych aliasów.")
    _unique([item.alias for item in config.cameras], "Profile kamer wymagają różnych aliasów.")
    _unique([item.alias for item in config.camera_cards], "Karty wymagają różnych aliasów.")
    _unique([item.device.serial for item in config.phones if item.device], "Aktywne telefony wymagają różnych identyfikatorów urządzeń.")
    assigned_cards = [card.alias for item in config.cameras for card in item.cards]
    _unique(assigned_cards, "Karta może być przypisana tylko raz do jednego profilu kamery.")
    for source in (*config.phones, *config.cameras):
        if source.collection_id not in ids:
            raise ConfigError("Profil importu wskazuje nieznany zbiór.")
        collection = config.collection(source.collection_id)
        policy = config.policy(collection.policy_id)
        if policy.changes != "keep_both" or policy.deletions != "keep":
            raise ConfigError("Zbiór importowanych oryginałów wymaga dopisywania bez propagacji usunięć.")
    for source in config.phones:
        if config.collection(source.collection_id).layout != "year_month":
            raise ConfigError("Zbiór telefonu wymaga układu year_month.")
    for source in config.cameras:
        if config.collection(source.collection_id).layout != "camera_day":
            raise ConfigError("Zbiór kamery wymaga układu camera_day.")


def _storage(config: Configuration) -> None:
    volumes = config.local.volumes
    targets = tuple(target for group in config.archive_groups for target in group.targets)
    _unique(
        [volume.uuid for volume in volumes] + [target.uuid for target in targets]
        + [card.uuid for card in config.camera_cards if card.uuid is not None],
        "Zarejestrowane karty i dyski wymagają różnych UUID.",
    )
    _disjoint([volume.root for volume in volumes] + [target.mount for target in targets], "Nakładające się korzenie nośników.")
    staging = config.local.staging_root
    undated = config.local.undated_root
    _disjoint([staging, undated], "Staging i pliki bez daty wymagają rozłącznych katalogów.")
    for path in (staging, undated):
        if any(_overlap(path, item.local_root) for item in config.collections):
            raise ConfigError("Staging i pliki bez daty muszą leżeć poza zbiorami.")
        if any(_overlap(path, target.mount) for target in targets):
            raise ConfigError("Staging i pliki bez daty muszą leżeć poza archiwami.")
    state = [config.source_file, config.app.database_file, config.app.reports_dir]
    _disjoint(state, "Konfiguracja, baza i raporty wymagają rozłącznych ścieżek.")
    roots = [volume.root for volume in volumes] + [target.mount for target in targets] + [staging, undated]
    if any(_overlap(path, root) for path in state for root in roots):
        raise ConfigError("Konfiguracja, baza i raporty muszą pozostać poza woluminami danych i archiwów.")
