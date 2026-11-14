"""Coordinate access to the working database through its parent directory."""

from collections.abc import Generator
from contextlib import contextmanager
from fcntl import LOCK_EX, LOCK_NB, LOCK_SH, flock
import os
from pathlib import Path

from .errors import StateError


class OperationBusy(StateError):
    """A conflicting operation currently holds a required lock."""


@contextmanager
def installation_lock(database: Path, *, exclusive: bool = False) -> Generator[None, None, None]:
    try:
        descriptor = os.open(database.parent, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        raise StateError("Nie można otworzyć katalogu stanu dla blokady instalacji.") from None
    try:
        try:
            flock(descriptor, (LOCK_EX if exclusive else LOCK_SH) | LOCK_NB)
        except BlockingIOError:
            raise OperationBusy("Instalacja jest używana przez inną operację.") from None
        except OSError:
            raise StateError("Nie można uzyskać blokady instalacji.") from None
        yield
    finally:
        os.close(descriptor)
