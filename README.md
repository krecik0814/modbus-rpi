# Modbus Energy Meter Dashboard — Dokumentacja

## Spis treści

1. [Opis projektu](#opis-projektu)
2. [Wymagania](#wymagania)
3. [Struktura plików](#struktura-plików)
4. [Instalacja](#instalacja)
5. [Uruchomienie](#uruchomienie)
6. [Argumenty CLI](#argumenty-cli)
7. [Interfejs webowy](#interfejs-webowy)
8. [Protokół Modbus — teoria](#protokół-modbus--teoria)
9. [Skaner rejestrów](#skaner-rejestrów)
10. [System presetów](#system-presetów)
11. [Dashboard — tryby wyświetlania](#dashboard--tryby-wyświetlania)
12. [Symulator](#symulator)
13. [Podłączenie fizyczne RS-485](#podłączenie-fizyczne-rs-485)
14. [Konfiguracja Raspberry Pi](#konfiguracja-raspberry-pi)
15. [Konfiguracja popularnych liczników](#konfiguracja-popularnych-liczników)
16. [Rozwiązywanie problemów](#rozwiązywanie-problemów)
17. [API REST](#api-rest)
18. [Architektura systemu](#architektura-systemu)

---

## Opis projektu

Modbus Dash to webowy system monitoringu liczników energii elektrycznej komunikujących się po protokole Modbus RTU (RS-485) lub Modbus TCP. Składa się z:

- **Serwera Flask** — backend obsługujący komunikację Modbus, zarządzanie presetami i REST API
- **Dashboardu HTML** — frontend w przeglądarce z live monitoringiem, skanerem rejestrów i edytorem presetów
- **Wbudowanego symulatora** — emuluje licznik 3-fazowy (35 rejestrów) do testów bez fizycznego sprzętu
- **Systemu presetów JSON** — mapy rejestrów dla różnych modeli liczników

System działa na dowolnym komputerze z Pythonem (Windows/Linux/macOS) w trybie symulacji TCP, oraz na Raspberry Pi z nakładką RS-485 HAT w trybie produkcyjnym z prawdziwymi licznikami.

---

## Wymagania

### Software

| Pakiet | Wersja | Wymagany |
|--------|--------|----------|
| Python | 3.10+ | Tak |
| Flask | 2.0+ | Tak |
| pymodbus | 3.6 — 3.12+ | Tak |
| pyserial | 3.0+ | Tylko RS-485 |

### Hardware (produkcja)

| Element | Opis |
|---------|------|
| Raspberry Pi | Model 3B+, 4B, lub 5 (z UART) |
| Nakładka RS-485 | Waveshare RS485/CAN HAT (SP3485) lub kompatybilna |
| Licznik energii | Dowolny z interfejsem Modbus RTU |
| Kabel | Skrętka 2-żyłowa (A, B) + opcjonalnie GND |

### Hardware (desktop — opcjonalnie)

| Element | Opis |
|---------|------|
| USB-to-RS485 adapter | Np. na chipie CH340 lub FTDI (15–30 zł) |

---

## Struktura plików

```
modbus-dash/
├── app.py                    # Backend Flask + symulator
├── templates/
│   └── index.html            # Frontend dashboard
├── presets/
│   ├── simulator_3f.json     # Preset wbudowanego symulatora
│   └── *.json                # Presety użytkownika
├── DOKUMENTACJA.md           # Ten plik
└── QUICKSTART_RPI.md         # Szybki start na RPi
```

---

## Instalacja

### Desktop (Windows/Linux/macOS)

```bash
pip install flask pymodbus
```

### Raspberry Pi

```bash
sudo apt update
pip install flask pymodbus pyserial
```

---

## Uruchomienie

### Desktop z symulatorem (domyślny)

```bash
cd modbus-dash
python app.py
```

Otwórz: `http://localhost:5000`

Symulator startuje automatycznie na porcie TCP 5020.

### RS-485 na Raspberry Pi

```bash
python app.py --serial /dev/ttyS0 --baudrate 9600
```

Dashboard: `http://<ip-rpi>:5000`

### TCP bez symulatora (bramka Modbus)

```bash
python app.py --no-sim
```

---

## Argumenty CLI

| Flaga | Opis | Domyślnie |
|-------|------|-----------|
| `--port` | Port HTTP dashboardu | 5000 |
| `--modbus-port` | Port symulatora Modbus TCP | 5020 |
| `--no-sim` | Nie uruchamiaj symulatora | wyłączone |
| `--serial` | Port szeregowy RS-485 (np. `/dev/ttyS0`, `COM3`) | brak |
| `--baudrate` | Prędkość transmisji | 9600 |
| `--parity` | Bit parzystości: `N` (None), `E` (Even), `O` (Odd) | N |
| `--stopbits` | Bity stopu: 1 lub 2 | 1 |
| `--debug` | Tryb debug Flask | wyłączone |

### Przykłady

```bash
# Desktop z symulatorem
python app.py

# RPi + Waveshare RS485 HAT
python app.py --serial /dev/ttyS0 --baudrate 9600

# RPi + licznik Orno (parity Even)
python app.py --serial /dev/ttyS0 --baudrate 9600 --parity E

# USB-to-RS485 na Windows
python app.py --serial COM3 --baudrate 9600

# Dashboard na innym porcie
python app.py --port 8080
```

Flaga `--serial` automatycznie wyłącza symulator.

---

## Interfejs webowy

### Dashboard

Główny widok monitoringu. Dwa tryby:

- **Karty** — wartości liczbowe pogrupowane kategoriami
- **Wykresy** — wykresy area z historią 60 odczytów (~1 min), Canvas 2D API, zero zależności

Dane odświeżane co 1 sekundę.

### Skaner rejestrów

- **Skan jednorazowy** — odpytuje zakres rejestrów, dekoduje w trzech byte order, auto-identyfikacja
- **Tryb Live** — ciągłe odpytywanie co 1.5s, aktualizacja w miejscu (bez migania)
- **Eksport CSV** — wszystkie nagrane skany z timestampami
- **Zapisz preset** — auto-tworzy preset z rozpoznanych rejestrów

### Presety

CRUD na mapach rejestrów: lista, edytor JSON, tworzenie, usuwanie.

### Ustawienia

Host, port TCP, Unit ID, informacja o trybie pracy, test połączenia.

### Pomoc

Wbudowana dokumentacja z tabelami parametrów i popularnych liczników.

---

## Protokół Modbus — teoria

### Czym jest Modbus

Protokół komunikacyjny z lat 70-tych, standard w automatyce. Urządzenie (slave) udostępnia tablicę rejestrów 16-bitowych. Master odpytuje: "daj mi N rejestrów od adresu X" i dostaje surowe bajty — bez nazw, bez typów.

### Typy rejestrów

| Typ | FC | Opis |
|-----|-----|------|
| Input Registers | 04 | 16-bit, read-only — pomiary (V, A, W, kWh) |
| Holding Registers | 03 | 16-bit, read/write — konfiguracja |
| Coils | 01 | 1-bit, read/write — sterowanie on/off |
| Discrete Inputs | 02 | 1-bit, read-only — stany binarne |

Liczniki energii: zwykle **Input Registers** (FC=04). Wyjątki: Schneider, Janitza → Holding (FC=03).

### Float32 w rejestrach

Float32 zajmuje 2 rejestry. Kolejność bajtów:

| Byte order | Kolejność | Kto używa |
|------------|-----------|-----------|
| Big-endian | AB CD | Eastron, Orno, Finder (większość) |
| Word-swap | CD AB | Carlo Gavazzi, niektóre Schneider |
| Little-endian | DC BA | Rzadko |

### Unit ID

Adres slave na magistrali RS-485 (1–247). Domyślnie 1. Przy jednym liczniku nie zmieniaj.

---

## Skaner rejestrów

### Heurystyka identyfikacji

| Zakres wartości | Identyfikacja |
|-----------------|---------------|
| 200–260 | Napięcie fazowe (V) |
| 340–420 | Napięcie międzyfazowe (V) |
| 0.01–100 | Prąd (A) |
| 49–51.5 | Częstotliwość (Hz) |
| 0.5–1.0 | cos φ |
| 100–50000 | Moc (W/VA/VAr) |
| 0.1–30 | THD (%) |
| > 100 | Energia (kWh) |

### Tryb Live — identyfikacja przez obserwację

Włącz Live, potem zmień obciążenie (np. włącz czajnik 2kW). Rejestry które skoczą o ~2000 to moc czynna. Te które się nie zmieniły to prawdopodobnie energia kumulatywna lub parametry konfiguracyjne.

---

## System presetów

### Format JSON

```json
{
  "name": "Eastron SDM630",
  "manufacturer": "Eastron",
  "model": "SDM630",
  "byte_order": "big_endian",
  "register_type": "input",
  "phases": 3,
  "registers": {
    "voltage_l1": {
      "address": 0,
      "unit": "V",
      "decimals": 1,
      "group": "voltage",
      "label": "Napięcie L1"
    }
  }
}
```

### Grupy (kolejność na dashboardzie)

`voltage` → `current` → `power` → `total` → `pf` → `system` → `energy` → `line_volt` → `thd` → `other`

---

## Symulator

Emuluje licznik 3-fazowy — 35 rejestrów float32:

- Napięcia fazowe ~230V (drift ±3V, szum gaussowski)
- Prądy 1.8–3.2A (zmienne obciążenie)
- Moce czynna/pozorna/bierna (obliczane z V×I×PF)
- cos φ 0.88–0.95
- Częstotliwość ~50Hz
- Energia akumulowana (kWh rosnące w czasie)
- Napięcia międzyfazowe ~400V
- THD napięcia i prądu

Startuje automatycznie. Wyłączany flagą `--no-sim` lub `--serial`.

---

## Podłączenie fizyczne RS-485

```
Licznik          Waveshare RS485 HAT          Raspberry Pi
  A ─────────────── A                          
  B ─────────────── B      SP3485 ──── GPIO ──── RPi
 (GND)───────────── GND   transceiver   UART
```

- **A ↔ A, B ↔ B** — jeśli nie działa, zamień (RS-485 toleruje)
- **GND** — przy odległości > 5m
- **Terminacja** — 120Ω na końcach magistrali (HAT ma jumper)
- **Max długość** — 1200m @ 9600 baud
- **Max urządzeń** — 32 (standard), 256 (z repeaterem)

---

## Konfiguracja Raspberry Pi

### 1. Włącz UART

```bash
sudo raspi-config
# → Interface Options → Serial Port
# → Login shell: NO
# → Hardware serial: YES
# → Reboot
```

### 2. Weryfikacja

```bash
ls -la /dev/ttyS0
```

### 3. Instalacja

```bash
pip install flask pymodbus pyserial
```

### 4. Uruchomienie

```bash
python app.py --serial /dev/ttyS0 --baudrate 9600
```

### 5. Autostart (systemd)

```bash
sudo nano /etc/systemd/system/modbus-dash.service
```

```ini
[Unit]
Description=Modbus Dash
After=network.target

[Service]
User=pi
WorkingDirectory=/home/pi/modbus-dash
ExecStart=/usr/bin/python3 app.py --serial /dev/ttyS0 --baudrate 9600
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable modbus-dash
sudo systemctl start modbus-dash
```

---

## Konfiguracja popularnych liczników

| Model | Fazy | Baudrate | Parity | Adres | Rejestry | Byte order |
|-------|------|----------|--------|-------|----------|------------|
| Eastron SDM120 | 1F | 2400 | N | 1 | Input (FC=04) | Big-endian |
| Eastron SDM230 | 1F | 2400 | N | 1 | Input (FC=04) | Big-endian |
| Eastron SDM630 | 3F | 2400 | N | 1 | Input (FC=04) | Big-endian |
| Orno OR-WE-504 | 1F | 9600 | E | 1 | Input (FC=04) | Big-endian |
| Orno OR-WE-514 | 3F | 9600 | E | 1 | Input (FC=04) | Big-endian |
| Orno OR-WE-517 | 3F | 9600 | E | 1 | Input (FC=04) | Big-endian |
| Finder 7E.78 | 3F | 9600 | N | 1 | Input (FC=04) | Big-endian |
| Carlo Gavazzi EM24 | 3F | 9600 | N | 1 | Input (FC=04) | Word-swap |
| Schneider iEM3155 | 3F | 19200 | E | 1 | Holding (FC=03) | Big-endian |
| Janitza UMG 96RM | 3F | 9600 | N | 1 | Holding (FC=03) | Big-endian |

---

## Rozwiązywanie problemów

| Problem | Przyczyna | Rozwiązanie |
|---------|-----------|-------------|
| Skaner zwraca zera | Zły baudrate | Eastron: 2400, Orno: 9600 |
| Skaner zwraca zera | Zły parity | Orno: parity E |
| Skaner zwraca zera | Odwrócone A/B | Zamień przewody |
| Wartości ~1e-38 | Zły byte order | W presecie: `"word_swap"` |
| Wartości ~1e-38 | Zły typ rejestrów | Spróbuj FC=03 zamiast FC=04 |
| Brak połączenia | Serwer nie działa | Sprawdź terminal z `python app.py` |
| Brak połączenia | Firewall | Otwórz port 5000 (HTTP) |
| UART nie działa | Brak uprawnień | `sudo usermod -a -G dialout $USER` |
| Wykresy puste | Za mało danych | Poczekaj 2-3 sekundy |

---

## API REST

| Endpoint | Metoda | Opis |
|----------|--------|------|
| `/api/presets` | GET | Lista presetów |
| `/api/presets/{name}` | GET | Szczegóły presetu |
| `/api/presets` | POST | Utwórz preset (body: JSON + `_save_as`) |
| `/api/presets/{name}` | PUT | Aktualizuj preset |
| `/api/presets/{name}` | DELETE | Usuń preset |
| `/api/scan` | POST | Skanuj rejestry (body: host, port, unit, start, end, register_type) |
| `/api/live` | POST | Odczyt wg presetu (body: host, port, unit, preset) |
| `/api/ping` | POST | Test połączenia |
| `/api/config` | GET | Aktualny tryb pracy |

---

## Architektura systemu

```
┌─────────────── Przeglądarka ───────────────────┐
│  Dashboard │ Skaner │ Presety │ Ustawienia      │
│            ↕ REST API (fetch, co 1s)            │
└────────────────────┬────────────────────────────┘
                     │ HTTP :5000
┌────────────────────┴────────────────────────────┐
│              Flask Backend (app.py)              │
│   /api/live  │  /api/scan  │  /api/presets      │
│              ↕ pymodbus client                   │
└──────────┬──────────────────┬───────────────────┘
           │                  │
     TCP :5020          Serial /dev/ttyS0
     (symulator)        (RS-485 → licznik)
```
