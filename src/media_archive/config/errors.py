"""Errors safe to display in CLI diagnostics."""


class ConfigError(ValueError):
    """A configuration error whose message never contains private values."""
