"""Publish private UTF-8 report pairs and rotate completed command reports."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from fcntl import LOCK_EX, flock
import json
import os
from pathlib import Path
import re
import shutil
from tempfile import TemporaryDirectory
from uuid import uuid4

from media_archive._progress import format_count
from media_archive.state import StateError


_RUN_NAME = re.compile(r"run-([0-9]{8}T[0-9]{12}Z)-([0-9a-f]{32})")
_REPORT_FORMAT = "media-archive/report-v1"
_STAMP_FORMAT = "%Y%m%dT%H%M%S%fZ"
_STATUS_LABELS = {0: "Sukces", 1: "Błąd wykonania", 2: "Błąd konfiguracji",
                  3: "Wynik częściowy", 4: "Audyt wykrył różnice", 130: "Przerwano"}


@dataclass(frozen=True)
class Report:
    command: str
    code: int
    message: str
    dry_run: bool = False
    details: dict[str, object] = field(default_factory=dict)
    invocation_id: str = field(default_factory=lambda: uuid4().hex)
    finished_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def render(self, *, summary_only: bool = False, show_all_import: bool = True) -> str:
        lines = [self.message]
        for key, value in self.details.items():
            if key == "Niedokończone operacje":
                for item in value:
                    lines.append(f"Operacja {item['id_operacji']} · {item['stan']} · uruchomienie {item['id_uruchomienia'] or '—'}")
                    if "następny_krok" in item:
                        lines.append(item["następny_krok"])
            elif key == "Zakresy spisu":
                for item in value:
                    summary = f"{item['zakres']} · {item['stan']}"
                    if "pliki" in item:
                        summary += f" · pliki {format_count(item['pliki'])}"
                    for note in ("uwaga", "błąd"):
                        if note in item:
                            summary += f" · {item[note]}"
                    if "błędy odczytu" in item:
                        summary += f" · błędy odczytu {len(item['błędy odczytu'])}"
                    lines.append(summary)
                    if not summary_only:
                        if "ID obserwacji" in item:
                            lines.append(f"ID obserwacji · {item['ID obserwacji']}")
                        lines.extend(f"- {error['ścieżka']} · {error['błąd']}"
                                     for error in item.get("błędy odczytu", ()))
            elif key in ("Decyzje spisu telefonu", "Manifest importu"):
                if key == "Manifest importu" and not show_all_import:
                    value = [item for item in value if item["staging"] is not None]
                if not summary_only and value:
                    lines.append(key)
                    lines.extend(f"- {item['ścieżka']} · {item['decyzja']}" for item in value)
            elif key == "Lokalizacje":
                for item in value:
                    lines.append(f"- Lokalizacja {item['id']} · {item['ścieżka']} · {item['nośnik'] or item['rodzaj']}"
                                 f" · {item['dostępność']} · {item['obecność']}")
                    if item["następna_lokalizacja"] is not None:
                        lines.append(f"  Następna lokalizacja · {item['następna_lokalizacja']}")
            elif key == "Konflikty SORT":
                for group in value:
                    status = "zachowaj oba" if group["zachowaj_oba_od"] else "do przejrzenia"
                    lines.append(f"- Grupa {group['grupa']} · {group['nazwa']} · {status}")
                    for item in group["warianty"]:
                        lines.append(f"  {item['ścieżka']} · wolumin {item['id_woluminu']} · SHA-256 {item['sha256']}")
                        if item["przeniesiono_do"] is not None:
                            lines.append(f"  Przeniesiono do {item['przeniesiono_do']}")
                        if item["id_lokalizacji"] is not None:
                            lines.append(f"  ID lokalizacji · {item['id_lokalizacji']}")
            elif key == "Cele BACKUP":
                lines.extend(f"- {item['grupa']}/{item['cel']} · {item['stan']}"
                             + (f" · {item['uwaga']}" if "uwaga" in item else "") for item in value)
            elif key == "Zakresy BACKUP":
                lines.append(key)
                lines.extend(f"- {item['zbiór']} · {item['label']}"
                             + (" · dostępny wybór miesiąca" if item.get("miesiące") else "") for item in value)
            elif key == "Pojemność BACKUP":
                for item in value:
                    lines.append(f"- {item['cel']} · wolne {item['dostępne']} B · wymagane {item['wymagane']} B"
                                 f" · rezerwa {item['rezerwa']} B · brakuje {item['brakuje']} B")
                    lines.append(f"  Pliki {item['pliki']} B · katalogi i części {item['katalogi_i_części']} B"
                                 f" · snapshot {item['snapshot']} B")
                    lines.append(f"  Zgodne {item['zgodne']} · nowe {item['nowe']} · podmiany {item['podmiany']}"
                                 f" · warianty konfliktowe {item['warianty_konfliktowe']}")
            elif key == "Historyczne kopie pod inną ścieżką":
                lines.append(key)
                for item in value:
                    lines.append(f"- {item['źródło']} · cel {item['cel']}")
                    lines.extend(f"  {path}" for path in item["zapisane ścieżki"])
            elif key == "Podsumowanie szybkiego BACKUP":
                lines.append(key)
                for item in value:
                    lines.append(f"- {item['cel']} · do skopiowania {item['do skopiowania']}"
                                 f" · zgodne z historią {item['zgodne z historią']}"
                                 f" · do weryfikacji {item['do weryfikacji']}")
            elif key == "Kopie do weryfikacji":
                lines.append(key)
                lines.extend(f"- {item['cel']} · {item['ścieżka']}" for item in value)
            elif key == "Wyniki BACKUP":
                for item in value:
                    status = item.get("wynik", item.get("błąd"))
                    if status != "zgodna z historią":
                        lines.append(f"- {item['cel']} · {item['ścieżka']} · {status}")
            elif key == "Błędy planowania":
                lines.extend(f"- {item['ścieżka']} · {item['błąd']}"
                             + (f" · cel {item['cel']}" if "cel" in item else "") for item in value)
            elif key == "Plan SORT":
                for item in value:
                    line = f"- Import {item['id_importu']} · {item['plik']} · {item['stan']}"
                    if item["cel"]:
                        line += f" → {item['cel']}"
                    if item["operacja"] is not None:
                        line += f" · operacja {item['operacja']}"
                    if item["grupa konfliktu"] is not None:
                        line += " · kolizja nazwy"
                    if item["błąd"]:
                        line += f" · {item['błąd']}"
                    if item.get("metadane", {}).get("diagnostyka"):
                        line += " · " + " · ".join(item["metadane"]["diagnostyka"])
                    lines.append(line)
            elif key == "Wyniki importu":
                for item in value:
                    if summary_only and item["stan"] == "zaimportowano":
                        continue
                    line = f"- {item['ścieżka']} · {item['stan']}"
                    if item["operacja"] is not None:
                        line += f" · operacja {item['operacja']}"
                    if "błąd" in item:
                        line += f" · {item['błąd']}"
                    lines.append(line)
            elif key == "Nieobsługiwane pliki telefonu":
                if not summary_only:
                    lines.append(key)
                    lines.extend(f"- {path}" for path in value)
            elif self.command == "audit" and key in ("Różnice", "Błędy skanowania", "Odczytane katalogi", "Odczyty SHA-256"):
                if not summary_only and value:
                    lines.append(key)
                    lines.extend(f"- {item}" for item in value)
            else:
                lines.append(f"{key} · {value}")
        return "\n".join(lines) + "\n"

    def to_json(self) -> str:
        return json.dumps({
            "format": _REPORT_FORMAT,
            "id_wywołania": self.invocation_id, "polecenie": self.command,
            "zakończono": self.finished_at.isoformat(), "kod": self.code,
            "wynik": _STATUS_LABELS[self.code], "tryb_planowania": self.dry_run,
            "komunikat": self.message, "szczegóły": self.details,
        }, ensure_ascii=False, indent=2) + "\n"


def write_report(directory: Path, report: Report, keep: int) -> Path:
    """Publish text and JSON together and rotate only managed completed pairs."""
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        stamp = report.finished_at.strftime(_STAMP_FORMAT)
        prefix = "plan" if report.dry_run else "interrupted" if report.code == 130 else "run"
        destination = directory / f"{prefix}-{stamp}-{report.invocation_id}"
        # The directory descriptor serializes cooperating publishers without a separate lock file.
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            flock(descriptor, LOCK_EX)
            if destination.exists() or destination.is_symlink():
                raise StateError("Raport o tym identyfikatorze już istnieje.")
            with TemporaryDirectory(prefix=".report-", dir=directory) as temporary:
                bundle = Path(temporary) / "report"
                bundle.mkdir(mode=0o700)
                (bundle / "report.txt").write_text(report.render(), encoding="utf-8")
                (bundle / "report.json").write_text(report.to_json(), encoding="utf-8")
                bundle.rename(destination)
            if prefix == "run":
                _rotate(directory, keep)
        finally:
            os.close(descriptor)
    except OSError:
        raise StateError("Nie można zapisać lub uporządkować raportów.") from None
    return destination


def _is_completed_report(bundle: Path) -> bool:
    name = _RUN_NAME.fullmatch(bundle.name)
    if name is None or bundle.is_symlink() or not bundle.is_dir():
        return False
    try:
        files = tuple(bundle.iterdir())
        if {file.name for file in files} != {"report.txt", "report.json"}:
            return False
        if any(file.is_symlink() or not file.is_file() for file in files):
            return False
        payload = json.loads((bundle / "report.json").read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("format") != _REPORT_FORMAT:
            return False
        if payload.get("id_wywołania") != name[2] or payload.get("tryb_planowania") is not False:
            return False
        if type(payload.get("kod")) is not int or payload["kod"] not in (0, 1, 2, 3, 4):
            return False
        finished_at = datetime.fromisoformat(payload["zakończono"])
        return finished_at.utcoffset() is not None and finished_at.astimezone(timezone.utc).strftime(_STAMP_FORMAT) == name[1]
    except (OSError, ValueError, KeyError, TypeError, OverflowError, RecursionError):
        return False


def _rotate(directory: Path, keep: int) -> None:
    completed = sorted((item for item in directory.iterdir() if _is_completed_report(item)), reverse=True)
    for item in completed[keep:]:
        shutil.rmtree(item)
