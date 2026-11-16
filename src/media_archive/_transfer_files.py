"""Descriptor-based filesystem operations for one verified transfer."""

from collections.abc import Generator
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
from media_archive.sources import SourceDigest
from media_archive.state import StateError

_WORK_NAME = re.compile(r"\.media-archive-transfer-[0-9a-f]{32}", re.IGNORECASE)
_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def is_transfer_part(name: str) -> bool:
    return _WORK_NAME.fullmatch(name) is not None


class TransferConflict(StateError):
    """An existing destination was left in place."""


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

    def preflight(self) -> tuple[Path, ...]:
        """Check the writable directory for the next creation and list missing parents."""
        parts = self.path.parent.parts
        with self._parents(nearest=True) as directories:
            parent = directories[-1]
            depth = len(directories) - 1
            if depth == len(parts):
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

    def recover_digest(self) -> SourceDigest | None:
        """Return fresh proof of an existing regular file, or confirmed absence."""
        try:
            with self.parent() as parent:
                with _open_regular(parent, self.path.name, self.volume.mount_id) as fd:
                    digest, status = _read_digest(fd)
                    self.check_parent(parent)
                    if _signature(os.stat(self.path.name, dir_fd=parent, follow_symlinks=False)) != _signature(status):
                        raise StateError("Plik docelowy zmienił się podczas sprawdzania.")
                    os.fsync(fd)
                    self.check_parent(parent, sync=True)
                    return digest
        except FileNotFoundError:
            # A vanished parent or leaf after opening is not proof of absence.
            self.ensure_absent()
            return None


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

    def publish(self, destination: Destination, verified: os.stat_result) -> None:
        destination.check_parent(self.parent)
        if _signature(os.stat("copy", dir_fd=self.directory, follow_symlinks=False)) != _signature(verified):
            raise StateError("Część zmieniła się od weryfikacji.")
        try:
            try:
                _rename_noreplace(self.directory, "copy", self.parent, destination.path.name)
            except OSError as error:
                if error.errno not in (errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP):
                    raise
                # A filesystem without rename flags can still publish a new name
                # with link(2). Never fall back to a rename that overwrites.
                os.link("copy", destination.path.name, src_dir_fd=self.directory, dst_dir_fd=self.parent,
                        follow_symlinks=False)
                os.fsync(self.parent)
                os.unlink("copy", dir_fd=self.directory)
        except FileExistsError:
            raise TransferConflict("Plik docelowy już istnieje. Pozostawiono go bez zmian.") from None
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
