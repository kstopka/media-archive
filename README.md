# Media Archive CLI

Projekt lokalnego narzędzia CLI na Linuxa do importu, porządkowania i archiwizacji zdjęć, filmów oraz dokumentów.

## Status

Projekt jest na etapie planowania. Specyfikacja jest przeglądana przed rozpoczęciem implementacji. Poniższe funkcje opisują docelowe działanie.

## Planowane operacje

- **IMPORT** — przyrostowe pobieranie materiałów z urządzeń mobilnych i kart pamięci, z rozpoznawaniem wcześniej zaimportowanej treści.
- **SORT** — organizowanie mediów według dat z metadanych oraz bezpieczna obsługa konfliktów nazw.
- **BACKUP** — ręcznie uruchamiane tworzenie kopii na zewnętrznych nośnikach, z niezależnym stanem każdej kopii.
- **AUDIT** — porównywanie archiwów, wykrywanie braków i różnic oraz raportowanie wyników.

Projekt przewiduje także wyszukiwanie zarejestrowanych plików, przegląd konfliktów i odtwarzanie bazy stanu z kopii zapasowej.

## Założenia

- Weryfikacja integralności przez SHA-256.
- Zachowanie oryginalnej zawartości mediów.
- Obsługa przerwanych operacji i ograniczonego miejsca.
- Konfigurowalne profile urządzeń, zbiorów i nośników archiwalnych.
- Jawne polityki aktualizacji i usuwania, z potwierdzaniem planowanych usunięć.
- Rozróżnienie szybkiego porównania atrybutów i pełnej weryfikacji treści.

## Planowane technologie

Python 3, SQLite, ExifTool, rsync oraz ADB dla obsługiwanych urządzeń z Androidem.

## Zawartość repozytorium

Na obecnym etapie publikowane są wyłącznie ten opis i reguły ignorowania plików. Dokumentacja robocza, lokalna konfiguracja, dane, próbki i raporty pozostają poza publicznym repozytorium.
