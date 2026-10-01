"""Konfiguracja trwała (data/config.json): magistrale, urządzenia, MQTT, historia.

Magistrala (bus) = fizyczne połączenie (port RS-485 albo host:port TCP).
Urządzenie (device) = licznik o danym Unit ID na magistrali, odczytywany wg presetu.
Flagi CLI (--serial itp.) nadpisują magistralę "default" tylko w pamięci.
"""

import copy
import json
import math
import os
import re
import tempfile
import threading
from pathlib import Path

ID_RE = re.compile(r"^[a-z0-9][a-z0-9_\-]{0,31}$")

DEFAULT_MQTT = {
    "enabled": False, "host": "localhost", "port": 1883, "username": "", "password": "",
    "tls": False, "topic_prefix": "modbus-dash", "ha_discovery": True,
    "ha_prefix": "homeassistant", "interval": 10, "retain": False,
}
DEFAULT_HISTORY = {"enabled": True, "retention_days": 30, "bucket_seconds": 60, "memory_points": 3600}


class ConfigError(ValueError):
    pass


def _check_id(kind, value):
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        raise ConfigError(f"{kind}: identyfikator może zawierać małe litery, cyfry, '_' i '-' (max 32 znaki)")
    return value


def validate_bus(bus_id, data):
    from .transport import TransportConfig  # import leniwy - transport wymaga pymodbus
    _check_id("magistrala", bus_id)
    if not isinstance(data, dict):
        raise ConfigError("magistrala: oczekiwano obiektu")
    name = str(data.get("name") or bus_id)[:64]
    try:
        cfg = TransportConfig.from_dict({k: v for k, v in data.items() if k != "name"})
    except (TypeError, ValueError) as e:
        raise ConfigError(f"magistrala '{bus_id}': {e}") from None
    return {"name": name, **cfg.to_dict()}


def validate_device(dev_id, data, buses):
    _check_id("urządzenie", dev_id)
    if not isinstance(data, dict):
        raise ConfigError("urządzenie: oczekiwano obiektu")
    bus = data.get("bus", "default")
    if bus not in buses:
        raise ConfigError(f"urządzenie '{dev_id}': nieznana magistrala '{bus}'")
    unit = data.get("unit", 1)
    if isinstance(unit, bool) or not isinstance(unit, int) or not 0 <= unit <= 255:
        raise ConfigError(f"urządzenie '{dev_id}': Unit ID musi być liczbą 0-255 (RS-485: 1-247)")
    interval = data.get("interval", 1.0)
    if isinstance(interval, bool) or not isinstance(interval, (int, float)) or not 0.2 <= interval <= 3600:
        raise ConfigError(f"urządzenie '{dev_id}': interwał musi być w zakresie 0.2-3600 s")
    preset = data.get("preset") or None
    if preset is not None and not isinstance(preset, str):
        raise ConfigError(f"urządzenie '{dev_id}': preset musi być tekstem")
    enabled = data.get("enabled", True)
    if enabled is None:
        enabled = True
    if not isinstance(enabled, bool):  # bool("false") == True - tylko prawdziwe wartości logiczne
        raise ConfigError(f"urządzenie '{dev_id}': 'enabled' musi być wartością logiczną (true/false)")
    return {
        "name": str(data.get("name") or dev_id)[:64],
        "bus": bus,
        "unit": unit,
        "preset": preset,
        "interval": float(interval),
        "enabled": enabled,
    }


def _merge(defaults, data, strict=False):
    """Uzupełnia brakujące pola wartościami domyślnymi. strict=True: błędny typ -> ConfigError."""
    out = dict(defaults)
    if isinstance(data, dict):
        for k, v in data.items():
            if k not in defaults or v is None:
                continue
            d = defaults[k]
            number = isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
            if isinstance(d, bool):
                ok = isinstance(v, bool)
            elif isinstance(d, int):
                # 3600.0 z serializatora JSON -> 3600; 3600.5 to błąd (deque(maxlen=...) wymaga int)
                ok = number and float(v).is_integer()
                v = int(v) if ok else v
            elif isinstance(d, float):
                ok = number
            else:
                ok = isinstance(v, type(d))
            if ok:
                out[k] = v
            elif strict:
                kind = ("logiczna" if isinstance(d, bool) else "liczba całkowita" if isinstance(d, int)
                        else "liczba" if isinstance(d, float) else "tekst")
                raise ConfigError(f"pole '{k}' ma nieprawidłowy typ (oczekiwano: {kind})")
    return out


def validate_mqtt(data, strict=False):
    m = _merge(DEFAULT_MQTT, data, strict)
    if not 1 <= int(m["port"]) <= 65535:
        raise ConfigError("MQTT: port 1-65535")
    if not 1 <= int(m["interval"]) <= 3600:
        raise ConfigError("MQTT: interwał 1-3600 s")
    prefix = str(m["topic_prefix"]).strip("/")
    if not prefix or any(c in prefix for c in "+#"):
        raise ConfigError("MQTT: nieprawidłowy prefiks topiców")
    m["topic_prefix"] = prefix
    return m


def validate_history(data, strict=False):
    h = _merge(DEFAULT_HISTORY, data, strict)
    if not 1 <= int(h["retention_days"]) <= 3650:
        raise ConfigError("Historia: retencja 1-3650 dni")
    if int(h["bucket_seconds"]) not in (10, 30, 60, 300, 900):
        raise ConfigError("Historia: agregacja 10, 30, 60, 300 lub 900 s")
    if not 60 <= int(h["memory_points"]) <= 86400:
        raise ConfigError("Historia: bufor w pamięci 60-86400 punktów")
    return h


class _AnyBus:
    """Przy wczytywaniu nie odrzucamy urządzeń z nieznaną magistralą."""

    def __contains__(self, item):
        return True


class ConfigStore:
    """Wątkowo bezpieczny dostęp do konfiguracji z atomowym zapisem na dysk."""

    def __init__(self, path, defaults=None, overrides=None):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._overrides = overrides or {}  # {"buses": {id: cfg}} - tylko w pamięci (CLI)
        self._listeners = []
        # kolejność zapisów i powiadomień; słuchacze (np. MQTT) działają już bez self._lock,
        # więc wolny słuchacz nie blokuje get() w wątkach odpytywania
        self._write_lock = threading.RLock()
        self._data = self._load(defaults or {})

    def _load(self, defaults):
        data = {}
        if self.path.is_file():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                bad = self.path.with_suffix(".json.bad")
                try:
                    os.replace(self.path, bad)
                except OSError:
                    pass
                data = {}
        base = copy.deepcopy(defaults)
        if not data:
            data = base
        buses = {}
        for bid, b in (data.get("buses") or base.get("buses") or {}).items():
            try:
                buses[bid] = validate_bus(bid, b)
            except ConfigError:
                continue
        if "default" not in buses:
            buses["default"] = validate_bus("default", (base.get("buses") or {}).get("default", {"kind": "tcp"}))
        devices = {}
        for did, d in (data.get("devices") or {}).items():
            try:
                # magistrala może istnieć tylko przy części uruchomień (np. "sim" z CLI) - urządzenie
                # zostaje w konfiguracji i pokaże błąd, zamiast zniknąć przy następnym zapisie
                devices[did] = validate_device(did, d, _AnyBus())
            except ConfigError:
                continue
        sections = {}
        for name, validator in (("mqtt", validate_mqtt), ("history", validate_history)):
            try:
                sections[name] = validator(data.get(name))
            except ConfigError:  # wartość spoza zakresu (ręczna edycja pliku) - nie blokuje startu
                sections[name] = validator(None)
        return {"version": 1, "buses": buses, "devices": devices, **sections}

    # ── odczyt ─────────────────────────────────────────────────
    def get(self):
        """Kopia konfiguracji z nałożonymi nadpisaniami CLI (oznaczone "locked")."""
        with self._lock:
            data = copy.deepcopy(self._data)
        for bid, b in self._overrides.get("buses", {}).items():
            data["buses"][bid] = {**validate_bus(bid, b), "locked": True}
        for did, d in self._overrides.get("devices", {}).items():
            data["devices"][did] = {**validate_device(did, d, data["buses"]), "locked": True}
        return data

    def is_locked_bus(self, bus_id):
        return bus_id in self._overrides.get("buses", {})

    def is_locked_device(self, dev_id):
        return dev_id in self._overrides.get("devices", {})

    # ── zapis ──────────────────────────────────────────────────
    def on_change(self, fn):
        self._listeners.append(fn)

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh, indent=2, ensure_ascii=False)
                fh.write("\n")
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _notify(self, section):
        for fn in list(self._listeners):
            fn(section)

    def put_bus(self, bus_id, data):
        if self.is_locked_bus(bus_id):
            raise ConfigError(f"magistrala '{bus_id}' jest ustawiona flagami CLI - zmień parametry uruchomienia")
        bus = validate_bus(bus_id, data)
        with self._write_lock:
            with self._lock:
                self._data["buses"][bus_id] = bus
                self._save()
            self._notify("buses")
        return bus

    def delete_bus(self, bus_id):
        if self.is_locked_bus(bus_id):
            raise ConfigError(f"magistrala '{bus_id}' jest ustawiona flagami CLI - zmień parametry uruchomienia")
        with self._write_lock:
            with self._lock:
                if bus_id == "default":
                    raise ConfigError("nie można usunąć magistrali 'default'")
                if bus_id not in self._data["buses"]:
                    return False
                users = [d for d, v in self._data["devices"].items() if v["bus"] == bus_id]
                if users:
                    raise ConfigError(f"magistrala jest używana przez: {', '.join(users)}")
                del self._data["buses"][bus_id]
                self._save()
            self._notify("buses")
            return True

    def put_device(self, dev_id, data):
        if self.is_locked_device(dev_id):
            raise ConfigError(f"urządzenie '{dev_id}' jest ustawione flagami CLI - zmień parametry uruchomienia")
        with self._write_lock:
            with self._lock:
                buses = set(self._data["buses"]) | set(self._overrides.get("buses", {}))
                dev = validate_device(dev_id, data, buses)
                self._data["devices"][dev_id] = dev
                self._save()
            self._notify("devices")
        return dev

    def delete_device(self, dev_id):
        if self.is_locked_device(dev_id):
            raise ConfigError(f"urządzenie '{dev_id}' jest ustawione flagami CLI")
        with self._write_lock:
            with self._lock:
                if dev_id not in self._data["devices"]:
                    return False
                del self._data["devices"][dev_id]
                self._save()
            self._notify("devices")
            return True

    def put_section(self, name, data):
        """Aktualizacja sekcji; pola nieobecne w data zachowują bieżące wartości."""
        validator = {"mqtt": validate_mqtt, "history": validate_history}[name]
        if not isinstance(data, dict):
            raise ConfigError("oczekiwano obiektu")
        with self._write_lock:
            with self._lock:
                value = validator({**self._data[name], **data}, strict=True)
                self._data[name] = value
                self._save()
            self._notify(name)
        return value
