# Modbus Dash

Webowy monitoring liczników energii elektrycznej po **Modbus RTU (RS-485)** i **Modbus TCP** - dla Raspberry Pi
z nakładką RS-485, adaptera USB-RS485 albo bramki Ethernet. Bez chmury, bez zewnętrznych zależności w przeglądarce.

- **Dashboard na żywo** - karty i wykresy, historia (pamięć + SQLite), eksport CSV, wiele liczników jednocześnie.
- **Biblioteka gotowych presetów** - Eastron, Orno, Carlo Gavazzi, Schneider, Finder, Chint, ABB, Janitza, Siemens,
  Socomec, Peacefair PZEM i inne (patrz [Obsługiwane liczniki](#obsługiwane-liczniki)).
- **Pełna obsługa typów danych** - int8/uint8/int16/uint16/int32/uint32/int64/uint64/float32/float64, wszystkie
  kolejności bajtów (ABCD, CDAB, BADC, DCBA), skalowanie i przesunięcie, skala z innego rejestru (SunSpec),
  Input i Holding Registers w jednym presecie.
- **Skaner rejestrów** z automatyczną identyfikacją wartości, trybem Live, wyszukiwaniem urządzeń (Unit ID) i
  **automatycznym rozpoznawaniem modelu licznika**.
- **Transporty**: RS-485 RTU, RS-485 ASCII, Modbus TCP, RTU-over-TCP (tanie bramki w trybie transparentnym), UDP.
- **Integracje**: MQTT z autodiscovery dla **Home Assistant**, metryki **Prometheus** (`/metrics`), REST API.
- **Wbudowany symulator** - testy bez sprzętu, potrafi udawać dowolny licznik z biblioteki.

## Spis treści

1. [Szybki start](#szybki-start)
2. [Instalacja](#instalacja)
3. [Podłączenie do licznika](#podłączenie-do-licznika)
4. [Argumenty CLI](#argumenty-cli)
5. [Interfejs webowy](#interfejs-webowy)
6. [Magistrale i urządzenia](#magistrale-i-urządzenia)
7. [Presety - format](#presety---format)
8. [Obsługiwane liczniki](#obsługiwane-liczniki)
9. [Skaner rejestrów](#skaner-rejestrów)
10. [Historia i eksport](#historia-i-eksport)
11. [Integracje: MQTT / Home Assistant / Prometheus](#integracje-mqtt--home-assistant--prometheus)
12. [Symulator](#symulator)
13. [Raspberry Pi i RS-485](#raspberry-pi-i-rs-485)
14. [Bezpieczeństwo](#bezpieczeństwo)
15. [Rozwiązywanie problemów](#rozwiązywanie-problemów)
16. [API REST](#api-rest)
17. [Architektura](#architektura)

---

## Szybki start

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

Na Raspberry Pi najprościej: `bash deploy/install-rpi.sh` (patrz [Instalacja](#instalacja)).

Otwórz `http://localhost:5000`. Startuje wbudowany symulator licznika 3-fazowego (Modbus TCP, port 5020)
i od razu widać go na dashboardzie jako urządzenie **Symulator 3F**.

Z prawdziwym licznikiem, np. Eastron SDM630 na nakładce RS-485:

```bash
python app.py --serial /dev/serial0 --baudrate 9600 --preset eastron_sdm630 --unit 1
```

Nie znasz parametrów licznika? Uruchom z samym `--serial ...`, wejdź w **Skaner** i użyj
**Szukaj urządzeń** (Unit ID) oraz **Rozpoznaj licznik** w zakładce **Urządzenia**.

## Instalacja

### Wymagania

| Pakiet | Wersja | Uwagi |
|--------|--------|-------|
| Python | 3.10+ | |
| Flask | 2.2+ | |
| pymodbus | 3.6+ (sprawdzone 3.6 - 3.15) | |
| pyserial | 3.4+ | tylko RS-485 / port szeregowy |
| paho-mqtt | 1.6+ / 2.x | opcjonalnie - MQTT / Home Assistant |
| waitress | 2.1+ | opcjonalnie - wydajniejszy serwer HTTP (używany automatycznie, gdy jest zainstalowany) |

### Desktop (Windows / Linux / macOS)

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

### Raspberry Pi (Raspberry Pi OS Bookworm i nowsze)

```bash
git clone <adres repozytorium> modbus-rpi && cd modbus-rpi
bash deploy/install-rpi.sh
```

Skrypt tworzy środowisko `.venv`, instaluje zależności (z `waitress` i `paho-mqtt`), dodaje użytkownika do grupy
`dialout` i instaluje usługę systemd (`deploy/modbus-dash.service`). Parametry uruchomienia zmienisz w
`/etc/systemd/system/modbus-dash.service` (linia `ExecStart`), potem:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now modbus-dash
journalctl -u modbus-dash -f
```

Na Bookworm `pip install` poza venv jest zablokowany (PEP 668) - dlatego skrypt używa `.venv`.

## Podłączenie do licznika

| Sposób | Przykład uruchomienia |
|--------|------------------------|
| Nakładka RS-485 na RPi | `python app.py --serial /dev/serial0 --baudrate 9600 --parity N` |
| Adapter USB-RS485 (Linux) | `python app.py --serial /dev/ttyUSB0 --baudrate 2400` |
| Adapter USB-RS485 (Windows) | `python app.py --serial COM3 --baudrate 9600 --parity E` |
| Licznik / bramka Modbus TCP | `python app.py --tcp 192.168.1.50:502` |
| Bramka transparentna (USR-TCP232, Elfin EW11 itp.) | `python app.py --rtu-over-tcp 192.168.1.60:8899` |
| Modbus ASCII | `python app.py --serial /dev/ttyUSB0 --framer ascii` |

Flagi `--serial`, `--tcp`, `--rtu-over-tcp` ustawiają magistralę **default** (tylko na czas działania programu,
w interfejsie oznaczona jako zablokowana). Bez flag magistrale i urządzenia konfigurujesz w interfejsie
(zakładka **Połączenia** i **Urządzenia**) - zapisują się w `data/config.json`.

## Argumenty CLI

| Flaga | Opis | Domyślnie |
|-------|------|-----------|
| `--host` | adres nasłuchu HTTP (`127.0.0.1` = tylko lokalnie) | `0.0.0.0` |
| `--port` | port HTTP dashboardu | `5000` |
| `--auth USER:HASŁO` | logowanie HTTP Basic (lub zmienna `MODBUS_DASH_AUTH`) | wyłączone |
| `--allow-write` | zezwala na zapis rejestrów/cewek ze skanera | wyłączone |
| `--allowed-host NAZWA` | dodatkowa nazwa hosta dashboardu bez `--auth`, np. `energia.lan`, `*.home.lan` (można powtarzać; zmienna `MODBUS_DASH_ALLOWED_HOSTS`) | IP, `localhost`, nazwa komputera |
| `--data-dir` | katalog na `config.json` i `history.sqlite` | `<katalog aplikacji>/data` |
| `--presets-dir` | katalog presetów użytkownika | `<katalog aplikacji>/presets` |
| `--no-history` | bez historii w SQLite | |
| `--debug` | tryb debug Flask (wymusza nasłuch na 127.0.0.1) | |
| `--log-level` | `DEBUG` / `INFO` / `WARNING` / `ERROR`; logi biblioteki pymodbus włącza zmienna `MODBUS_DASH_PYMODBUS_LOG=DEBUG` | `INFO` |
| `--serial PORT` | port RS-485: `/dev/serial0`, `/dev/ttyUSB0`, `COM3` | |
| `--baudrate` | prędkość transmisji | `9600` |
| `--parity` | `N` / `E` / `O` | `N` |
| `--stopbits` | `1` / `2` | `1` |
| `--bytesize` | `7` / `8` | `8` |
| `--framer` | `rtu` / `ascii` (port szeregowy) | `rtu` |
| `--local-echo` | adapter RS-485 odsyła własną transmisję (lokalne echo) - odrzucaj ją | |
| `--tcp HOST[:PORT]` | urządzenie lub bramka Modbus TCP | |
| `--rtu-over-tcp HOST[:PORT]` | bramka w trybie transparentnym (ramki RTU po TCP) | |
| `--timeout` | czas oczekiwania na odpowiedź [s] | `1.0` |
| `--retries` | ponowienia przy braku odpowiedzi | `1` |
| `--delay-ms` | dodatkowa przerwa między ramkami (wolne liczniki) | `0` |
| `--preset ID` | od razu odczytuj licznik wg presetu (urządzenie `cli`) | |
| `--unit` | Unit ID dla `--preset` | `1` |
| `--interval` | interwał odczytu dla `--preset` [s] | `1.0` |
| `--no-sim` | nie uruchamiaj symulatora | |
| `--sim` | uruchom symulator także przy `--serial` / `--tcp` / `--rtu-over-tcp` | |
| `--modbus-port` | port TCP symulatora | `5020` |
| `--sim-preset ID[:UNIT]` | dodatkowy licznik w symulatorze (można powtarzać; Unit ID domyślnie 2, każdy licznik inny; 1 to Symulator 3F) | |
| `--sim-framing` | `tcp` albo `rtu` (RTU-over-TCP) | `tcp` |
| `--sim-strict` | symulator zwraca wyjątek 02 dla niezmapowanych adresów | |

Flagi `--serial`, `--tcp` i `--rtu-over-tcp` automatycznie wyłączają symulator; dodaj `--sim`, jeśli chcesz go mimo to
uruchomić.

## Interfejs webowy

| Widok | Co robi |
|-------|---------|
| **Dashboard** | wartości na żywo (karty / wykresy), zakres historii od 5 min do 30 dni, szczegóły wykresu, eksport CSV |
| **Urządzenia** | liczniki odczytywane w tle: magistrala, Unit ID, preset, interwał; rozpoznawanie modelu |
| **Skaner** | odczyt dowolnego zakresu rejestrów z dekodowaniem i podpowiedziami, Live, szukanie Unit ID, tworzenie presetu |
| **Presety** | biblioteka wbudowana (tylko do odczytu, można skopiować) i własne presety z edytorem JSON i walidacją |
| **Połączenia** | magistrale: RS-485 (RTU/ASCII), TCP, RTU-over-TCP, UDP; test połączenia |
| **Integracje** | MQTT / Home Assistant, ustawienia historii, Prometheus |
| **Pomoc** | wbudowana dokumentacja |

Interfejs działa na telefonie (menu zwija się do górnego paska).

## Magistrale i urządzenia

- **Magistrala** (bus) = jedno fizyczne połączenie: port RS-485 albo adres TCP. Wszystkie zapytania na magistrali
  przechodzą przez jedną kolejkę (RS-485 jest half-duplex), połączenie jest utrzymywane między odczytami.
- **Urządzenie** = licznik o danym **Unit ID** na magistrali, odczytywany wg **presetu** co **interwał** sekund.
  Na jednej magistrali RS-485 może być wiele liczników (różne Unit ID).

Odczyty wykonuje proces w tle - przeglądarki dostają wartości z pamięci, więc kilka otwartych kart nie zwiększa ruchu
na magistrali. Gdy licznik nie odpowiada, odstęp między próbami rośnie (do 30 s, ale nigdy nie jest krótszy niż
ustawiony interwał), żeby nie blokować innych urządzeń.

- Ramki RTU (RS-485 i RTU over TCP) nie mają numerów transakcji. Każda odpowiedź jest sprawdzana (funkcja, Unit ID,
  liczba rejestrów), a po timeoucie albo udanym ponowieniu aplikacja czeka na spóźnioną odpowiedź i ją odrzuca -
  kolejne bloki nie dostaną danych poprzednich, także za przezroczystą bramką (USR, Elfin).
- Gdy licznik odrzuci blok wyjątkiem, odczyt jest dzielony na mniejsze części, a podział zostaje zapamiętany tylko
  wtedy, gdy pomógł. Wyjątki 05/06 („zajęte”) nie zmieniają planu, a plan jest układany od nowa co godzinę.
- Ta sama nazwa portu pod różnymi aliasami (`/dev/serial0` i `/dev/ttyAMA0`, `/dev/serial/by-id/...` i
  `/dev/ttyUSB0`) to jedna magistrala; usunięte albo zmienione połączenie od razu zwalnia port.

## Presety - format

Preset to plik JSON z mapą rejestrów. Wbudowane leżą w `presets/library/` (tylko do odczytu), własne w `presets/`.
Stary format (v2 i wcześniejsze wersje aplikacji) jest nadal obsługiwany - wszystkie nowe pola są opcjonalne.
Wyjątek: `"byte_order": "little_endian"` oznacza teraz pełne odwrócenie bajtów (DCBA); w wersji 2 działał jak
`word_swap`, więc preset, który wtedy dawał poprawne wartości, wymaga `word_swap` (CDAB).

```json
{
  "name": "Mój licznik",
  "manufacturer": "Producent",
  "model": "XYZ-3",
  "phases": 3,
  "register_type": "holding",
  "byte_order": "ABCD",
  "data_type": "float32",
  "address_offset": 0,
  "serial": {"baudrate": 9600, "parity": "E", "stopbits": 1},
  "read": {"max_block": 64, "max_gap": 10},
  "probe": "voltage_l1",
  "registers": {
    "voltage_l1":    {"address": 0,        "unit": "V",   "decimals": 1, "group": "voltage", "label": "Napięcie L1"},
    "current_l1":    {"address": "0x0010", "unit": "A",   "decimals": 2, "group": "current", "label": "Prąd L1",
                      "type": "int32", "byte_order": "CDAB", "scale": 0.001},
    "energy_import": {"address": 256,      "unit": "kWh", "decimals": 2, "group": "energy",  "label": "Energia pobrana",
                      "type": "uint32", "scale": 0.01, "register_type": "input"},
    "power_l1":      {"address": 20, "type": "uint16", "invalid": [65535], "unit": "W", "group": "power",
                      "label": "Moc czynna L1"}
  }
}
```

| Pole | Opis |
|------|------|
| `name`, `manufacturer`, `model`, `description`, `source` | opis presetu (lista presetów, tabela liczników); `source` - skąd pochodzi mapa rejestrów |
| `phases` | liczba faz: 1, 2 lub 3 (domyślnie 3) |
| `serial` | fabryczne ustawienia portu (`baudrate`, `bytesize`, `parity`, `stopbits`) - tylko informacyjnie (kolumna *Port (fabr.)*, ostrzeżenie w formularzu urządzenia); parametry połączenia ustawiasz w magistrali |
| `register_type` | domyślna funkcja: `input` (FC04, domyślnie) lub `holding` (FC03); każdy rejestr może ją nadpisać |
| `data_type` / `type` | `int8`, `uint8` (połowa rejestru), `int16`, `uint16`, `int32`, `uint32`, `float32`, `int64`, `uint64`, `float64` (domyślnie `float32`) |
| `byte_order` | `ABCD` (big-endian), `CDAB` (word swap), `BADC` (byte swap), `DCBA` (little-endian); aliasy `big_endian`, `word_swap`, `byte_swap`, `little_endian` (domyślnie `ABCD`) |
| `address` | adres protokołu liczony od 0, liczba lub napis `"0x0156"` |
| `address_offset` | dodawany do każdego adresu - np. `-1`, gdy przepisujesz adresy z dokumentacji liczone od 1, albo `-30001` / `-40001` dla notacji 3xxxx / 4xxxx |
| `scale`, `offset` | wartość = surowa × `scale` + `offset` (np. `0.1` dla napięcia w 0.1 V, `1000` dla mocy w kW → W) |
| `invalid` | surowe wartości oznaczające brak danych (np. `65535`) |
| `scale_from` | klucz rejestru z wykładnikiem: wartość × 10^(jego wartość) - SunSpec „scale factor”, Gossen EnergyMID |
| `scale_from_mode` | `pow10` (domyślnie) albo `multiply` - mnożnikiem jest sama wartość wskazanego rejestru |
| `decimals`, `unit`, `group`, `label` | prezentacja na dashboardzie |
| `read.max_block` / `read.max_gap` | maks. rejestrów w jednym zapytaniu (1-125) i maks. dziura łączona w jeden odczyt |
| `probe` | rejestr używany przy rozpoznawaniu modelu |

**Kolejność bajtów na przykładzie 230.0 V** (float32 = `0x43660000`):

| Kolejność | Rejestr 1 | Rejestr 2 | Kto używa |
|-----------|-----------|-----------|-----------|
| ABCD | `0x4366` | `0x0000` | Eastron, Orno, Finder, Schneider, Janitza, Chint (większość) |
| CDAB | `0x0000` | `0x4366` | Carlo Gavazzi (int32), część bramek i falowników |
| BADC | `0x6643` | `0x0000` | rzadko |
| DCBA | `0x0000` | `0x6643` | rzadko |

**Klucze kanoniczne.** Jeżeli znaczenie się zgadza, używaj kluczy z `modbus_dash/quantities.py`
(`voltage_l1`, `current_n`, `power_total`, `pf_l1`, `frequency`, `energy_import`, `energy_export`, `thd_v_l1`, ...).
Dzięki nim działa symulator, kolory faz, Home Assistant (klasy urządzeń) i rozpoznawanie modelu.

**Grupy** (kolejność na dashboardzie): `voltage` → `line_volt` → `current` → `power` → `total` → `pf` → `system` →
`energy` → `thd` → `other`.

## Obsługiwane liczniki

Tabela odpowiada presetom z katalogu `presets/library`. Kolumna *Port (fabr.)* pokazuje ustawienia fabryczne
tylko wtedy, gdy potwierdza je dokumentacja - zawsze sprawdź ustawienia w menu licznika.

| Producent | Model | Fazy | Rejestry | Typy danych | Kolejność | Port (fabr.) | Wartości | Preset |
|---|---|---|---|---|---|---|---|---|
| ABB | A43 / A44 / B23 / B24 (wersje z RS-485 Modbus), także 1-fazowy B21 | 3 | Holding (FC03) | int16, int32, int64, uint16, uint32, uint64 | ABCD | - | 46 | `abb_a43_a44_b23_b24` |
| B+G e-tech | DS100-00B / DS100-30B | 3 | Holding (FC03) | int16, int32, uint16, uint32 | ABCD | 9600 8N1 | 51 | `bg_etech_ds100` |
| B+G e-tech | WS100-1943 / WS100-19L3 | 1 | Holding (FC03) | int16, int32, uint16, uint32 | ABCD | 9600 8N1 | 19 | `bg_etech_ws100` |
| Bernecker Engineering | MPM3PM | 3 | Holding (FC03) | int32, uint32 | ABCD | - | 23 | `bernecker_mpm3pm` |
| Carlo Gavazzi | EM24-DIN AV9 / AV0 / AV5 / AV6 z portem RS485 (starszy protokół EM24-DIN v2) | 3 | Input (FC04) | int16, int32 | CDAB | - | 40 | `carlo_gavazzi_em24` |
| Carlo Gavazzi | EM24DINAV23XE1X / EM24DINAV53XE1X (także wersje PFA i PFB) | 3 | Input (FC04) | int16, int32, uint16 | CDAB | - | 39 | `carlo_gavazzi_em24_e1` |
| Carlo Gavazzi | EM330-DIN / EM340-DIN / ET330-DIN / ET340-DIN (port S1, RS485) | 3 | Input (FC04) | int16, int32 | CDAB | 9600 8N1 | 41 | `carlo_gavazzi_em330_em340` |
| Chint | DDSU666 (Modbus RTU) | 1 | Holding (FC03) | float32 | ABCD | - | 9 | `chint_ddsu666` |
| Chint | DTSU666 / DSSU666 | 3 | Holding (FC03) | float32 | ABCD | 9600 8N1 | 30 | `chint_dtsu666` |
| DZG Metering | DVH4013 (wersja z RS-485 Modbus) | 3 | Holding (FC03) | uint32 | ABCD | 9600 8E1 | 18 | `dzg_dvh4013` |
| Eastron | SDM120-Modbus | 1 | Input (FC04) | float32 | ABCD | 2400 8N1 | 16 | `eastron_sdm120` |
| Eastron | SDM220-Modbus / SDM230-Modbus | 1 | Input (FC04) | float32 | ABCD | - | 14 | `eastron_sdm220_sdm230` |
| Eastron | SDM54-M / SDM54-2T | 3 | Input (FC04) | float32 | ABCD | 9600 8N1 | 53 | `eastron_sdm54` |
| Eastron | SDM630-Modbus V1 / V2 (MID i non-MID) | 3 | Input (FC04) | float32 | ABCD | 9600 8N1 | 70 | `eastron_sdm630` |
| Eastron | SDM72D-M (pierwsza wersja) | 3 | Input (FC04) | float32 | ABCD | - | 4 | `eastron_sdm72` |
| Eastron | SDM72D-M-2 (SDM72DM-V2) | 3 | Input (FC04) | float32 | ABCD | 9600 8N1 | 38 | `eastron_sdm72_v2` |
| Eastron | SMART X96-1A | 3 | Input (FC04) | float32 | ABCD | 9600 8N1 | 70 | `eastron_smart_x96_1a` |
| Eltako | DSZ15DZMOD / DSZ16 (format danych integer) | 3 | Input (FC04) | int32, uint32 | ABCD | 9600 8N1 | 16 | `eltako_dsz15dzmod` |
| Eltako | DSZ16D / DSZ16DZ / DSZ16WD / DSZ16WDZ (także wersje bez MID z literą E) | 3 | Input (FC04) | int32, uint32 | ABCD | 9600 8N1 | 40 | `eltako_dsz16` |
| Eltako | WSZ16D / WSZ16DZ (także wersje bez MID z literą E) | 1 | Input (FC04) | int32, uint32 | ABCD | 9600 8N1 | 14 | `eltako_wsz16` |
| Finder | 7M.24 (wersje z RS485 Modbus RTU) | 1 | Input (FC04) | float32 | ABCD | - | 16 | `finder_7m24` |
| Finder | 7M.38.8.400.xxxx (wersje z RS485 Modbus RTU) | 3 | Input (FC04) | float32 | ABCD | - | 45 | `finder_7m38` |
| Gossen Metrawatt | EM2281 / EM2289 / EM2381 / EM2387 / EM2389 (U228x-W7 / U238x-W7) | 3 | Input (FC04) | int16, int8, uint16, uint32 | ABCD | - | 34 | `gossen_metrawatt_energymid` |
| Hiking | DDS238-2 ZN/S / DDS238-2 ZN/SR | 1 | Holding (FC03) | int16, uint16, uint32 | ABCD | 9600 8N1 | 8 | `hiking_dds238_2_zn_s` |
| Inepro Metering | PRO1-Mod | 1 | Holding (FC03) | float32 | ABCD | 9600 8E1 | 25 | `inepro_pro1` |
| Inepro Metering | PRO380-Mod (także wersja CT) | 3 | Holding (FC03) | float32 | ABCD | 9600 8E1 | 59 | `inepro_pro380` |
| Janitza | B23 312-10J / B24 312-10J (wersje z RS-485 Modbus) | 3 | Holding (FC03) | float32 | ABCD | - | 34 | `janitza_b23_b24` |
| Lovato Electric | DMG610 (wspólna mapa serii DMG6..: DMG615, DMG611R, DMG620) | 3 | Input (FC04) | int32, uint32, uint64 | ABCD | 9600 8N1 | 44 | `lovato_dmg610` |
| OEM (różne marki) | DDM18SD (RS485 Modbus RTU) | 1 | Input (FC04) | float32 | ABCD | - | 8 | `ddm18sd` |
| Orno | OR-WE-504 | 1 | Holding (FC03) | uint16, uint32 | ABCD | 9600 8E1 | 9 | `orno_or_we_504` |
| Orno | OR-WE-514 / OR-WE-515 | 1 | Holding (FC03) | int16, int32, uint16, uint32 | ABCD | 9600 8E1 | 13 | `orno_or_we_514` |
| Orno | OR-WE-516 / OR-WE-517 | 3 | Holding (FC03) | float32 | ABCD | 9600 8E1 | 59 | `orno_or_we_517` |
| Orno | OR-WE-525 / OR-WE-526 | 1 | Input (FC04) | int16, int32 | ABCD | 9600 8N1 | 25 | `orno_or_we_525` |
| Peacefair | PZEM-004T v3.0 (10 A / 100 A) / PZEM-014 / PZEM-016 | 1 | Input (FC04) | int16, uint16, uint32 | CDAB | 9600 8N1 | 7 | `peacefair_pzem_004t` |
| Peacefair | PZEM-017 / PZEM-003 | 1 | Input (FC04) | int16, uint16, uint32 | CDAB | 9600 8N2 | 6 | `peacefair_pzem_017` |
| Saia Burgess Controls | ALE3D5FD10C2A00 / ALE3D5FD10C3A00 (MID) | 3 | Holding (FC03) | int16, uint16, uint32 | ABCD | - | 19 | `saia_burgess_ale3` |
| Schneider Electric | iEM3150 / iEM3155 / iEM3250 / iEM3255 / iEM3350 / iEM3355 | 3 | Holding (FC03) | float32, int64 | ABCD | - | 26 | `schneider_iem3000` |
| Shelly | Pro 3EM / Pro 3EM-3CT63 / Pro 3EM-120 / Pro 3EM-400 | 3 | Input (FC04) | float32 | CDAB | - | 30 | `shelly_pro_3em` |
| Siemens | SENTRON PAC2200 (7KM2200, RS485 lub Ethernet) | 3 | Input (FC04) | float32, float64 | ABCD | 19200 8N2 | 34 | `siemens_pac2200` |
| Socomec | Countis E33 / E43 (rodzina E3x / E4x z tablicą JBUS common) | 3 | Holding (FC03) | int32, uint32 | ABCD | - | 35 | `socomec_countis_e3x_e4x` |
| SolarEdge | SE-MTR-3Y; ta sama mapa rejestrów: WattNode WNC-3Y/3D-xxx-MB (np. SE-WNC-3Y-400-MB-K1) | 3 | Input (FC04) | float32 | CDAB | - | 50 | `solaredge_se_mtr_3y` |
| WAGO | 879-3000 (4PU) / 879-3020 (4PS) / 879-3040 (2PU CT) | 3 | Holding (FC03) | float32 | ABCD | 9600 8E1 | 47 | `wago_879_30x0` |

Mapy rejestrów oparto głównie na projekcie [mbmd](https://github.com/volkszaehler/mbmd) (licencja BSD-3) oraz
dokumentacji producentów (źródło w polu `source` każdego presetu). Brakuje Twojego licznika? Zeskanuj go skanerem,
utwórz preset i podziel się nim.

## Skaner rejestrów

1. Wybierz magistralę, Unit ID i typ rejestrów (Input FC04 / Holding FC03 / Coils FC01 / Discrete FC02).
2. Podaj zakres (dziesiętnie, `0x...` albo w notacji 30001/40001 - zostanie przeliczony na adres od 0).
3. **Skanuj** - każdy adres jest dekodowany jako u16/i16, float32 we wszystkich 4 kolejnościach i int32/uint32.
   Kolumna *Identyfikacja* podpowiada, co to może być: częstotliwość (45-65 Hz), napięcie międzyfazowe (340-440 V),
   fazowe (180-260 V lub 100-130 V), cos φ, prąd, moc, THD, energia. Podpowiedź wybiera też najbardziej
   prawdopodobną kolejność bajtów i skalę dla liczb całkowitych (np. `2301` → 230.1 V przy `scale` 0.1).
4. **Live** - skan powtarzany co ~1.5 s, zmienione komórki są podświetlane. Włącz czajnik i zobacz, które rejestry
   skoczyły o ~2000 W.
5. **Utwórz preset** - propozycja presetu z rozpoznanych rejestrów (z kluczami kanonicznymi, gdy wzorzec jest pewny)
   trafia do edytora; zapisujesz ją po poprawkach. Kolejność bajtów i wyrównanie wartości 32-bit (adresy parzyste
   albo nieparzyste) aplikacja wybiera sama na podstawie wszystkich wierszy. Przy skanie *Co 1 rejestr* dane często
   pasują prawie równie dobrze do drugiego wariantu (np. ABCD od adresów parzystych i CDAB od nieparzystych) - wtedy
   szkic dostaje ostrzeżenie: porównaj wartości z wyświetlaczem licznika albo wybierz drugą kolejność ręcznie.

Skaner omija dziury w mapie rejestrów: gdy licznik odrzuci zapytanie (wyjątek 02), blok jest dzielony aż do
pojedynczych rejestrów (pary rejestrów zostają razem), a nieczytelne zakresy są wypisane nad tabelą. Zapytania mają
maks. 80 rejestrów (limit m.in. liczników Eastron). Limit zapytań na jeden skan rośnie z zakresem (co najmniej 400);
jeśli zostanie osiągnięty, niesprawdzona reszta zakresu jest pokazana osobno, a nie jako „nieczytelna”.

**Szukaj urządzeń** sprawdza Unit ID z zakresu (np. 1-247) krótkim zapytaniem. **Rozpoznaj licznik** (Urządzenia)
odczytuje kilka kluczowych rejestrów według każdego presetu z biblioteki i ocenia, który daje wiarygodne wartości.

## Historia i eksport

- W pamięci: ostatnie 3600 odczytów każdego urządzenia (pełna rozdzielczość).
- W SQLite (`data/history.sqlite`): średnie/min/maks z przedziałów 60 s, przechowywane 30 dni (ustawienia w
  **Integracje**). Jeden zapis na przedział - oszczędza kartę SD.
- Eksport CSV z dashboardu (`;` jako separator, przecinek dziesiętny - otwiera się poprawnie w polskim Excelu).

## Integracje: MQTT / Home Assistant / Prometheus

**MQTT** (wymaga `pip install paho-mqtt`), konfiguracja w **Integracje**:

| Topic | Zawartość |
|-------|-----------|
| `modbus-dash/status` | `online` / `offline` (retained, LWT) |
| `modbus-dash/<urządzenie>/state` | JSON `{"voltage_l1": 230.1, ..., "ts": 1730000000.0}` |
| `modbus-dash/<urządzenie>/availability` | `online` / `offline` |

Z włączonym **Home Assistant discovery** każda wielkość pojawia się w HA jako encja z poprawną klasą
(`voltage`, `current`, `power`, `energy` z `total_increasing` itd.) - energia (Wh, kWh, MWh) od razu nadaje się do
panelu Energia.

- Edycja presetu od razu aktualizuje encje (nowe rejestry pojawiają się, usunięte znikają).
- Urządzenie wyłączone albo z niepoprawnym presetem jest w HA `offline`, a nie „zamrożone” na ostatnim odczycie.
- Encje usuniętych urządzeń są sprzątane przy każdym połączeniu z brokerem (także po restarcie aplikacji), a zmiana
  prefiksu topików usuwa encje ze starym prefiksem.
- Zapisane hasło MQTT nie jest pokazywane w interfejsie. Po zmianie adresu, portu, użytkownika albo TLS trzeba je
  wpisać ponownie - aplikacja nie wyśle zapisanego hasła do innego brokera.

**Prometheus**: `GET /metrics`, np.

```yaml
scrape_configs:
  - job_name: modbus-dash
    static_configs: [{targets: ["raspberrypi.local:5000"]}]
```

Metryki: `modbus_dash_value{device,device_name,key,label,unit,group}`, `modbus_dash_up`,
`modbus_dash_polls_total`, `modbus_dash_poll_failures_total`, `modbus_dash_poll_duration_seconds`,
`modbus_dash_last_poll_timestamp_seconds`, `modbus_dash_last_success_timestamp_seconds`, `modbus_dash_info{version}`.

## Symulator

Własny serwer Modbus (niezależny od zmian API pymodbus) z modelem instalacji 3-fazowej: napięcia z dryfem i szumem,
zmienne obciążenia, fotowoltaika na L3 (ujemna moc i energia oddana), prąd neutralny jako suma wektorowa, THD,
energia narastająca w czasie. Obsługuje FC 01-06, 08 (tylko echo, podfunkcja 00), 15, 16, 17, 43/14 (identyfikacja urządzenia).

```bash
python app.py --sim-preset eastron_sdm630:2 --sim-preset carlo_gavazzi_em24:3   # dodatkowe liczniki
python app.py --sim-framing rtu                                                 # RTU-over-TCP
python -m modbus_dash.simulator --help                                          # samodzielny symulator
```

Samodzielny symulator potrafi też udawać licznik na porcie szeregowym, także bez sprzętu: `--pty` tworzy wirtualny
port (Linux, macOS) i wypisuje jego ścieżkę, którą podajesz dashboardowi jako `--serial`. `--delay MS` spowalnia
odpowiedzi (wolny licznik), a `--gateway-errors` (tylko Modbus TCP, `--framing tcp`) odpowiada na nieznany Unit ID wyjątkiem 0x0B, jak bramka.

```bash
python -m modbus_dash.simulator --port 0 --pty --preset eastron_sdm120   # "Wirtualny port szeregowy: /dev/pts/3"
python app.py --no-sim --serial /dev/pts/3 --preset eastron_sdm120       # odczyt przez RTU, jak z prawdziwego licznika
```

Symulator wypełnia rejestry dowolnego presetu (typy, kolejność bajtów, skala), więc pozwala przetestować preset
przed podłączeniem prawdziwego licznika.

## Raspberry Pi i RS-485

### UART

```bash
sudo raspi-config
# Interface Options -> Serial Port -> login shell: NIE, hardware serial: TAK -> reboot
ls -l /dev/serial0
```

- **RPi 3 / 4 / Zero W / Zero 2 W**: pełny UART (PL011, `ttyAMA0`) obsługuje domyślnie Bluetooth, a `serial0`
  wskazuje na **mini-UART** (`ttyS0`). Mini-UART **nie obsługuje parzystości ani 2 bitów stopu** (jądro po cichu
  przełącza go na 8N1), a jego prędkość zależy od taktowania procesora. Liczniki 8E1 (np. Orno, Schneider, DZG)
  na mini-UART dają same timeouty - Modbus Dash wykrywa tę sytuację i zgłasza ją od razu. Rozwiązanie: dopisz
  `dtoverlay=disable-bt` do `/boot/firmware/config.txt` (starsze systemy: `/boot/config.txt`), wykonaj
  `sudo systemctl disable hciuart` i uruchom ponownie - `serial0` wskaże wtedy pełny UART `ttyAMA0`
  (`readlink -f /dev/serial0`). Liczniki 8N1 (np. Eastron) działają także na mini-UART.
- **RPi 5**: nie ma mini-UART. UART na GPIO14/15 to `/dev/ttyAMA0` (włączany przez raspi-config albo
  `dtparam=uart0=on` w `config.txt`). Alias `serial0` może wskazywać `ttyAMA10` - to osobne złącze debug UART między
  portami micro-HDMI - dlatego na RPi 5 podawaj wprost `--serial /dev/ttyAMA0`.
- Uprawnienia: `sudo usermod -a -G dialout $USER` (wyloguj się i zaloguj ponownie).

### Okablowanie

```
Licznik            Nakładka RS-485             Raspberry Pi
  A ─────────────── A
  B ─────────────── B      transceiver ── UART (GPIO14/15)
  GND/COM ───────── GND   (jeśli licznik ma taki zacisk)
```

- **A ↔ A, B ↔ B**. Oznaczenia A/B (D+/D-) różnią się u producentów - w specyfikacji Modbus B to linia „+”,
  wiele nakładek oznacza ją odwrotnie. Jeśli brak odpowiedzi, zamień przewody (nic się nie uszkodzi).
- Skrętka; specyfikacja Modbus zaleca trzeci przewód wspólnej masy (GND/COM). Wiele liczników (np. Eastron SDM)
  nie ma takiego zacisku - wtedy wystarczy para A/B, ale przy długich odcinkach i zakłóceniach masa pomaga.
- **Terminacja 120 Ω** na obu końcach długiej magistrali (wiele nakładek ma zworkę); przy jednym liczniku na kilku
  metrach zwykle niepotrzebna.
- 32 obciążenia jednostkowe na segment (więcej z transceiverami 1/4 lub 1/8 UL), maks. 247 adresów Modbus;
  ok. 1000-1200 m przy 9600 bit/s. Każdy licznik musi mieć inny Unit ID i te same parametry portu.
- Większość nakładek (np. Waveshare RS485 CAN HAT, nakładki z automatycznym sterowaniem kierunkiem) działa bez
  dodatkowej konfiguracji. Nakładki wymagające ręcznego sterowania pinem DE/RE z GPIO nie są obsługiwane.
  Jeśli przejściówka odsyła własną transmisję (lokalne echo), zaznacz to w ustawieniach połączenia lub użyj
  `--local-echo`.

## Bezpieczeństwo

- Domyślnie dashboard nasłuchuje na wszystkich interfejsach **bez hasła** - włącz `--auth USER:HASŁO`
  (albo `Environment=MODBUS_DASH_AUTH=...` w systemd) lub ogranicz nasłuch `--host 127.0.0.1`.
- Zapytania zmieniające stan muszą mieć `Content-Type: application/json` i nie mogą pochodzić z obcej strony
  (ochrona przed CSRF - złośliwa strona otwarta w przeglądarce nie skasuje Twoich presetów).
- Bez `--auth` dashboard odpowiada tylko pod adresem IP, nazwą `localhost` i nazwą tego komputera (także
  `nazwa.local`) - to chroni przed atakiem *DNS rebinding*, w którym obca strona podszywa się pod adres Raspberry Pi.
  Jeśli otwierasz dashboard pod inną nazwą (wpis w DNS, reverse proxy), dodaj ją: `--allowed-host energia.lan`
  (albo `*.home.lan`). Z włączonym `--auth` nazwa hosta nie jest sprawdzana.
- Port szeregowy musi być ścieżką urządzenia (`/dev/ttyUSB0`, `COM3`) - adresy URL pyserial (`socket://`, `spy://`...)
  są odrzucane.
- Serwer waitress odrzuca zapytania większe niż 2 MB, zanim cokolwiek zapisze na dysk.
- Zapis do urządzeń (`/api/write`) jest wyłączony, dopóki nie podasz `--allow-write`.
- `--debug` uruchamia debugger Werkzeug, który pozwala wykonać dowolny kod - dlatego wymusza nasłuch na 127.0.0.1.
- Presety są zapisywane atomowo, identyfikatory są walidowane (brak path traversal). Plików biblioteki wbudowanej nie
  da się zmienić ani usunąć przez API; własny preset o tym samym identyfikatorze przesłania wbudowany, dopóki go nie
  usuniesz.

## Rozwiązywanie problemów

| Objaw | Przyczyna | Rozwiązanie |
|-------|-----------|-------------|
| Brak odpowiedzi (timeout) | zły Unit ID, baudrate lub parzystość | sprawdź menu licznika; **Szukaj urządzeń**; typowo 9600 8N1, Eastron SDM120 2400 8N1, Orno często 9600 8E1 (OR-WE-525/526: 8N1) |
| Brak odpowiedzi (timeout) | zamienione A/B, brak zasilania licznika | zamień A z B |
| Brak odpowiedzi, port otwarty | konsola na porcie / mini-UART RPi 3/4 (licznik 8E1) | `raspi-config` (login shell: NIE), `dtoverlay=disable-bt` |
| „Błąd transmisji: odpowiedź ma 0 rejestrów zamiast N” (skaner: „urządzenie nie zwróciło żadnych danych”) | adapter z lokalnym echem | zaznacz „Adapter z lokalnym echem” / `--local-echo` |
| „Brak uprawnień do portu …” | użytkownik spoza grupy `dialout` | `sudo usermod -a -G dialout $USER` (wyloguj się i zaloguj) |
| „Port … nie istnieje” | zła nazwa portu, UART wyłączony, adapter odłączony | `ls /dev/serial* /dev/ttyUSB*`; lista portów w zakładce Połączenia |
| Wyjątek 02 (niedozwolony adres) | rejestr nie istnieje w tym modelu | sprawdź preset / użyj skanera; aplikacja sama dzieli odrzucone bloki |
| Wyjątek 01 (niedozwolona funkcja) | licznik nie obsługuje FC04 lub FC03 | zmień `register_type` w presecie |
| Wartości ~1e-38, ~1e+38 lub bez sensu | zła kolejność bajtów | w skanerze porównaj kolumny ABCD/CDAB/BADC/DCBA |
| Wartości 10× / 1000× za duże | brak skali | ustaw `scale` (np. `0.1`, `0.001`) |
| Napięcie ~0, reszta OK | wartości typu int odczytywane jako float | ustaw `type` (np. `uint16`) |
| Urządzenie "nieaktualne" | licznik wolno odpowiada | zwiększ interwał, `--timeout`, `--delay-ms` |

## API REST

Odpowiedzi w JSON (oprócz eksportu CSV i `/metrics`); błędy jako `{"error": "..."}` z kodem 4xx/5xx.

| Endpoint | Metoda | Opis |
|----------|--------|------|
| `/api/info`, `/api/health` | GET | wersje, symulator, funkcje / stan urządzeń |
| `/api/presets` | GET, POST | lista presetów / nowy preset (nigdy nie nadpisuje istniejącego pliku; `copy_from`: kopia presetu) |
| `/api/presets/{id}` | GET, PUT, DELETE | preset (`?raw=1`: dokładna treść pliku; wbudowanych nie można zmieniać) |
| `/api/presets/validate` | POST | walidacja presetu |
| `/api/buses` | GET | lista połączeń (magistral) |
| `/api/buses/{id}` | PUT, DELETE | zapis / usunięcie połączenia (`?create=1`: błąd 409 zamiast nadpisania) |
| `/api/buses/{id}/ping`, `/api/buses/test` | POST | test połączenia (bez `unit`: nawiązanie połączenia; UDP: tylko z `unit`) |
| `/api/serial-ports` | GET | dostępne porty szeregowe |
| `/api/devices` | GET | lista urządzeń ze stanem |
| `/api/devices/{id}` | PUT, DELETE | zapis / usunięcie urządzenia (`?create=1`: błąd 409 zamiast nadpisania) |
| `/api/devices/{id}/read` | POST | natychmiastowy odczyt z licznika |
| `/api/devices/{id}/values` | GET | ostatnie wartości (z pamięci) |
| `/api/devices/{id}/history?seconds=` | GET | historia (pamięć lub SQLite) |
| `/api/devices/{id}/history.csv` | GET | eksport CSV |
| `/api/scan` | POST | skan zakresu rejestrów |
| `/api/scan/units`, `/api/detect` | POST | szukanie Unit ID / rozpoznawanie modelu (zadania w tle) |
| `/api/jobs/{id}` | GET, DELETE | postęp / anulowanie zadania |
| `/api/scan/preset` | POST | propozycja presetu z wyników skanu (opcjonalnie `byte_order`, `alignment`: `even`/`odd`; niepewne wyrównanie: `_warnings`, `_alternative`) |
| `/api/write` | POST | zapis rejestru / cewki (tylko z `--allow-write`) |
| `/api/settings/mqtt`, `/api/settings/history` | GET, PUT | integracje |
| `/metrics` | GET | metryki Prometheus |
| `/api/live`, `/api/ping`, `/api/config` | POST/GET | zgodność ze starszą wersją |

Przykład:

```bash
curl -s http://raspberrypi.local:5000/api/devices/cli/values | jq .values
```

## Architektura

```
┌──────────────────────── Przeglądarka ─────────────────────────┐
│ Dashboard │ Urządzenia │ Skaner │ Presety │ Połączenia │ ...  │
└───────────────┬───────────────────────────────────────────────┘
                │ REST (JSON)
┌───────────────┴──────────── app.py / modbus_dash ─────────────┐
│ web.py (Flask) ── scanner.py (skan, Unit ID, rozpoznawanie)   │
│      │                                                        │
│ poller.py (wątek na magistralę) ── history.py (SQLite)        │
│      │                          ├─ mqtt.py (MQTT / HA)        │
│      │                          └─ metrics.py (Prometheus)    │
│ planner.py (bloki) ── codec.py (typy, kolejność bajtów)       │
│ transport.py (Bus: RTU / ASCII / TCP / RTU-over-TCP / UDP)    │
└──────────┬───────────────────────────┬────────────────────────┘
      RS-485 / USB                 TCP :502 / bramka
      (liczniki)                   simulator.py :5020
```

| Moduł | Rola |
|-------|------|
| `codec.py` | dekodowanie/kodowanie typów i kolejności bajtów |
| `presets.py` | walidacja presetów, biblioteka + presety użytkownika |
| `planner.py` | grupowanie rejestrów w bloki (≤125, z dziurami), adaptacyjny podział po błędach |
| `transport.py` | jedno trwałe połączenie na magistralę, blokada, ponowienia, zgodność z pymodbus 3.6-3.15 |
| `poller.py` | odczyty w tle, bufor ostatnich wartości i historii |
| `heuristics.py` | podpowiedzi skanera, propozycje presetów, ocena wiarygodności |
| `scanner.py` | skan zakresów, szukanie Unit ID, rozpoznawanie modelu (zadania w tle) |
| `history.py` | historia w SQLite (średnia/min/maks z przedziałów) |
| `mqtt.py` | publikacja MQTT i discovery Home Assistant |
| `metrics.py` | metryki Prometheus |
| `quantities.py` | klucze kanoniczne wielkości, jednostki, klasy Home Assistant |
| `simulator.py` | serwer Modbus TCP / RTU-over-TCP / RTU (port szeregowy) z modelem fizycznym |
| `config.py` | `data/config.json`: magistrale, urządzenia, MQTT, historia |
| `web.py` | aplikacja Flask: interfejs i REST API |
