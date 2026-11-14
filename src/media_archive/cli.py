"""Command-line interface for configuration, setup and state inspection."""

import argparse
from collections.abc import Sequence
from pathlib import Path
import sys

from media_archive import operations
from media_archive.config import ConfigError, load_config
from media_archive.reports import Report, write_report
from media_archive.state import DatabasePublishedError, StateError
from media_archive.state.snapshots import SnapshotPublishedError, create_snapshot, restore_snapshot, validate_snapshot


def _operation_id(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        number = 0
    if not 0 < number <= 2**63 - 1:
        raise argparse.ArgumentTypeError("ID operacji musi być dodatnią liczbą całkowitą.")
    return number


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="media-archive",
        description="Lokalne narzędzie do importu, porządkowania i archiwizacji mediów.",
        epilog="IMPORT, SORT, BACKUP i AUDIT nie są jeszcze zaimplementowane.",
    )
    parser.add_argument("--config", metavar="PLIK", help="plik konfiguracji (domyślnie .env)")
    parser.add_argument("--check-config", action="store_true", help="sprawdź sam plik konfiguracji")
    commands = parser.add_subparsers(dest="command", metavar="POLECENIE")
    setup = commands.add_parser("setup", help="utwórz bazę i zapisz początkowy rejestr")
    setup.add_argument("--yes", action="store_true", help="potwierdź utworzenie bazy i zapis rejestru")
    migrate = commands.add_parser("migrate", help="zaktualizuj schemat istniejącej bazy")
    commands.add_parser("pending", help="wyświetl niedokończone operacje")
    resume = commands.add_parser("resume", help="pokaż plan wznowienia operacji")
    resume.add_argument("operation_id", type=_operation_id, metavar="ID_OPERACJI")
    snapshot = commands.add_parser("snapshot", help="zapisz generację stanu")
    snapshot.add_argument("directory", type=Path, metavar="KATALOG")
    verify = commands.add_parser("verify-snapshot", help="sprawdź zapisaną generację")
    verify.add_argument("generation", type=Path, metavar="GENERACJA")
    restore = commands.add_parser("restore", help="przywróć bazę z generacji")
    restore.add_argument("generation", type=Path, metavar="GENERACJA")
    restore.add_argument("--yes", action="store_true", help="potwierdź przywrócenie bazy")
    for command in (setup, migrate, resume):
        command.add_argument("--dry-run", action="store_true", help="pokaż plan bez zapisu stanu")
        command.add_argument("--report", action="store_true", help="zapisz raport tekstowy i JSON")
    return parser


def _run(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    if args.command is None and not (args.check_config or args.config is not None):
        parser.print_help()
        return 0
    if args.check_config and args.command is not None:
        parser.error("--check-config wymaga wywołania bez polecenia.")
    if args.command == "setup" and not (args.dry_run or args.yes):
        parser.error("Użyj setup --dry-run, aby zobaczyć plan, albo setup --yes, aby go wykonać.")
    if args.command == "restore" and (args.config is None or not args.yes):
        parser.error("Użyj --config PLIK restore GENERACJA --yes.")
    if args.command == "verify-snapshot":
        try:
            generation = validate_snapshot(args.generation.absolute())
        except (ConfigError, StateError) as error:
            print(f"Błąd generacji: {error}", file=sys.stderr)
            return 1
        print(f"Generacja {generation.generation_id} poprawna · rewizja {generation.revision}")
        return 0
    try:
        config = load_config(args.config if args.config is not None else ".env")
    except ConfigError as error:
        print(f"Błąd konfiguracji: {error}", file=sys.stderr)
        return 2
    if args.command is None:
        print("Konfiguracja poprawna. Nie sprawdzano dostępności ani tożsamości nośników.")
        return 0

    dry_run = getattr(args, "dry_run", False)
    save_report = getattr(args, "report", False) or (args.command in ("setup", "migrate") and not dry_run)
    try:
        operations.check_application_paths(config, reports=save_report)
    except StateError as error:
        print(f"Błąd wykonania: {error}", file=sys.stderr)
        return 1
    try:
        if args.command == "setup":
            details = operations.setup(config, dry_run=dry_run)
            message = "Plan konfiguracji początkowej." if dry_run else "Baza i początkowy rejestr są gotowe."
        elif args.command == "migrate":
            details = operations.migrate(config, dry_run=dry_run)
            message = "Plan migracji bazy." if dry_run else "Baza ma aktualny schemat."
        elif args.command == "snapshot":
            generation = create_snapshot(config, args.directory.absolute())
            details = {"Katalog generacji": str(generation)}
            message = "Zapisano zweryfikowaną generację stanu."
        elif args.command == "restore":
            result = restore_snapshot(config, args.generation.absolute())
            details = {"Generacja": result.generation_id}
            if result.previous_database is not None:
                details["Poprzednia baza"] = str(result.previous_database)
            message = "Przywrócono bazę. Stan wymaga uzgodnienia z nośnikami."
        else:
            if args.command == "resume" and not dry_run:
                raise StateError("Wznowienie transferu będzie dostępne po wdrożeniu operacji na plikach. "
                                 "Użyj resume ID --dry-run.")
            details = operations.pending(config, getattr(args, "operation_id", None))
            message = "Plan wznowienia." if dry_run else "Podgląd niedokończonych operacji."
            if not details["Niedokończone operacje"]:
                message = "Brak niedokończonych operacji."
        report = Report(args.command, 0, message, dry_run, details)
    except ConfigError as error:
        report = Report(args.command, 2, f"Błąd konfiguracji: {error}", dry_run)
    except KeyboardInterrupt:
        report = Report(args.command, 130, "Przerwano polecenie.", dry_run)
    except (DatabasePublishedError, SnapshotPublishedError) as error:
        report = Report(args.command, 3, f"Wynik częściowy. {error}", dry_run)
    except StateError as error:
        report = Report(args.command, 1, f"Błąd wykonania: {error}", dry_run)

    print(report.render(), end="", file=sys.stderr if report.code else sys.stdout)
    if save_report:
        try:
            destination = write_report(config.app.reports_dir, report, config.app.report_keep_runs)
        except StateError as error:
            print(str(error), file=sys.stderr)
            if report.code == 0:
                return 1 if dry_run else 3
        else:
            print(f"Raport · {destination.name}")
    return report.code


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    try:
        return _run(parser.parse_args(argv), parser)
    except KeyboardInterrupt:
        print("Przerwano polecenie.", file=sys.stderr)
        return 130
