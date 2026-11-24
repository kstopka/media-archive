"""Descriptor-based filesystem operations for one verified transfer."""

from collections.abc import Callable, Generator
from contextlib import ExitStack, contextmanager
import ctypes
from dataclasses import dataclass
import errno
from hashlib import file_digest
import os
from pathlib import Path
import re
import stat
import subprocess

from media_archive.devices import MountedVolume, _mount_id, _open_root
from media_archive.sources import SourceDigest, SourceEntry
from media_archive.state import StateError

_WORK_NAME = re.compile(r"\.media-archive-transfer-[0-9a-f]{32}", re.IGNORECASE)
_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def is_transfer_part(name: str) -> bool:
    return _WORK_NAME.fullmatch(name) is not None


class TransferConflict(StateError):
    """An existing destination was left in place."""


class LinkUnavailable(StateError):
    """The mount refused to publish a hard link."""


def _signature(status: os.stat_result) -> tuple[int, ...]:
    return status.st_dev, status.st_ino, status.st_mode, status.st_size, status.st_mtime_ns, status.st_ctime_ns


def _read_digest(fd: int) -> tuple[SourceDigest, os.stat_result]:
    before = os.fstat(fd)
    with os.fdopen(os.dup(fd), "rb") as stream:
        digest = file_digest(stream, "sha256").hexdigest()
    if _signature(os.fstat(fd)) != _signature(before):
        raise StateError("Kopia zmieniła się podczas niezależnej weryfikacji.")
    return SourceDigest(digest, before.st_size), before


def _rename_noreplace(old_fd: int, old: str, new_fd: int, new: str) -> None:
    # Linux renameat2 is the atomic no-clobber operation, including on volumes without hard links.
    rename = getattr(ctypes.CDLL(None, use_errno=True), "renameat2", None)
    if rename is None:
        raise OSError(errno.ENOSYS, os.strerror(errno.ENOSYS))
    rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    rename.restype = ctypes.c_int
    if rename(old_fd, os.fsencode(old), new_fd, os.fsencode(new), 1) != 0:  # RENAME_NOREPLACE
        number = ctypes.get_errno()
        raise OSError(number, os.strerror(number))


@dataclass(frozen=True)
class Destination:
    volume: MountedVolume
    path: Path  # disk-relative

    @contextmanager
    def _parents(self, *, create: bool = False, nearest: bool = False) -> Generator[list[int], None, None]:
        with ExitStack() as stack:
            try:
                root = _open_root(stack, self.volume.root, self.volume.mount_id)
            except OSError:
                raise StateError("Nie można ponownie otworzyć sprawdzonego korzenia celu.") from None
            if not os.path.samestat(os.fstat(root), os.fstat(self.volume._root_fd)):
                raise StateError("Korzeń celu zmienił się podczas transferu.")
            parent = os.open(".", _DIRECTORY, dir_fd=root)
            stack.callback(os.close, parent)
            directories = [parent]
            for part in self.path.parent.parts:
                try:
                    child = os.open(part, _DIRECTORY, dir_fd=parent)
                except FileNotFoundError:
                    if nearest:
                        break
                    if not create:
                        raise
                    try:
                        os.mkdir(part, dir_fd=parent)
                        os.fsync(parent)
                    except FileExistsError:
                        pass
                    child = os.open(part, _DIRECTORY, dir_fd=parent)
                stack.callback(os.close, child)
                if _mount_id(child) != self.volume.mount_id:
                    raise StateError("Cel przechodzi do innego montowania.")
                directories.append(child)
                parent = child
            yield directories

    @contextmanager
    def parent(self, *, create: bool = False) -> Generator[int, None, None]:
        with self._parents(create=create) as directories:
            yield directories[-1]

    def check_parent(self, parent: int, *, sync: bool = False) -> None:
        with self._parents() as directories:
            if not os.path.samestat(os.fstat(parent), os.fstat(directories[-1])):
                raise StateError("Katalog docelowy zmienił się podczas transferu.")
            if sync:
                # A retry may see directories left visible by a failed fsync.
                for directory in reversed(directories):
                    os.fsync(directory)

    def ensure_absent(self) -> None:
        try:
            with self.parent() as parent:
                os.stat(self.path.name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            return
        raise TransferConflict("Plik docelowy już istnieje. Pozostawiono go bez zmian.")

    def sync_absence(self) -> None:
        """Confirm a removed path and synchronize its surviving directories."""
        with self._parents(nearest=True) as directories:
            self.ensure_absent()
            for directory in reversed(directories):
                os.fsync(directory)
            self.ensure_absent()

    def preflight(self, *, allow_existing: bool = False) -> tuple[Path, ...]:
        """Check the writable directory for the next creation and list missing parents."""
        parts = self.path.parent.parts
        with self._parents(nearest=True) as directories:
            parent = directories[-1]
            depth = len(directories) - 1
            if not allow_existing and depth == len(parts):
                try:
                    os.stat(self.path.name, dir_fd=parent, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    raise TransferConflict("Plik docelowy już istnieje. Pozostawiono go bez zmian.")
            if not os.access(f"/proc/self/fd/{parent}", os.W_OK | os.X_OK, effective_ids=True):
                raise StateError("Brak prawa zapisu w katalogu wymaganym przez transfer do stagingu.")
            return tuple(Path(*parts[:index]) for index in range(depth + 1, len(parts) + 1))

    def space(self) -> tuple[int, int]:
        """Return available bytes and allocation unit at an existing ancestor of the target."""
        with self._parents(nearest=True) as directories:
            info = os.fstatvfs(directories[-1])
            unit = info.f_frsize or info.f_bsize
            if unit <= 0 or info.f_bavail < 0:
                raise StateError("Nie można wiarygodnie oszacować miejsca na stagingu.")
            return info.f_bavail * unit, unit

    def file_size(self) -> int | None:
        """Check that the destination is a regular file on the expected mount."""
        try:
            with self.parent() as parent:
                fd = os.open(self.path.name, os.O_PATH | os.O_NOFOLLOW, dir_fd=parent)
                with _file(fd, self.volume.mount_id):
                    return os.fstat(fd).st_size
        except FileNotFoundError:
            return None

    def sibling_names(self, name_key: Callable[[str], str]) -> tuple[str, ...]:
        """List target names only under an unambiguous spelling of each parent."""
        parts = self.path.parent.parts
        with self._parents(nearest=True) as directories:
            for name, directory in zip(parts, directories):
                matches = [entry for entry in os.listdir(directory) if name_key(entry) == name_key(name)]
                if matches and matches != [name]:
                    raise StateError("Nazwa katalogu docelowego koliduje po normalizacji.")
            if len(directories) != len(parts) + 1:
                return ()
            return tuple(os.listdir(directories[-1]))

    def recover_digest(self, *, sync: bool = True) -> SourceDigest | None:
        """Return fresh proof of an existing regular file, or confirmed absence."""
        try:
            with self.parent() as parent:
                with _open_regular(parent, self.path.name, self.volume.mount_id) as fd:
                    digest, status = _read_digest(fd)
                    self.check_parent(parent)
                    if _signature(os.stat(self.path.name, dir_fd=parent, follow_symlinks=False)) != _signature(status):
                        raise StateError("Plik docelowy zmienił się podczas sprawdzania.")
                    if sync:
                        os.fsync(fd)
                        self.check_parent(parent, sync=True)
                    return digest
        except FileNotFoundError:
            # A vanished parent or leaf after opening is not proof of absence.
            self.ensure_absent()
            return None

    def publish_link(self, source_fd: int, record_proof: Callable[[SourceDigest], int]) -> int | None:
        """Publish directly from a pinned working file without a retained alias."""
        if _mount_id(source_fd) != self.volume.mount_id or os.fstat(source_fd).st_nlink != 1:
            return None
        with self.parent(create=True) as parent:
            digest, status = _read_digest(source_fd)
            if status.st_nlink != 1:
                return None
            os.fsync(source_fd)
            proof = record_proof(digest)
            self.check_parent(parent)
            if _signature(os.fstat(source_fd)) != _signature(status):
                raise StateError("Kopia robocza zmieniła się od weryfikacji.")
            try:
                os.link(f"/proc/self/fd/{source_fd}", self.path.name, dst_dir_fd=parent, follow_symlinks=True)
            except FileExistsError:
                raise TransferConflict("Plik docelowy już istnieje. Pozostawiono go bez zmian.") from None
            except OSError as error:
                if error.errno in (errno.EXDEV, errno.EPERM, errno.EOPNOTSUPP, errno.EMLINK, errno.ENOSYS):
                    raise LinkUnavailable("Wolumin nie obsługuje publikacji przez twarde dowiązanie.") from None
                raise
            self.check_parent(parent, sync=True)
            published = os.stat(self.path.name, dir_fd=parent, follow_symlinks=False)
            # Linking updates ctime on the same inode.
            if _signature(published)[:-1] != _signature(status)[:-1]:
                raise StateError("Opublikowany plik zmienił się przed zapisaniem wyniku.")
            return proof

    @contextmanager
    def keep_verified(self, expected: SourceDigest) -> Generator[Callable[[], None], None, None]:
        """Pin the verified published file and let cleanup recheck it before unlinking."""
        try:
            with self.parent() as parent, _open_regular(parent, self.path.name, self.volume.mount_id) as fd:
                digest, status = _read_digest(fd)
                if digest != expected:
                    raise StateError("Gotowa kopia SORT nie zgadza się z historią. Pozostawiono kopię roboczą.")
                os.fsync(fd)
                self.check_parent(parent, sync=True)

                def confirm() -> None:
                    try:
                        self.check_parent(parent)
                        current = os.stat(self.path.name, dir_fd=parent, follow_symlinks=False)
                    except OSError:
                        raise StateError("Gotowa kopia SORT zniknęła przed usunięciem kopii roboczej.") from None
                    if _signature(current) != _signature(status):
                        raise StateError("Gotowa kopia SORT zmieniła się przed usunięciem kopii roboczej.")

                confirm()
                yield confirm
        except FileNotFoundError:
            raise StateError("Brak gotowej kopii SORT. Pozostawiono kopię roboczą.") from None

    def remove_verified(self, expected: SourceDigest, before_remove: Callable[[], None], *,
                        approved_entry: SourceEntry | None = None) -> None:
        """Unlink an owned copy only while its pinned content and directory still match."""
        try:
            with self.parent() as parent:
                try:
                    with _open_regular(parent, self.path.name, self.volume.mount_id) as fd:
                        digest, status = _read_digest(fd)
                        self.check_parent(parent)
                        actual = os.stat(self.path.name, dir_fd=parent, follow_symlinks=False)
                        if digest != expected or _signature(actual) != _signature(status):
                            raise StateError("Kopia robocza uległa zmianie. Pozostawiono ją na miejscu.")
                        if approved_entry is not None and SourceEntry.from_stat(approved_entry.path, actual) != approved_entry:
                            raise TransferConflict("Zatwierdzony plik archiwalny zmienił się przed usunięciem.")
                        before_remove()
                        self.check_parent(parent)
                        if _signature(os.stat(self.path.name, dir_fd=parent, follow_symlinks=False)) != _signature(status):
                            raise TransferConflict("Plik zmienił się bezpośrednio przed usunięciem.")
                        os.unlink(self.path.name, dir_fd=parent)
                except FileNotFoundError:
                    self.ensure_absent()
                    if approved_entry is not None:
                        raise TransferConflict("Zatwierdzony plik archiwalny zniknął przed usunięciem.") from None
                self.check_parent(parent, sync=True)
        except FileNotFoundError:
            self.ensure_absent()
            if approved_entry is not None:
                raise TransferConflict("Zatwierdzony katalog archiwalny zniknął przed usunięciem.") from None


@contextmanager
def _file(fd: int, mount_id: int) -> Generator[int, None, None]:
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode) or _mount_id(fd) != mount_id:
            raise StateError("Oczekiwano zwykłego pliku na sprawdzonym montowaniu.")
        yield fd
    finally:
        os.close(fd)


@contextmanager
def _open_regular(directory: int, name: str, mount_id: int) -> Generator[int, None, None]:
    # Inspect the pinned entry before opening it for I/O.
    with _file(os.open(name, os.O_PATH | os.O_NOFOLLOW, dir_fd=directory), mount_id) as path_fd:
        fd = os.open(f"/proc/self/fd/{path_fd}", os.O_RDONLY)
        try:
            yield fd
        finally:
            os.close(fd)


@dataclass(frozen=True)
class Part:
    directory: int
    parent: int
    name: str
    mount_id: int

    @classmethod
    @contextmanager
    def create(cls, parent: int, name: str, mount_id: int) -> Generator["Part", None, None]:
        os.mkdir(name, mode=0o700, dir_fd=parent)
        os.fsync(parent)
        directory = os.open(name, _DIRECTORY, dir_fd=parent)
        try:
            if _mount_id(directory) != mount_id:
                raise StateError("Katalog części znajduje się na innym montowaniu.")
            fd = os.open("copy", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
            os.close(fd)
            os.fsync(directory)
            yield cls(directory, parent, name, mount_id)
        finally:
            os.close(directory)

    def rsync_from(self, source_fd: int) -> None:
        try:
            result = subprocess.run(
                ["rsync", "--copy-links", "--inplace", "--ignore-times", "--times", "--",
                 f"/proc/self/fd/{source_fd}", f"/proc/self/fd/{self.directory}/copy"],
                pass_fds=(source_fd, self.directory), stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
            )
        except FileNotFoundError:
            raise StateError("Nie znaleziono programu rsync.") from None
        if result.returncode:
            raise StateError("rsync nie ukończył zapisu części.")

    def verified_copy(self) -> tuple[SourceDigest, os.stat_result]:
        with _open_regular(self.directory, "copy", self.mount_id) as fd:
            digest, status = _read_digest(fd)
            if status.st_nlink != 1:
                raise StateError("Część transferu ma dodatkowe dowiązania.")
            os.fsync(fd)
            return digest, status

    def publish(self, destination: Destination, verified: os.stat_result, *, allow_link_fallback: bool = True,
                previous: SourceDigest | None = None) -> None:
        if previous is None:
            self._publish_new(destination, verified, allow_link_fallback)
        else:
            self._replace_verified(destination, verified, previous)
        os.fsync(self.directory)
        destination.check_parent(self.parent, sync=True)
        published = os.stat(destination.path.name, dir_fd=self.parent, follow_symlinks=False)
        if not os.path.samestat(verified, published) or (published.st_size, published.st_mtime_ns) != (
            verified.st_size, verified.st_mtime_ns
        ):
            raise StateError("Opublikowany plik zmienił się przed zapisaniem wyniku.")
        if os.path.samestat(os.stat(self.name, dir_fd=self.parent, follow_symlinks=False), os.fstat(self.directory)):
            try:
                os.rmdir(self.name, dir_fd=self.parent)
            except OSError:
                pass  # The copy is already durable; retain any remaining directory.

    def _check_part(self, destination: Destination, verified: os.stat_result) -> None:
        destination.check_parent(self.parent)
        if _signature(os.stat("copy", dir_fd=self.directory, follow_symlinks=False)) != _signature(verified):
            raise StateError("Część zmieniła się od weryfikacji.")

    def _replace_verified(self, destination: Destination, verified: os.stat_result, previous: SourceDigest) -> None:
        name = destination.path.name
        try:
            with _open_regular(self.parent, name, self.mount_id) as fd:
                digest, status = _read_digest(fd)
                self._check_part(destination, verified)
                if (digest != previous
                        or _signature(os.stat(name, dir_fd=self.parent, follow_symlinks=False)) != _signature(status)):
                    raise TransferConflict("Starsza kopia zmieniła się przed podmianą. Pozostawiono ją bez zmian.")
                os.replace("copy", name, src_dir_fd=self.directory, dst_dir_fd=self.parent)
        except FileNotFoundError:
            raise TransferConflict("Starsza kopia zniknęła przed podmianą.") from None

    def _publish_new(self, destination: Destination, verified: os.stat_result, allow_link_fallback: bool) -> None:
        self._check_part(destination, verified)
        try:
            try:
                _rename_noreplace(self.directory, "copy", self.parent, destination.path.name)
            except OSError as error:
                if not allow_link_fallback or error.errno not in (errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP):
                    raise
                # A filesystem without rename flags can still publish a new name
                # with link(2). Never fall back to a rename that overwrites.
                os.link("copy", destination.path.name, src_dir_fd=self.directory, dst_dir_fd=self.parent,
                        follow_symlinks=False)
                os.fsync(self.parent)
                os.unlink("copy", dir_fd=self.directory)
        except FileExistsError:
            raise TransferConflict("Plik docelowy już istnieje. Pozostawiono go bez zmian.") from None
