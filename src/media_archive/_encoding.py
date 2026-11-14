"""UTF-8 validation shared by configuration and state APIs."""

from collections.abc import Callable


def require_utf8(value: str, error: Callable[[str], Exception] = ValueError) -> str:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        pass
    else:
        return value
    # Raise outside the handler so the exception does not retain the input.
    raise error("Tekst i ścieżki muszą być zapisywalne w UTF-8.")
