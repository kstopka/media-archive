"""Command-line interface for configuration, setup and state inspection."""

import argparse
from collections.abc import Sequence
from datetime import date
from pathlib import Path
import sys

from media_archive import operations
from media_archive.audit import quick_audit
from media_archive.camera_import import import_camera
from media_archive.config import ConfigError, load_config
from media_archive.full_audit import full_audit
from media_archive.import_planning import ImportPlan, plan_import
from media_archive.import_recovery import resume_import
from media_archive.import_restore import reconcile_imports
from media_archive.inventory import inventory
from media_archive.phone_initialization import initialize_phone
from media_archive.phone_import import import_phone
from media_archive.reports import Report, write_report
from media_archive.sort_execution import SortResult, execute_sort, is_sort_operation, resume_sort
from media_archive.sort_planning import SortItem, plan_sort, set_sort_date
from media_archive.state import DatabasePublishedError, DeviceKey, StateError
from media_archive.state.snapshots import SnapshotPublishedError, create_snapshot, restore_snapshot, validate_snapshot


def _operation_id(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        number = 0
    if not 0 < number <= 2**63 - 1:
        raise argparse.ArgumentTypeError("ID operacji musi być dodatnią liczbą całkowitą.")
    return number


def _device_key(value: str) -> DeviceKey:
    match value.split(":"):
        case ["card", alias] if alias:
            return DeviceKey("card", alias)
        case ["local", alias] if alias:
            return DeviceKey("local", alias)
        case ["archive", group, alias] if group and alias:
            return DeviceKey("archive", alias, group)
    raise argparse.ArgumentTypeError("Użyj card:ALIAS, local:ALIAS albo archive:GRUPA:ALIAS.")


def _sort_date(value: str) -> date:
    try:
        result = date.fromisoformat(value)
        if result.isoformat() == value:
            return result
    except ValueError:
        pass
    raise argparse.ArgumentTypeError("Podaj poprawną datę w postaci RRRR-MM-DD.")


_IMPORT_DECISIONS = {
    "copy": "do skopiowania", "reimport": "do ponownego importu", "already_imported": "już zaimportowano",
    "existing_baseline": "kopia rozpoznana w spisie bazowym", "baseline_skip": "pominięcie bazowe",
    "duplicate": "duplikat w tej partii", "awaiting_initialization": "oczekuje na inicjalizację",
    "ignored": "pominięty format", "unsupported": "nieobsługiwany wpis", "unreadable": "błąd odczytu",
    "destination_conflict": "cel już istnieje",
}


def _import_details(plan: ImportPlan) -> dict[str, object]:
    details: dict[str, object] = {
        "Profil źródła": plan.profile, "Pliki do importu": len(plan.transfers),
        "Wymagane bajty na stagingu": plan.required_bytes, "Rezerwa stagingu w bajtach": plan.reserve_bytes,
        "Pominięte formaty": sum(item.decision == "ignored" for item in plan.items),
        "Nieobsługiwane wpisy": sum(item.decision == "unsupported" for item in plan.items),
    }
    if plan.available_bytes is not None:
        details["Wolne bajty na stagingu"] = plan.available_bytes
    if plan.problems:
        details["Przyczyny blokady"] = plan.problems
    if plan.missing_staging:
        details["Brakujące materiały w stagingu"] = [
            {"id_importu": item.import_id, "staging": item.path, "sha256": item.sha256,
             "reimport zaplanowany": item.reimport_planned} for item in plan.missing_staging
        ]
    details["Manifest importu"] = [
        {"ścieżka": item.relative_path.as_posix(), "bajty": item.size, "sha256": item.sha256,
         "decyzja": _IMPORT_DECISIONS[item.decision],
         "staging": item.destination.as_posix() if item.destination is not None else None}
        for item in plan.items
    ]
    return details


def _sort_details(plan: tuple[SortItem, ...], outcomes: tuple[str, ...] | None = None) -> dict[str, object]:
    rows = []
    states = {"ready": "gotowy", "undated": "do wyjaśnienia", "blocked": "zablokowany",
              "placed": "w zbiorze", "interrupted": "przerwano"}
    for index, item in enumerate(plan):
        metadata = item.metadata
        row = {
            "id_importu": item.import_id, "plik": item.name, "profil": item.profile,
            "operacja": item.operation_id, "grupa konfliktu": item.conflict_group,
            "staging": str(item.staging), "cel": str(item.target) if item.target is not None else None,
            "stan": states[outcomes[index] if outcomes is not None else item.state],
            "ręczna_data": item.manual_date.isoformat() if item.manual_date else None, "błąd": item.problem,
        }
        if metadata is not None:
            row["metadane"] = {
                "pole": metadata.tag, "surowa_data": metadata.raw_date,
                "odczytana_data": metadata.timestamp.isoformat() if metadata.timestamp else None,
                "ExifTool": metadata.tool_version, "diagnostyka": metadata.diagnostics,
            }
        rows.append(row)
    return {"Pliki gotowe do SORT": sum(item.state == "ready" for item in plan),
            "Pliki do wyjaśnienia": sum(item.state == "undated" for item in plan),
            "Zablokowane pliki": sum(item.state == "blocked" for item in plan), "Plan SORT": rows}


def _sort_result(results: tuple[SortResult, ...]) -> tuple[int, str, dict[str, object]]:
    statuses = tuple(result.status for result in results)
    code = 130 if "interrupted" in statuses else 3 if {"blocked", "undated"}.intersection(statuses) else 0
    message = "Przerwano SORT." if code == 130 else "SORT wymaga uwagi." if code else "SORT ukończony."
    details = _sort_details(tuple(result.item for result in results), outcomes=statuses)
    details["Przeniesiono do zbioru"] = statuses.count("placed")
    return code, message, details


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="media-archive",
        description="Lokalne narzędzie do importu, porządkowania i archiwizacji mediów.",
        epilog="Dostępny jest AUDIT, pierwszy spis, import i SORT. Operacje BACKUP "
               "nie są jeszcze zaimplementowane.",
    )
    parser.add_argument("--config", metavar="PLIK", help="plik konfiguracji (domyślnie .env)")
    parser.add_argument("--check-config", action="store_true", help="sprawdź sam plik konfiguracji")
    commands = parser.add_subparsers(dest="command", metavar="POLECENIE")
    setup = commands.add_parser("setup", help="utwórz bazę i zapisz początkowy rejestr")
    setup.add_argument("--yes", action="store_true", help="potwierdź utworzenie bazy i zapis rejestru")
    migrate = commands.add_parser("migrate", help="zaktualizuj schemat istniejącej bazy")
    commands.add_parser("pending", help="wyświetl niedokończone operacje")
    resume = commands.add_parser("resume", help="wznów IMPORT lub SORT albo pokaż plan odzyskiwania")
    resume.add_argument("operation_id", type=_operation_id, metavar="ID_OPERACJI")
    resume.add_argument("--card", metavar="KARTA")
    resume.add_argument("--mount", type=Path, metavar="KATALOG")
    reconcile = commands.add_parser("reconcile", help="uzgodnij stan importu po przywróceniu bazy")
    reconcile.add_argument("--audit", action="append", default=[], metavar="ID_AUDYTU")
    snapshot = commands.add_parser("snapshot", help="zapisz generację stanu")
    snapshot.add_argument("directory", type=Path, metavar="KATALOG")
    verify = commands.add_parser("verify-snapshot", help="sprawdź zapisaną generację")
    verify.add_argument("generation", type=Path, metavar="GENERACJA")
    restore = commands.add_parser("restore", help="przywróć bazę z generacji")
    restore.add_argument("generation", type=Path, metavar="GENERACJA")
    restore.add_argument("--yes", action="store_true", help="potwierdź przywrócenie bazy")
    audit = commands.add_parser("audit", help="porównaj metadane lub SHA-256 dwóch nośników")
    audit.add_argument("--group", metavar="GRUPA")
    audit.add_argument("--targets", nargs=2, metavar=("PIERWSZY", "DRUGI"))
    audit.add_argument("--mode", choices=("quick", "full"))
    audit.add_argument("--resume", metavar="ID_AUDYTU", help="wznów pełny audyt z zapisanej bazy")
    inventory_parser = commands.add_parser("inventory", help="wykonaj lub wznów pierwszy spis zbiorów i archiwów")
    inventory_parser.add_argument("--hash-local", action="store_true", help="odczytaj także SHA-256 lokalnych plików")
    phone_init = commands.add_parser("init-phone", help="zapisz lub porównaj pierwszy spis telefonu")
    phone_init.add_argument("profile", metavar="PROFIL")
    phone_init.add_argument("--audit", metavar="ID_AUDYTU", help="użyj zapisanego pełnego audytu właściwej pary")
    import_parser = commands.add_parser("import", help="importuj z karty lub telefonu albo pokaż plan")
    import_parser.add_argument("--source", required=True, metavar="PROFIL")
    import_parser.add_argument("--card", metavar="KARTA")
    import_parser.add_argument("--mount", type=Path, metavar="KATALOG")
    import_parser.add_argument("--reimport", action="append", default=[], metavar="ŚCIEŻKA",
                               help="zaplanuj ponowny import wskazanego pliku względnie do katalogu źródła")
    sort = commands.add_parser("sort", help="uporządkuj zweryfikowane importy")
    sort_date = commands.add_parser("sort-date", help="zapisz ręczną datę importu oczekującego na SORT")
    sort_date.add_argument("import_id", type=_operation_id, metavar="ID_IMPORTU")
    sort_date.add_argument("date", type=_sort_date, metavar="RRRR-MM-DD")
    device = commands.add_parser("device", help="sprawdź lub jawnie zarejestruj wolumin")
    actions = device.add_subparsers(dest="device_action", required=True)
    for action, help_text in (
        ("check", "sprawdź zamontowany wolumin"), ("register", "zatwierdź dowody tożsamości woluminu"),
        ("replace", "zarejestruj inny nośnik"), ("reformat", "zarejestruj nowy UUID tego samego nośnika"),
    ):
        command = actions.add_parser(action, help=help_text)
        command.add_argument("device_key", type=_device_key, metavar="POWIĄZANIE")
        command.add_argument("--to", type=Path, metavar="KATALOG", required=action in ("replace", "reformat"))
        if action != "check":
            command.add_argument("--yes", action="store_true", help="zapisz sprawdzoną zmianę w rejestrze")
    for command in (setup, migrate, resume, audit, import_parser, sort):
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
    if args.command in ("import", "resume") and (args.card is None) != (args.mount is None):
        parser.error("Import z karty wymaga jednocześnie --card i --mount.")
    if args.command == "audit":
        if args.resume is not None:
            if args.group is not None or args.targets is not None or args.mode == "quick":
                parser.error("Użyj audit --resume ID_AUDYTU, aby wznowić zapisany pełny audyt.")
        elif args.group is None or args.targets is None:
            parser.error("Nowy audyt wymaga --group GRUPA --targets PIERWSZY DRUGI.")
        if args.dry_run and (args.resume is not None or args.mode == "full"):
            parser.error("Pełny audyt zapisuje postęp w bazie i nie obsługuje --dry-run.")
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
    if args.command == "device":
        dry_run = args.device_action != "check" and not getattr(args, "yes", False)
    save_report = getattr(args, "report", False) or (
        args.command in ("setup", "migrate", "audit", "inventory", "init-phone", "import", "resume", "reconcile", "sort")
        and not dry_run
    )
    try:
        operations.check_application_paths(config, reports=save_report,
                                           device_mount=getattr(args, "mount", getattr(args, "to", None)))
    except StateError as error:
        print(f"Błąd wykonania: {error}", file=sys.stderr)
        return 1
    try:
        code = 0
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
        elif args.command == "device":
            details = operations.device(config, args.device_key, args.to, action=args.device_action, dry_run=dry_run)
            if args.device_action == "check":
                message = "Zamontowany wolumin jest zgodny z rejestrem."
            elif dry_run:
                message = "Plan zmiany rejestru. Powtórz z --yes, aby zatwierdzić."
            else:
                message = "Zapisano zatwierdzoną zmianę rejestru."
        elif args.command == "audit":
            if args.mode == "full" or args.resume is not None:
                outcome = full_audit(config, args.group, args.targets, resume=args.resume,
                                     announce=lambda message: print(message, file=sys.stderr, flush=True))
            else:
                outcome = quick_audit(config, args.group, *args.targets, dry_run=dry_run)
            code, message, details = outcome.code, outcome.message, outcome.details
        elif args.command == "inventory":
            outcome = inventory(config, hash_local=args.hash_local,
                                announce=lambda message: print(message, file=sys.stderr, flush=True))
            code, message, details = outcome.code, outcome.message, outcome.details
        elif args.command == "init-phone":
            outcome = initialize_phone(config, args.profile, audit_id=args.audit,
                                       announce=lambda message: print(message, file=sys.stderr, flush=True))
            code, message, details = outcome.code, outcome.message, outcome.details
        elif args.command == "import":
            def announce(message: str) -> None:
                print(message, file=sys.stderr, flush=True)

            if dry_run:
                plan = plan_import(config, args.source, card=args.card, mount=args.mount, reimport=args.reimport,
                                   announce=announce)
                code = 0 if plan.ready else 3
                message = "Plan importu gotowy." if plan.ready else "Plan importu jest zablokowany."
                details = _import_details(plan)
            else:
                if args.card is not None:
                    outcome = import_camera(config, args.source, card=args.card, mount=args.mount,
                                            reimport=args.reimport, announce=announce)
                else:
                    outcome = import_phone(config, args.source, reimport=args.reimport, announce=announce)
                code, message = outcome.code, outcome.message
                details = _import_details(outcome.plan)
                details.update({
                    "Zaimportowano plików": sum(item.status == "imported" for item in outcome.results),
                    "Zaimportowano bajtów": sum(item.size for item in outcome.results if item.status == "imported"),
                    "Wyniki importu": [
                        {"ścieżka": item.path.as_posix(), "stan": {"imported": "zaimportowano", "failed": "błąd",
                                                                 "interrupted": "przerwano"}[item.status],
                         "operacja": item.operation_id, **({"błąd": item.error} if item.error else {})}
                        for item in outcome.results
                    ],
                })
        elif args.command == "resume" and not dry_run:
            if is_sort_operation(config, args.operation_id):
                if args.card is not None:
                    raise ConfigError("Wznowienie SORT korzysta z lokalnej kopii roboczej.")
                code, message, details = _sort_result((resume_sort(config, args.operation_id),))
            else:
                code, message, details = resume_import(config, args.operation_id, card=args.card, mount=args.mount)
        elif args.command == "sort":
            if dry_run:
                plan = plan_sort(config)
                details = _sort_details(plan)
                code = 3 if any(item.state != "ready" for item in plan) else 0
                message = "Podgląd SORT wymaga uwagi." if code else "Podgląd SORT gotowy."
            else:
                code, message, details = _sort_result(execute_sort(config))
        elif args.command == "sort-date":
            set_sort_date(config, args.import_id, args.date)
            details = {"ID importu": args.import_id, "Ręczna data": args.date.isoformat()}
            message = "Zapisano ręczną datę. Użyj sort --dry-run, aby odświeżyć podgląd."
        elif args.command == "reconcile":
            try:
                details = reconcile_imports(config, args.audit)
            except StateError as error:
                code, message, details = 3, f"Uzgodnienie importu nie zostało zakończone. {error}", {}
            else:
                message = "Stan importu uzgodniony. Można wznowić import."
        else:
            details = operations.pending(config, getattr(args, "operation_id", None))
            message = "Plan wznowienia." if dry_run else "Podgląd niedokończonych operacji."
            if not details["Niedokończone operacje"]:
                message = "Brak niedokończonych operacji."
        report = Report(args.command, code, message, dry_run, details)
    except ConfigError as error:
        report = Report(args.command, 2, f"Błąd konfiguracji: {error}", dry_run)
    except KeyboardInterrupt:
        report = Report(args.command, 130, "Przerwano polecenie.", dry_run)
    except (DatabasePublishedError, SnapshotPublishedError) as error:
        report = Report(args.command, 3, f"Wynik częściowy. {error}", dry_run)
    except StateError as error:
        report = Report(args.command, 1, f"Błąd wykonania: {error}", dry_run)

    print(report.render(summary_only=args.command in ("audit", "inventory", "init-phone", "import") and save_report), end="",
          file=sys.stderr if report.code else sys.stdout)
    if save_report:
        try:
            destination = write_report(config.app.reports_dir, report, config.app.report_keep_runs)
        except StateError as error:
            print(str(error), file=sys.stderr)
            if report.code in (0, 4):
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
