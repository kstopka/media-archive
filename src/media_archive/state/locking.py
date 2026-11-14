"""Process-held Linux locks for operations on registered resources."""

from collections.abc import Generator, Iterable
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
import errno
from fcntl import LOCK_EX, LOCK_NB, LOCK_SH, flock
from hashlib import sha256
import os
from pathlib import Path
import stat
from typing import Literal
from uuid import uuid4

from media_archive._encoding import require_utf8
from media_archive.config import Configuration

from .database import _database_path
from .errors import StateError


@dataclass(frozen=True)
class Resource:
    kind: Literal["collection", "archive", "source"]
    id: str

    def __post_init__(self) -> None:
        if (self.kind not in ("collection", "archive", "source") or not isinstance(self.id, str)
                or not self.id or "\x00" in self.id):
            raise StateError("Niepoprawny zasób blokady operacji.")
        require_utf8(self.id, StateError)
        if self.kind == "collection" and not Path(self.id).is_absolute():
            raise StateError("Blokada zbioru wymaga bezwzględnej ścieżki.")


def canonical_path(path: Path) -> Path:
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError):
        raise StateError("Nie można ustalić położenia zasobu dla blokad.") from None
    require_utf8(str(resolved), StateError)
    return resolved


def collection_resource(config: Configuration, collection_id: str) -> Resource:
    """Use the same lock for a root collection and all of its descendants."""
    collection = config.collection(collection_id)
    while collection.parent_id is not None:
        collection = config.collection(collection.parent_id)
    return Resource("collection", str(canonical_path(collection.local_root)))


class OperationBusy(StateError):
    """A conflicting process currently holds a required resource."""


@dataclass(frozen=True, eq=False)
class OperationLocks:
    """A live lock group, usable only in the process that acquired it."""

    _database: Path = field(repr=False)
    _resources: frozenset[Resource] = field(repr=False)
    _owner: int = field(default_factory=os.getpid, init=False)
    _token: str = field(default_factory=lambda: uuid4().hex, init=False, repr=False)

_active_locks: set[OperationLocks] = set()


def require_locks(locks: OperationLocks, database: Path, resources: Iterable[Resource]) -> str:
    if (type(locks) is not OperationLocks or locks not in _active_locks or locks._owner != os.getpid()
            or database != locks._database or not locks._resources.issuperset(resources)):
        raise StateError("Operacja wymaga aktywnych blokad wszystkich zapisanych zasobów.")
    return locks._token


def _lock_file(stack: ExitStack, path: Path, mode: int) -> None:
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        stack.callback(os.close, fd)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise StateError("Niepoprawny plik blokady operacji.")
        flock(fd, mode | LOCK_NB)
    except OSError as error:
        if error.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
            raise OperationBusy("Inna operacja używa wymaganego zasobu.") from None
        raise StateError("Nie można uzyskać blokady operacji.") from None


@contextmanager
def lock_resources(
    database: str | Path, resources: Iterable[Resource],
) -> Generator[OperationLocks, None, None]:
    """Acquire every requested lock or release the entire group on failure."""
    requested = frozenset(resources)
    if not requested:
        raise StateError("Operacja wymaga co najmniej jednego zasobu.")
    candidate = _database_path(database)
    try:
        info = candidate.lstat()
        canonical = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        raise StateError("Nie można ustalić położenia bazy dla blokad.") from None
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise StateError("Blokady wymagają zwykłego pliku bazy z jedną nazwą.")
    require_utf8(str(canonical), StateError)
    modes = {(resource.kind, resource.id): LOCK_EX for resource in requested}
    for resource in requested:
        if resource.kind == "collection":
            root = Path(resource.id)
            if str(canonical_path(root)) != resource.id:
                raise StateError("Blokada zbioru wymaga kanonicznej ścieżki.")
            for parent in root.parents:
                modes.setdefault((resource.kind, str(parent)), LOCK_SH)
    with ExitStack() as stack:
        for (kind, resource_id), mode in sorted(modes.items()):
            key = f"{canonical.name}\0{kind}\0{resource_id}"
            digest = sha256(key.encode("utf-8")).hexdigest()
            _lock_file(stack, canonical.parent / f".media-archive-lock-{digest}", mode)
        locks = OperationLocks(canonical, requested)
        _active_locks.add(locks)
        try:
            yield locks
        finally:
            _active_locks.remove(locks)
