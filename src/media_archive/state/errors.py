"""State errors safe to display without database paths or SQL values."""


class StateError(RuntimeError):
    """A state operation failed with a safe, application-level diagnostic."""
