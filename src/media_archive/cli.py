"""Command-line entry point; configuration checks never create runtime state."""

import argparse
from collections.abc import Sequence
import sys

from media_archive.config import ConfigError, load_config


def main(argv: Sequence[str] | None = None) -> int:
    """Show help or validate configuration; archive operations come later."""
    parser = argparse.ArgumentParser(
        prog="media-archive",
        description=(
            "Lokalne narzędzie do importu, porządkowania i archiwizacji mediów."
        ),
        epilog=(
            "Dostępna jest pomoc CLI i sprawdzenie konfiguracji. "
            "IMPORT, SORT, BACKUP i AUDIT nie są jeszcze zaimplementowane."
        ),
        add_help=False,
    )
    parser.add_argument("-h", "--help", action="help", help="pokaż pomoc i zakończ")
    parser.add_argument(
        "--config", metavar="PLIK",
        help="plik konfiguracji (domyślnie .env w bieżącym katalogu)",
    )
    parser.add_argument(
        "--check-config", action="store_true",
        help="sprawdź konfigurację bez zapisu i dostępu do nośników",
    )
    args = parser.parse_args(argv)
    if args.check_config or args.config is not None:
        try:
            load_config(args.config if args.config is not None else ".env")
        except ConfigError as error:
            print(f"Błąd konfiguracji: {error}", file=sys.stderr)
            return 2
        print("Konfiguracja poprawna. Nie sprawdzano dostępności ani tożsamości nośników.")
        return 0
    parser.print_help()
    return 0
