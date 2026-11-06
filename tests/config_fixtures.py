"""Synthetic registry configuration fixtures."""

from pathlib import Path

from dotenv import dotenv_values


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def write_config(path: Path, values: dict[str, str]) -> None:
    path.write_text(
        "\n".join(f"{key}='{value}'" for key, value in values.items()) + "\n",
        encoding="utf-8",
    )


def registry_values(root: Path) -> dict[str, str]:
    """Current public schema with synthetic paths/identities."""
    values = {
        key: value for key, value in dotenv_values(
            PROJECT_ROOT / '.env.examples', interpolate=False,
        ).items() if value is not None
    }
    for key, value in values.items():
        if value.startswith('/path/to/'):
            values[key] = str(root / value.removeprefix('/path/to/'))
        elif value.startswith('/home/your-user/'):
            values[key] = str(root / 'system' / value.removeprefix('/home/your-user/'))
        elif value.startswith('REPLACE_WITH_'):
            values[key] = f'test-{key.lower()}'
    return values
