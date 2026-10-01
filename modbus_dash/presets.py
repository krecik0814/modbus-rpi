"""Presety - mapy rejestrów liczników.

Format JSON:

    {
      "name": "Eastron SDM630",
      "manufacturer": "Eastron", "model": "SDM630", "description": "...",
      "phases": 3,
      "register_type": "input",      # domyślna funkcja: input (FC04) | holding (FC03)
      "byte_order": "ABCD",          # domyślna kolejność: ABCD | CDAB | BADC | DCBA (+ aliasy)
      "data_type": "float32",        # domyślny typ: int8 uint8 int16 uint16 int32 uint32 float32 int64 uint64 float64
      "address_offset": 0,           # dodawany do adresów (np. -1 dla adresów 1-based z dokumentacji)
      "serial": {"baudrate": 9600, "parity": "N", "stopbits": 1},   # ustawienia fabryczne (informacyjnie)
      "read": {"max_block": 64, "max_gap": 10},                     # planowanie odczytów
      "probe": "voltage_l1",         # rejestr do automatycznego rozpoznawania licznika
      "registers": {
        "voltage_l1": {"address": 0, "unit": "V", "decimals": 1, "group": "voltage",
                       "label": "Napięcie L1",
                       # opcjonalnie, nadpisują domyślne z poziomu presetu:
                       "type": "float32", "byte_order": "ABCD", "register_type": "input",
                       "scale": 1, "offset": 0,
                       "invalid": [65535]}   # surowe wartości oznaczające "brak" (np. ABB)
      }
    }

Wartość fizyczna = surowa * scale + offset.

Skala z innego rejestru (SunSpec "scale factor", Gossen EnergyMID):
    "current_l1": {"address": 100, "type": "int16", "scale_from": "current_sf"}
    "current_sf": {"address": 104, "type": "int16", "group": "other"}
daje wartość = surowa * scale * 10^(wartość current_sf) + offset; z
"scale_from_mode": "multiply" mnożnik to sama wartość wskazanego rejestru.
"""

import json
import math
import os
import re
import tempfile
import threading
from pathlib import Path

from . import codec

_FUNCTION_ALIASES = {
    "input": "input", "ir": "input", "fc4": "input", "fc04": "input", "4": "input",
    "input_registers": "input", "input_register": "input",
    "holding": "holding", "hr": "holding", "fc3": "holding", "fc03": "holding", "3": "holding",
    "holding_registers": "holding", "holding_register": "holding",
}

# 64 rejestry: bezpieczne dla większości liczników (Eastron przyjmuje maks. 80, parzyście)
DEFAULT_MAX_BLOCK = 64
DEFAULT_MAX_GAP = 10
MODBUS_MAX_REGS = 125

PRESET_ID_RE = re.compile(r"^\w[\w .\-]{0,79}$", re.UNICODE)
REGISTER_KEY_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]{0,63}$")


class PresetFileError(ValueError):
    """Plik presetu istnieje, ale nie jest poprawnym JSON-em; .text zawiera jego treść."""

    def __init__(self, preset_id, message, text):
        super().__init__(f"plik presetu '{preset_id}' jest uszkodzony: {message}")
        self.preset_id = preset_id
        self.text = text


class PresetError(ValueError):
    """Błąd walidacji presetu; .errors zawiera listę komunikatów."""

    def __init__(self, errors):
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


def normalize_function(value):
    if value is None:
        return "input"
    key = str(value).strip().lower()
    try:
        return _FUNCTION_ALIASES[key]
    except KeyError:
        raise ValueError(f"nieznany typ rejestrów: {value!r} (input/holding)") from None


def parse_address(value):
    """Adres jako int albo napis dziesiętny/szesnastkowy ('0x0156')."""
    if isinstance(value, bool):
        raise ValueError("adres musi być liczbą")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        s = value.strip().lower()
        try:
            return int(s, 16) if s.startswith("0x") else int(s, 10)
        except ValueError:
            pass
    raise ValueError(f"nieprawidłowy adres: {value!r}")


def _number(value, name, errors, where, default):
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        errors.append(f"{where}: '{name}' musi być liczbą")
        return default
    if not _finite(value):  # json przyjmuje NaN/Infinity i ogromne liczby, przeglądarka już nie
        errors.append(f"{where}: '{name}' musi być skończoną liczbą")
        return default
    return value


def _finite(value):
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def normalize_preset(data):
    """Waliduje preset i zwraca postać znormalizowaną.

    Wynik: dict z kluczami meta (name, manufacturer, model, phases, ...),
    'max_block', 'max_gap', 'probe' oraz 'registers': {key: spec}, gdzie spec ma
    zawsze: key, address, type, order, function, count, scale, offset,
    decimals, unit, group, label, invalid, scale_from, scale_mode.
    Rzuca PresetError z listą wszystkich znalezionych problemów.
    """
    errors = []
    if not isinstance(data, dict):
        raise PresetError(["preset musi być obiektem JSON"])

    def opt(name, conv, default):
        try:
            return conv(data.get(name)) if data.get(name) is not None else default
        except ValueError as e:
            errors.append(f"preset: {e}")
            return default

    def_type = opt("data_type", codec.normalize_data_type, "float32")
    def_order = opt("byte_order", codec.normalize_byte_order, "ABCD")
    def_func = opt("register_type", normalize_function, "input")
    addr_offset = data.get("address_offset", 0)
    if isinstance(addr_offset, bool) or not isinstance(addr_offset, int):
        errors.append("preset: 'address_offset' musi być liczbą całkowitą")
        addr_offset = 0

    read = data.get("read") or {}
    if not isinstance(read, dict):
        errors.append("preset: 'read' musi być obiektem")
        read = {}
    max_block = read.get("max_block", DEFAULT_MAX_BLOCK)
    max_gap = read.get("max_gap", DEFAULT_MAX_GAP)
    if isinstance(max_block, bool) or not isinstance(max_block, int) or not 1 <= max_block <= MODBUS_MAX_REGS:
        errors.append(f"preset: 'read.max_block' musi być liczbą 1-{MODBUS_MAX_REGS}")
        max_block = DEFAULT_MAX_BLOCK
    if isinstance(max_gap, bool) or not isinstance(max_gap, int) or not 0 <= max_gap <= MODBUS_MAX_REGS:
        errors.append(f"preset: 'read.max_gap' musi być liczbą 0-{MODBUS_MAX_REGS}")
        max_gap = DEFAULT_MAX_GAP

    phases = data.get("phases", 3)
    if phases not in (1, 2, 3):
        errors.append("preset: 'phases' musi być 1, 2 lub 3")
        phases = 3

    regs_in = data.get("registers", {})
    if not isinstance(regs_in, dict):
        errors.append("preset: 'registers' musi być obiektem {klucz: {...}}")
        regs_in = {}

    registers = {}
    for key, spec in regs_in.items():
        where = f"rejestr '{key}'"
        if not REGISTER_KEY_RE.fullmatch(str(key)):
            errors.append(f"{where}: klucz może zawierać tylko litery, cyfry, '_', '.', '-' (max 64 znaki)")
            continue
        if not isinstance(spec, dict):
            errors.append(f"{where}: opis musi być obiektem")
            continue
        try:
            address = parse_address(spec.get("address")) + addr_offset
        except ValueError as e:
            errors.append(f"{where}: {e}")
            continue
        try:
            dtype = codec.normalize_data_type(spec.get("type", spec.get("data_type", def_type)))
            order = codec.normalize_byte_order(spec.get("byte_order", def_order))
            func = normalize_function(spec.get("register_type", def_func))
        except ValueError as e:
            errors.append(f"{where}: {e}")
            continue
        count = codec.DATA_TYPES[dtype][0]
        if not 0 <= address <= 0xFFFF - (count - 1):
            errors.append(f"{where}: adres {address} ({dtype}) wychodzi poza zakres 0-65535")
            continue
        scale = _number(spec.get("scale"), "scale", errors, where, 1)
        if scale == 0:
            errors.append(f"{where}: 'scale' nie może być 0")
            scale = 1
        offset = _number(spec.get("offset"), "offset", errors, where, 0)
        decimals = spec.get("decimals", 2)
        if isinstance(decimals, bool) or not isinstance(decimals, int) or not 0 <= decimals <= 10:
            errors.append(f"{where}: 'decimals' musi być liczbą całkowitą 0-10")
            decimals = 2
        invalid = spec.get("invalid", [])
        if not isinstance(invalid, list):
            invalid = [invalid]
        if any(isinstance(x, bool) or not isinstance(x, (int, float)) or not _finite(x) for x in invalid):
            errors.append(f"{where}: 'invalid' musi być liczbą lub listą liczb")
            invalid = []
        # porównujemy jako float: JSON z przeglądarki gubi precyzję dużych liczb 64-bit
        invalid = [float(x) for x in invalid]
        group = spec.get("group", "other")
        if not isinstance(group, str) or not group:
            group = "other"
        unit = spec.get("unit", "")
        label = spec.get("label", key)
        if not isinstance(unit, str):
            errors.append(f"{where}: 'unit' musi być tekstem")
            unit = ""
        if not isinstance(label, str):
            errors.append(f"{where}: 'label' musi być tekstem")
            label = str(key)
        scale_from = spec.get("scale_from")
        scale_mode = spec.get("scale_from_mode", "pow10")
        if scale_from is not None and not isinstance(scale_from, str):
            errors.append(f"{where}: 'scale_from' musi być kluczem innego rejestru")
            scale_from = None
        if scale_mode not in ("pow10", "multiply"):
            errors.append(f"{where}: 'scale_from_mode' musi być 'pow10' lub 'multiply'")
            scale_mode = "pow10"
        registers[key] = {
            "key": key, "address": address, "type": dtype, "order": order,
            "function": func, "count": count, "scale": scale, "offset": offset,
            "decimals": decimals, "unit": unit, "group": group, "label": label,
            "invalid": invalid, "scale_from": scale_from, "scale_mode": scale_mode,
        }

    for key, spec in registers.items():
        src = spec["scale_from"]
        if src is None:
            continue
        if src == key or src not in registers:
            errors.append(f"rejestr '{key}': 'scale_from' wskazuje nieistniejący rejestr '{src}'")
        elif registers[src]["scale_from"]:
            errors.append(f"rejestr '{key}': rejestr skali '{src}' nie może sam mieć 'scale_from'")

    probe = data.get("probe")
    if probe is not None and (not isinstance(probe, str) or probe not in registers):
        errors.append(f"preset: 'probe' wskazuje nieistniejący rejestr {probe!r}")
        probe = None

    if errors:
        raise PresetError(errors)

    meta = {k: data.get(k, "") for k in ("name", "manufacturer", "model", "description")}
    return {
        **meta,
        "phases": phases,
        "serial": data.get("serial") if isinstance(data.get("serial"), dict) else None,
        "max_block": max_block,
        "max_gap": max_gap,
        "probe": probe,
        "registers": registers,
    }


def slugify(name):
    """Bezpieczna nazwa pliku z nazwy presetu (zachowuje polskie litery i spacje)."""
    safe = "".join(c for c in str(name) if c.isalnum() or c in "-_ .").lstrip(" .-").rstrip(" .")
    safe = re.sub(r"\.{2,}", ".", safe)[:80].rstrip(" .")
    return safe or "preset"


def valid_id(preset_id):
    # fullmatch: "$" w match() przepuszcza końcowy znak nowej linii
    return (isinstance(preset_id, str) and bool(PRESET_ID_RE.fullmatch(preset_id))
            and ".." not in preset_id and not preset_id.endswith((".", " ")))


class PresetStore:
    """Presety użytkownika (katalog zapisywalny) + biblioteka wbudowana (tylko odczyt).

    Identyfikator presetu = nazwa pliku bez .json. Preset użytkownika o tym samym
    identyfikatorze przesłania wbudowany.
    """

    def __init__(self, user_dir, library_dir=None):
        self.user_dir = Path(user_dir)
        self.library_dir = Path(library_dir) if library_dir else None
        self.user_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._cache = {}  # path -> (mtime, raw, normalized|PresetError)

    # ── ścieżki ────────────────────────────────────────────────
    def _path(self, directory, preset_id):
        if directory is None or not valid_id(preset_id):
            return None
        p = (directory / f"{preset_id}.json").resolve()
        if p.parent != directory.resolve():
            return None
        return p

    def _locate(self, preset_id):
        """Zwraca (ścieżka, builtin) albo (None, False)."""
        p = self._path(self.user_dir, preset_id)
        if p and p.is_file():
            return p, False
        p = self._path(self.library_dir, preset_id)
        if p and p.is_file():
            return p, True
        return None, False

    def _load(self, path):
        mtime = path.stat().st_mtime_ns
        with self._lock:
            hit = self._cache.get(path)
            if hit and hit[0] == mtime:
                return hit[1], hit[2]
        raw = json.loads(path.read_text(encoding="utf-8"))
        try:
            norm = normalize_preset(raw)
        except PresetError as e:
            norm = e
        except Exception as e:  # jeden zły plik nie może zepsuć listy presetów
            norm = PresetError([f"błąd walidacji: {e}"])
        with self._lock:
            self._cache[path] = (mtime, raw, norm)
        return raw, norm

    # ── API ────────────────────────────────────────────────────
    def list(self):
        """Lista podsumowań presetów (użytkownika i wbudowanych)."""
        seen = {}
        dirs = [(self.library_dir, True), (self.user_dir, False)]
        for directory, builtin in dirs:
            if directory is None or not directory.is_dir():
                continue
            for f in sorted(directory.glob("*.json")):
                if not valid_id(f.stem):
                    continue
                try:
                    raw, norm = self._load(f)
                except (OSError, ValueError):
                    seen[f.stem] = {"id": f.stem, "_filename": f.stem, "name": f.stem,
                                    "builtin": builtin, "valid": False,
                                    "errors": ["nieprawidłowy plik JSON"], "register_count": 0}
                    continue
                if not isinstance(raw, dict):
                    seen[f.stem] = {"id": f.stem, "_filename": f.stem, "name": f.stem,
                                    "builtin": builtin, "valid": False,
                                    "errors": ["plik nie zawiera obiektu JSON"], "register_count": 0}
                    continue
                overrides = (not builtin) and f.stem in seen and seen[f.stem].get("builtin")
                seen[f.stem] = {
                    "id": f.stem,
                    "_filename": f.stem,
                    "name": raw.get("name") or f.stem,
                    "manufacturer": raw.get("manufacturer", ""),
                    "model": raw.get("model", ""),
                    "description": raw.get("description", ""),
                    "phases": raw.get("phases"),
                    "register_type": raw.get("register_type", "input"),
                    "serial": raw.get("serial"),
                    "builtin": builtin,
                    "overrides_builtin": bool(overrides),
                    "register_count": len(raw.get("registers") or {}),
                    "valid": not isinstance(norm, PresetError),
                    "errors": norm.errors if isinstance(norm, PresetError) else [],
                }
        return sorted(seen.values(), key=lambda p: (p["builtin"], str(p["name"]).lower()))

    def get_raw(self, preset_id):
        """Surowy JSON presetu z polami _id/_filename/_builtin albo None."""
        path, builtin = self._locate(preset_id)
        if not path:
            return None
        try:
            raw, _ = self._load(path)
        except ValueError as e:
            raise PresetFileError(preset_id, str(e), path.read_text(encoding="utf-8", errors="replace")) from None
        if not isinstance(raw, dict):
            raise PresetFileError(preset_id, "oczekiwano obiektu JSON", path.read_text(encoding="utf-8", errors="replace"))
        return {**raw, "_id": preset_id, "_filename": preset_id, "_builtin": builtin}

    def get_text(self, preset_id):
        """Dokładna treść pliku presetu (tekst) albo None."""
        path, _ = self._locate(preset_id)
        return path.read_text(encoding="utf-8") if path else None

    def get(self, preset_id):
        """Znormalizowany preset; None gdy brak; rzuca PresetError gdy niepoprawny."""
        path, builtin = self._locate(preset_id)
        if not path:
            return None
        try:
            _, norm = self._load(path)
        except ValueError as e:
            raise PresetError([f"plik presetu jest uszkodzony: {e}"]) from None
        if isinstance(norm, PresetError):
            raise norm
        return {**norm, "id": preset_id, "builtin": builtin}

    def is_builtin(self, preset_id):
        path, builtin = self._locate(preset_id)
        return bool(path) and builtin

    def unique_id(self, name):
        """Identyfikator dla nowego presetu, który nie nadpisze istniejącego pliku."""
        base = slugify(name)
        candidate, n = base, 2
        while (self.user_dir / f"{candidate}.json").exists():
            candidate = f"{base[:74]}-{n}"
            n += 1
        return candidate

    def save(self, preset_id, data):
        """Waliduje i zapisuje preset użytkownika (atomowo). Zwraca id."""
        if not valid_id(preset_id):
            raise PresetError([f"nieprawidłowy identyfikator presetu: {preset_id!r}"])
        clean = {k: v for k, v in data.items() if not str(k).startswith("_")}
        normalize_preset(clean)  # rzuca PresetError
        try:
            text = json.dumps(clean, indent=2, ensure_ascii=False, allow_nan=False)
        except ValueError:
            raise PresetError(["preset zawiera wartości NaN/Infinity (niedozwolone w JSON)"]) from None
        path = self._path(self.user_dir, preset_id)
        fd, tmp = tempfile.mkstemp(dir=str(self.user_dir), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text + "\n")
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        with self._lock:
            self._cache.pop(path, None)
        return preset_id

    def delete(self, preset_id):
        """Usuwa preset użytkownika. Zwraca True/False; wbudowanych nie da się usunąć."""
        path = self._path(self.user_dir, preset_id)
        if path and path.is_file():
            path.unlink()
            with self._lock:
                self._cache.pop(path, None)
            return True
        return False
