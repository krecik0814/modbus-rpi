"""Transport Modbus: magistrale TCP, RTU over TCP, UDP, RTU i ASCII na klientach pymodbus.

Magistrala (Bus) = jedno fizyczne połączenie (port RS-485 albo host:port)
współdzielone przez wszystkich użytkowników: odpytywanie, skaner, API.
Zapytania są szeregowane blokadą, połączenie jest utrzymywane między
zapytaniami i odnawiane po błędach. Ponowienia robimy sami, a błędy pymodbus
zamieniamy na ModbusError z polskim komunikatem.

Zgodność z pymodbus 3.6 .. 3.15 wykrywamy z sygnatur i atrybutów, nie z numeru wersji:
- Unit ID: argument device_id= (3.10+) albo slave= (starsze),
- ramkowanie: FramerType (3.7+), Framer (3.6) albo klasy framerów,
- nowy menedżer transakcji (sync_execute, 3.8+) sam ponawia wysyłkę -> retries=0;
  stary (3.6/3.7) nie ponawia wysyłki, ale 3.7 bez retries>=1 nie doczytuje ramek,
- część wersji zwraca ModbusIOException zamiast go rzucać,
- 3.10.0 odwraca kolejność bajtów w odpowiedziach FC01/FC02 (wykrywane próbną ramką),
- połączenia TCP i porty szeregowe otwieramy sami (dokładne komunikaty błędów,
  TCP_NODELAY, keepalive) i podajemy klientowi jako client.socket.

Zachowanie magistrali:
- ponowienia (retries) po timeoucie i błędzie transmisji, nie po odpowiedzi wyjątkiem;
  bezczynne połączenie TCP zerwane przez serwer dostaje jedną dodatkową próbę,
- TCP/UDP: po błędzie nowe połączenie (gubi spóźnione odpowiedzi i martwe sesje);
  RS-485: port zostaje otwarty, przed kolejną ramką cisza i czyszczenie wejścia,
- odpowiedź niepasująca do zapytania (inna funkcja/długość) to błąd "io",
- override() zmienia timeout/retries tylko dla bieżącego wątku (np. skaner),
- close() z innego wątku przerywa trwające zapytanie (po CLOSE_WAIT s).

Logi pymodbus (ERROR przy każdym timeoucie) są wyciszane, chyba że aplikacja sama
ustawiła poziom loggera "pymodbus" albo zmienną MODBUS_DASH_PYMODBUS_LOG (np. DEBUG).
"""

import contextlib
import dataclasses
import errno
import functools
import importlib
import inspect
import logging
import math
import os
import re
import socket
import threading
import time
from dataclasses import dataclass

log = logging.getLogger("modbus-dash.transport")

KINDS = ("tcp", "rtu_over_tcp", "udp", "rtu", "ascii")
NET_KINDS = ("tcp", "rtu_over_tcp", "udp")
SERIAL_KINDS = ("rtu", "ascii")
_KIND_ALIASES = {"serial": "rtu", "rs485": "rtu", "rs-485": "rtu", "modbus_tcp": "tcp", "socket": "tcp",
                 "rtu-over-tcp": "rtu_over_tcp", "rtuovertcp": "rtu_over_tcp", "rtu_tcp": "rtu_over_tcp"}
_PARITY_ALIASES = {"N": "N", "NONE": "N", "E": "E", "EVEN": "E", "O": "O", "ODD": "O"}

MAX_REGS = 125
MAX_BITS = 2000
MAX_WRITE_REGS = 123

TIMEOUT_RANGE = (0.05, 60.0)
MAX_RETRIES = 10
MAX_DELAY_MS = 5000
BAUD_RANGE = (300, 4_000_000)

RECOVERY_GAP = 0.05     # s ciszy po timeoucie na RS-485 (spóźniona odpowiedź), potem czyszczenie wejścia
CLOSE_WAIT = 2.0        # s czekania na trwające zapytanie przy zamykaniu
IDLE_CLOSE = 300.0      # s bezczynności, po których zamykamy połączenie
IDLE_DROP = 3600.0      # s bezczynności, po których BusManager zapomina magistralę

_HOST_RE = re.compile(r"^[A-Za-z0-9._:%\-]+$")  # nazwa, IPv4, IPv6 (także ze strefą %eth0)

EXCEPTION_NAMES = {
    1: "niedozwolona funkcja",
    2: "niedozwolony adres",
    3: "niedozwolona wartość",
    4: "błąd urządzenia",
    5: "potwierdzenie - operacja w toku",
    6: "urządzenie zajęte",
    8: "błąd parzystości pamięci",
    0x0A: "bramka - brak ścieżki do urządzenia",
    0x0B: "bramka - urządzenie nie odpowiada",
}


# ── błędy ─────────────────────────────────────────────────────
class ModbusError(Exception):
    """Błąd magistrali. kind: connection | timeout | exception | io (invalid - złe argumenty).

    code: kod wyjątku Modbus (kind == "exception", także 0x0A/0x0B z bramek).
    """

    detail = None           # oryginalny błąd pymodbus/pyserial (diagnostyka)
    connect_failed = False  # nie udało się otworzyć połączenia - ponawianie nie ma sensu

    def __init__(self, kind, message, code=None):
        super().__init__(message)
        self.kind = kind
        self.code = code


class InvalidRequest(ModbusError, ValueError):
    """Niepoprawne argumenty zapytania - nic nie zostało wysłane."""

    def __init__(self, message):
        super().__init__("invalid", message)


def exception_error(code):
    """Odpowiedź wyjątkiem -> ModbusError. Bramki: 0x0B jak timeout, 0x0A jak brak połączenia."""
    kind = {0x0A: "connection", 0x0B: "timeout"}.get(code, "exception")
    return ModbusError(kind, f"Wyjątek Modbus {code:02X}: {EXCEPTION_NAMES.get(code, 'nieznany kod')}", code)


def _conn_error(message):
    e = ModbusError("connection", message)
    e.connect_failed = True
    return e


# ── konfiguracja ──────────────────────────────────────────────
def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _as_int(value, msg):
    if isinstance(value, bool):
        raise ValueError(msg)
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    if not isinstance(value, int):
        raise ValueError(msg)
    return value


def _as_parity(value, msg):
    try:
        return _PARITY_ALIASES[str(value).strip().upper()]
    except KeyError:
        raise ValueError(msg) from None


def _as_float(value, msg):
    if isinstance(value, bool):
        raise ValueError(msg)
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise ValueError(msg) from None
    if not math.isfinite(v):
        raise ValueError(msg)
    return v


@dataclass(frozen=True)
class TransportConfig:
    kind: str = "tcp"            # tcp | rtu_over_tcp | udp | rtu | ascii
    host: str = "127.0.0.1"      # rodzaje sieciowe
    port: int = 502
    serial_port: str = ""        # rtu/ascii: /dev/serial0, /dev/ttyUSB0, COM3
    baudrate: int = 9600
    parity: str = "N"            # N/E/O
    stopbits: int = 1            # 1/2
    bytesize: int = 8            # 7/8
    timeout: float = 1.0         # s na jedno zapytanie
    retries: int = 1             # dodatkowe próby po timeoucie / błędzie transmisji
    delay_ms: int = 0            # dodatkowa cisza między ramkami

    def __post_init__(self):
        # sprawdzamy tylko pola znaczące dla danego rodzaju łącza
        if self.kind not in KINDS:
            raise ValueError(f"rodzaj połączenia musi być jednym z: {', '.join(KINDS)}")
        if self.kind in NET_KINDS:
            if not isinstance(self.host, str) or not self.host or len(self.host) > 253 \
                    or not _HOST_RE.match(self.host):
                raise ValueError("podaj poprawny adres hosta (IP lub nazwa)")
            if not _is_int(self.port) or not 1 <= self.port <= 65535:
                raise ValueError("port TCP musi być w zakresie 1-65535")
        else:
            if not isinstance(self.serial_port, str) or not self.serial_port.strip() \
                    or len(self.serial_port) > 255 or any(ch in self.serial_port for ch in "\0\r\n"):
                raise ValueError("podaj port szeregowy (np. /dev/serial0, /dev/ttyUSB0, COM3)")
            if not _is_int(self.baudrate) or not BAUD_RANGE[0] <= self.baudrate <= BAUD_RANGE[1]:
                raise ValueError(f"prędkość musi być w zakresie {BAUD_RANGE[0]}-{BAUD_RANGE[1]} bit/s")
            if self.parity not in ("N", "E", "O"):
                raise ValueError("parzystość musi być N, E lub O")
            if not _is_int(self.stopbits) or self.stopbits not in (1, 2):
                raise ValueError("bity stopu: 1 lub 2")
            if not _is_int(self.bytesize) or self.bytesize not in (7, 8):
                raise ValueError("bity danych: 7 lub 8")
        if isinstance(self.timeout, bool) or not isinstance(self.timeout, (int, float)) \
                or not TIMEOUT_RANGE[0] <= self.timeout <= TIMEOUT_RANGE[1]:
            raise ValueError(f"timeout musi być w zakresie {TIMEOUT_RANGE[0]:g}-{TIMEOUT_RANGE[1]:g} s")
        if not _is_int(self.retries) or not 0 <= self.retries <= MAX_RETRIES:
            raise ValueError(f"liczba ponowień musi być w zakresie 0-{MAX_RETRIES}")
        if not _is_int(self.delay_ms) or not 0 <= self.delay_ms <= MAX_DELAY_MS:
            raise ValueError(f"przerwa między ramkami musi być w zakresie 0-{MAX_DELAY_MS} ms")

    @staticmethod
    def from_dict(d):
        """Słownik (np. z config.json / API) -> TransportConfig. Rzuca ValueError z polskim opisem.

        Puste pola liczbowe = wartości domyślne; nieznane klucze są pomijane.
        """
        if isinstance(d, TransportConfig):
            return d
        if not isinstance(d, dict):
            raise ValueError("konfiguracja magistrali musi być obiektem")
        defaults = {f.name: f.default for f in dataclasses.fields(TransportConfig)}
        kind = str(d.get("kind") or defaults["kind"]).strip().lower()
        kind = _KIND_ALIASES.get(kind, kind)
        if kind not in KINDS:
            raise ValueError(f"rodzaj połączenia musi być jednym z: {', '.join(KINDS)}")
        net = kind in NET_KINDS
        host = d.get("host")
        host = defaults["host"] if host is None else str(host).strip()
        port, hport = d.get("port"), ""
        if host.startswith("[") and "]" in host:  # [IPv6] albo [IPv6]:port
            host, _, rest = host[1:].partition("]")
            hport = rest[1:] if rest.startswith(":") else ""
        elif host.count(":") == 1:  # host:port w jednym polu
            host, _, hport = host.partition(":")
        if hport:
            if port not in (None, "") and str(port).strip() != hport:
                raise ValueError("podaj port w osobnym polu, nie w adresie hosta")
            port = hport
        out = {"kind": kind, "host": host, "serial_port": str(d.get("serial_port") or "").strip()}
        for name, conv, msg, relevant in (
                ("port", _as_int, "port TCP musi być liczbą 1-65535", net),
                ("baudrate", _as_int, "prędkość musi być liczbą całkowitą", not net),
                ("parity", _as_parity, "parzystość musi być N, E lub O", not net),
                ("stopbits", _as_int, "bity stopu: 1 lub 2", not net),
                ("bytesize", _as_int, "bity danych: 7 lub 8", not net),
                ("timeout", _as_float, "timeout musi być liczbą (sekundy)", True),
                ("retries", _as_int, "liczba ponowień musi być liczbą całkowitą", True),
                ("delay_ms", _as_int, "przerwa między ramkami musi być liczbą całkowitą (ms)", True)):
            raw = port if name == "port" else d.get(name)
            if raw is None or (isinstance(raw, str) and not raw.strip()):
                out[name] = defaults[name]
                continue
            try:
                out[name] = conv(raw, msg)
            except ValueError:
                if relevant:
                    raise
                out[name] = defaults[name]  # pole bez znaczenia dla tego rodzaju łącza
        return TransportConfig(**out)

    def to_dict(self):
        return dataclasses.asdict(self)

    @property
    def is_serial(self):
        return self.kind in SERIAL_KINDS

    def _hostport(self):
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{host}:{self.port}"

    def key(self):
        """Tożsamość fizycznego łącza: ten sam port szeregowy dla RTU i ASCII to jedna magistrala."""
        if self.is_serial:
            return f"serial:{self.serial_port}"
        return f"{self.kind}:{self._hostport()}"

    def describe(self):
        if self.is_serial:
            mode = "RS-485" if self.kind == "rtu" else "RS-485 ASCII"
            return f"{mode} {self.serial_port} {self.baudrate} {self.bytesize}{self.parity}{self.stopbits}"
        name = {"tcp": "TCP", "rtu_over_tcp": "RTU over TCP", "udp": "UDP"}[self.kind]
        return f"{name} {self._hostport()}"

    def link(self):
        """Parametry, których zmiana wymaga ponownego otwarcia łącza (reszta zmienia się w locie)."""
        if self.is_serial:
            return (self.kind, self.serial_port, self.baudrate, self.parity, self.stopbits, self.bytesize)
        return (self.kind, self.host, self.port)


# ── zgodność z wersjami pymodbus ──────────────────────────────
def _quiet_pymodbus():
    """pymodbus loguje ERROR przy każdym timeoucie - błędy i tak raportujemy sami."""
    lg = logging.getLogger("pymodbus")
    if lg.level == logging.NOTSET:
        level = os.environ.get("MODBUS_DASH_PYMODBUS_LOG", "").strip().upper()
        lg.setLevel(level if level in ("DEBUG", "INFO", "WARNING", "ERROR") else logging.CRITICAL)


def _framer_values():
    for mod, name in (("pymodbus", "FramerType"), ("pymodbus.framer", "FramerType"),
                      ("pymodbus", "Framer"), ("pymodbus.framer", "Framer")):
        try:
            enum = getattr(importlib.import_module(mod), name)
        except (ImportError, AttributeError):
            continue
        return "enum", {"socket": enum.SOCKET, "rtu": enum.RTU, "ascii": enum.ASCII}
    try:  # bardzo stare API: klasy framerów
        from pymodbus.framer import ModbusAsciiFramer, ModbusRtuFramer, ModbusSocketFramer
        return "class", {"socket": ModbusSocketFramer, "rtu": ModbusRtuFramer, "ascii": ModbusAsciiFramer}
    except ImportError:
        return "str", {"socket": "socket", "rtu": "rtu", "ascii": "ascii"}


@functools.lru_cache(maxsize=None)
def _pm():
    """Wykryte API pymodbus (raz na proces)."""
    try:
        import pymodbus
        from pymodbus import client as pmc
        from pymodbus.exceptions import ConnectionException, ModbusException
    except ImportError as e:
        raise ModbusError("connection", f"brak biblioteki pymodbus ({e}) - pip install pymodbus") from None
    _quiet_pymodbus()
    params = inspect.signature(pmc.ModbusTcpClient.read_holding_registers).parameters
    unit_kw = next((k for k in ("device_id", "slave", "unit") if k in params), "slave")
    try:
        from pymodbus.transaction import TransactionManager
        resends = hasattr(TransactionManager, "sync_execute")
    except ImportError:
        resends = False
    framer_api, framers = _framer_values()
    return dict(
        version=str(getattr(pymodbus, "__version__", "?")),
        tcp=pmc.ModbusTcpClient, udp=getattr(pmc, "ModbusUdpClient", None),
        serial=getattr(pmc, "ModbusSerialClient", None),
        ConnectionException=ConnectionException, ModbusException=ModbusException,
        unit_kw=unit_kw, framer_api=framer_api, framers=framers, bits_reversed=_bit_bytes_reversed(),
        # nowy menedżer ponawia wysyłkę (mnożąc timeout) - ponowienia robimy sami
        retries=0 if resends else 1,
    )


def _bit_bytes_reversed():
    """pymodbus 3.10.0 składa bajty odpowiedzi FC01/FC02 od końca - sprawdzamy na próbnej ramce."""
    for mod in ("pymodbus.pdu.bit_message", "pymodbus.pdu.bit_read_message", "pymodbus.bit_read_message"):
        try:
            resp = importlib.import_module(mod).ReadCoilsResponse()
            resp.decode(b"\x02\x01\x00")
            return not resp.bits[0] and bool(resp.bits[8])
        except Exception:  # noqa: BLE001 - inna wersja API, próbujemy dalej
            continue
    return False


def _client_kwargs(cls, kw):
    """Tylko argumenty znane danej wersji klienta (3.6 przyjmuje wszystko przez **kwargs)."""
    params = inspect.signature(cls.__init__).parameters
    if any(p.kind is p.VAR_KEYWORD for p in params.values()):
        return kw
    return {k: v for k, v in kw.items() if k in params}


def pymodbus_version():
    try:
        import pymodbus
    except ImportError:
        return "brak"
    return str(getattr(pymodbus, "__version__", "?"))


def pymodbus_info():
    """Wykryte API pymodbus (diagnostyka): wersja, argument Unit ID, framery, retries klienta."""
    pm = _pm()
    return {"version": pm["version"], "unit_kw": pm["unit_kw"], "framer_api": pm["framer_api"],
            "client_retries": pm["retries"], "serial": pm["serial"] is not None, "udp": pm["udp"] is not None}


# ── magistrala ────────────────────────────────────────────────
_REG_FC = {"input": 4, "holding": 3}
_BIT_FC = {"coil": 1, "discrete": 2}
_READ_NAMES = {4: "read_input_registers", 3: "read_holding_registers",
               1: "read_coils", 2: "read_discrete_inputs"}


def _mismatch(message):
    """Odpowiedź nie pasuje do zapytania - zwykle spóźniona odpowiedź na wcześniejsze
    zapytanie (RTU nie ma numerów transakcji). Takie błędy dostają dodatkowe próby."""
    err = ModbusError("io", message)
    err.mismatch = True
    return err


def _check_response(rr, fc):
    """Odpowiedź pymodbus -> ta sama odpowiedź albo wyjątek (ModbusError lub błąd pymodbus)."""
    if isinstance(rr, BaseException):
        raise rr  # 3.6/3.7 zwracają ModbusIOException zamiast go rzucić
    if rr is None:
        raise ModbusError("io", "Błąd transmisji: brak odpowiedzi z biblioteki pymodbus")
    if rr.isError():
        code = getattr(rr, "exception_code", None)
        if code:
            raise exception_error(int(code))
        raise ModbusError("io", f"Błąd transmisji: {rr}")
    got = getattr(rr, "function_code", fc)
    if isinstance(got, int) and got != fc:
        # np. spóźniona odpowiedź na wcześniejsze zapytanie (RTU nie ma numerów transakcji)
        raise _mismatch(f"Błąd transmisji: odpowiedź FC{got:02d} na zapytanie FC{fc:02d}")
    return rr


class Bus:
    """Jedna fizyczna magistrala/połączenie współdzielone przez wszystkich.

    Bezpieczna wątkowo: jedno zapytanie naraz (RLock). Połączenie otwierane
    leniwie i utrzymywane między zapytaniami; po błędzie łącza zamykane, a
    kolejne zapytanie łączy się ponownie.
    """

    def __init__(self, cfg, _previous=None):
        if isinstance(cfg, dict):
            cfg = TransportConfig.from_dict(cfg)
        self.cfg = cfg
        self.lock = threading.RLock()
        self._client = None
        self._previous = _previous    # poprzednia instancja dla tego łącza (musi zwolnić port)
        self._retired = False
        self._interrupted = False     # close() z innego wątku przerwał trwające zapytanie
        self._dirty = False           # po timeoucie na RS-485: wyczyść wejście przed kolejną ramką
        self._spare_port = None       # otwarty port dla nowego klienta (po błędzie na RS-485)
        self._applied = None          # (timeout, id gniazda) ustawione w kliencie
        self._last_io = 0.0           # monotonic: koniec ostatniej ramki
        self._used = time.monotonic()
        self._link_ok = None
        self._local = threading.local()
        self._stats_lock = threading.Lock()
        self._st = {"requests": 0, "errors": 0, "timeouts": 0, "exceptions": 0, "retries": 0,
                    "connects": 0, "last_error": None, "last_error_ts": None, "last_ok_ts": None,
                    "avg_ms": None}

    def __repr__(self):
        return f"<Bus {self.cfg.describe()}>"

    # ── API ───────────────────────────────────────────────────
    def read_registers(self, unit, function, address, count):
        """FC04 (input) / FC03 (holding) -> lista uint16."""
        fc = _REG_FC.get(function)
        if fc is None:
            raise InvalidRequest(f"nieznany typ rejestrów: {function!r} (input/holding)")
        self._validate(unit, address, count, MAX_REGS, "rejestrów")

        def op(client, kw):
            rr = _check_response(getattr(client, _READ_NAMES[fc])(address, count=count, **kw), fc)
            regs = list(getattr(rr, "registers", None) or [])
            if len(regs) != count:
                raise _mismatch(f"Błąd transmisji: odpowiedź ma {len(regs)} rejestrów zamiast {count}")
            return [int(r) & 0xFFFF for r in regs]
        return self._execute(unit, op)

    def read_bits(self, unit, function, address, count):
        """FC01 (coil) / FC02 (discrete) -> lista bool."""
        fc = _BIT_FC.get(function)
        if fc is None:
            raise InvalidRequest(f"nieznany typ bitów: {function!r} (coil/discrete)")
        self._validate(unit, address, count, MAX_BITS, "bitów")

        def op(client, kw):
            rr = _check_response(getattr(client, _READ_NAMES[fc])(address, count=count, **kw), fc)
            bits = list(getattr(rr, "bits", None) or [])
            if _pm()["bits_reversed"]:
                bits = [b for i in range(len(bits) - 8, -1, -8) for b in bits[i:i + 8]]
            if not count <= len(bits) < count + 8:  # bity przychodzą pełnymi bajtami
                raise _mismatch(f"Błąd transmisji: odpowiedź ma {len(bits)} bitów zamiast {count}")
            return [bool(b) for b in bits[:count]]
        return self._execute(unit, op)

    def write_register(self, unit, address, value):
        """FC06. value: 0..65535 (ujemne -32768..-1 zapisywane jako int16)."""
        self._validate(unit, address, 1, 1, "rejestrów")
        if not _is_int(value) or not -0x8000 <= value <= 0xFFFF:
            raise InvalidRequest("wartość rejestru musi być liczbą 0-65535 (lub -32768..-1)")
        self._execute(unit, lambda client, kw: _check_response(
            client.write_register(address, value & 0xFFFF, **kw), 6))

    def write_registers(self, unit, address, values):
        """FC16, 1..123 rejestrów."""
        values = list(values) if isinstance(values, (list, tuple)) else values
        if not isinstance(values, list) or not 1 <= len(values) <= MAX_WRITE_REGS:
            raise InvalidRequest(f"zapis wielu rejestrów: od 1 do {MAX_WRITE_REGS} wartości")
        if any(not _is_int(v) or not -0x8000 <= v <= 0xFFFF for v in values):
            raise InvalidRequest("wartości rejestrów muszą być liczbami 0-65535 (lub -32768..-1)")
        self._validate(unit, address, len(values), MAX_WRITE_REGS, "rejestrów")
        regs = [v & 0xFFFF for v in values]
        self._execute(unit, lambda client, kw: _check_response(client.write_registers(address, regs, **kw), 16))

    def write_coil(self, unit, address, value):
        """FC05."""
        self._validate(unit, address, 1, 1, "bitów")
        self._execute(unit, lambda client, kw: _check_response(client.write_coil(address, bool(value), **kw), 5))

    def ping(self, unit=None):
        """Test łącza (unit=None) albo urządzenia (odczyt 1 rejestru holding; wyjątek Modbus = żyje).

        Zwraca {"ok", "error", "kind", "ms"}; nigdy nie rzuca.
        """
        t0 = time.monotonic()
        try:
            if unit in (None, ""):
                timeout, _ = self._settings()
                with self.lock:
                    self._check_open()
                    self._connect(timeout)
            else:
                try:
                    unit = _as_int(unit, "Unit ID musi być liczbą")
                except ValueError as e:
                    raise InvalidRequest(str(e)) from None
                try:
                    self.read_registers(unit, "holding", 0, 1)
                except ModbusError as e:
                    if e.kind != "exception":
                        raise
        except ModbusError as e:
            return {"ok": False, "error": str(e), "kind": e.kind, "ms": _ms(t0)}
        except Exception as e:  # noqa: BLE001 - ping nigdy nie rzuca
            return {"ok": False, "error": f"Błąd: {e}", "kind": "io", "ms": _ms(t0)}
        return {"ok": True, "error": None, "kind": None, "ms": _ms(t0)}

    @contextlib.contextmanager
    def override(self, timeout=None, retries=None):
        """Tymczasowo inny timeout / liczba ponowień - tylko dla zapytań z bieżącego wątku."""
        prev = getattr(self._local, "override", None)
        ov = dict(prev or {})
        if timeout is not None:
            ov["timeout"] = min(max(float(timeout), TIMEOUT_RANGE[0]), TIMEOUT_RANGE[1])
        if retries is not None:
            ov["retries"] = min(max(int(retries), 0), MAX_RETRIES)
        self._local.override = ov
        try:
            yield self
        finally:
            self._local.override = prev

    def close(self):
        """Zamyka połączenie (kolejne zapytanie otworzy je ponownie).

        Czeka na trwające zapytanie najwyżej CLOSE_WAIT s, potem zamyka siłowo.
        """
        got = self.lock.acquire(timeout=CLOSE_WAIT)
        try:
            if not got:
                self._interrupted = True  # trwające zapytanie kończy się błędem, bez ponowień
            self._close_client()
        finally:
            if got:
                self.lock.release()

    def stats(self):
        with self._stats_lock:
            st = dict(self._st)
        st["avg_ms"] = round(st["avg_ms"], 1) if st["avg_ms"] is not None else None
        st["connected"] = self.connected
        return st

    @property
    def connected(self):
        return self._spare_port is not None or self._is_open(self._client)

    # ── wewnętrzne ────────────────────────────────────────────
    def _settings(self):
        ov = getattr(self._local, "override", None) or {}
        return ov.get("timeout", self.cfg.timeout), ov.get("retries", self.cfg.retries)

    def _validate(self, unit, address, count, limit, what):
        # Unit 0 to broadcast w RTU (bez odpowiedzi); w TCP zwykły adres, 255 też bywa używany
        lo, hi = (0, 255) if self.cfg.kind in ("tcp", "udp") else (1, 247)
        if not _is_int(unit) or not lo <= unit <= hi:
            raise InvalidRequest(f"Unit ID musi być w zakresie {lo}-{hi}")
        if not _is_int(count) or not 1 <= count <= limit:
            raise InvalidRequest(f"liczba {what} musi być w zakresie 1-{limit}")
        if not _is_int(address) or address < 0 or address + count > 65536:
            raise InvalidRequest("adres poza zakresem 0-65535")

    def _check_open(self):
        if self._retired:
            raise ModbusError("connection", "Magistrala zamknięta (zmieniono konfigurację) - spróbuj ponownie")

    def _execute(self, unit, op):
        """op(client, unit_kwargs) z ponowieniami; zwraca wynik op albo rzuca ModbusError."""
        timeout, retries = self._settings()
        kw = {_pm()["unit_kw"]: unit}
        with self.lock:
            self._check_open()
            self._interrupted = False
            t_start = self._used = time.monotonic()
            attempt, stale_retry, resyncs = 0, True, 0
            while True:
                reused = self._is_open(self._client)
                t0 = time.monotonic()
                try:
                    client = self._connect(timeout)
                    self._pace(client)
                    t0 = time.monotonic()
                    result = op(client, kw)
                except Exception as e:  # noqa: BLE001 - każdy błąd pymodbus/pyserial -> ModbusError
                    err = e if isinstance(e, ModbusError) else self._classify(e, time.monotonic() - t0,
                                                                              timeout, unit)
                else:
                    self._last_io = time.monotonic()
                    self._record(t_start, None, attempt)
                    return result
                self._last_io = time.monotonic()
                if self._retired or self._interrupted:  # zamknięte z innego wątku - bez ponowień
                    err = ModbusError("connection", "Połączenie zamknięte w trakcie zapytania")
                    self._close_client()
                    self._record(t_start, err, attempt)
                    raise err
                self._recover(err)
                transient = err.code is None and err.kind in ("timeout", "io", "connection") \
                    and not err.connect_failed
                if transient and reused and stale_retry and err.kind != "timeout" \
                        and self.cfg.kind in ("tcp", "rtu_over_tcp"):
                    stale_retry = False  # bezczynne połączenie zerwane przez serwer - raz jeszcze, bez liczenia
                    continue
                if getattr(err, "mismatch", False) and resyncs < 2:
                    # spóźniona odpowiedź odczytana jako bieżąca - po wyczyszczeniu wejścia
                    # ponawiamy bez zużywania limitu ponowień
                    resyncs += 1
                    continue
                if (transient or err.code == 0x0B) and attempt < retries:
                    attempt += 1
                    continue
                self._record(t_start, err, attempt)
                raise err

    def _classify(self, e, elapsed, timeout, unit):
        pm = _pm()
        detail = f"{type(e).__name__}: {e}"
        log.debug("%s: %s", self.cfg.describe(), detail)
        if isinstance(e, pm["ConnectionException"]):
            err = ModbusError("connection", f"Połączenie przerwane ({self.cfg.describe()})")
        elif isinstance(e, TimeoutError) or (isinstance(e, pm["ModbusException"]) and elapsed >= 0.75 * timeout):
            # pymodbus zgłasza brak odpowiedzi jako ModbusIOException - rozpoznajemy po czasie
            err = ModbusError("timeout", f"Brak odpowiedzi od Unit ID {unit} (timeout {timeout:g} s)")
        elif isinstance(e, OSError):
            what = f"port {self.cfg.serial_port}" if self.cfg.is_serial else self.cfg.describe()
            err = ModbusError("connection", f"Błąd łącza ({what}): {getattr(e, 'strerror', None) or e}")
        else:
            err = ModbusError("io", "Błąd transmisji: niepoprawna lub niepełna odpowiedź")
        err.detail = detail
        return err

    def _recover(self, err):
        if err.code is not None:
            return  # urządzenie odpowiedziało wyjątkiem - łącze sprawne
        if self.cfg.is_serial and err.kind != "connection":
            # RS-485: port zostaje otwarty, ale klient pymodbus jest nowy - starsze wersje
            # trzymają resztki ramki w buforze; przed kolejną ramką cisza i czyszczenie wejścia
            self._dirty = True
            port = getattr(self._client, "socket", None)
            if port is not None and getattr(port, "is_open", False):
                self._client, self._spare_port = None, port
                return
        # TCP/UDP: nowe połączenie gubi spóźnione odpowiedzi i martwe sesje
        self._close_client()

    def _pace(self, client):
        """Przerwa między ramkami (delay_ms) i czyszczenie wejścia po błędzie na RS-485."""
        gap = self.cfg.delay_ms / 1000.0
        if self._dirty:
            gap = max(gap, RECOVERY_GAP)
        wait = self._last_io + gap - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        if self._dirty:
            self._dirty = False
            sock = getattr(client, "socket", None)
            flush = getattr(sock, "reset_input_buffer", None) or getattr(sock, "flushInput", None)
            if flush is not None:
                with contextlib.suppress(Exception):
                    flush()

    @staticmethod
    def _is_open(client):
        if client is None:
            return False
        if hasattr(client, "socket"):
            return client.socket is not None
        return bool(getattr(client, "connected", False))

    def _connect(self, timeout):
        prev, self._previous = self._previous, None
        if prev is not None:
            prev._retire()  # poprzednia instancja musi zwolnić port przed otwarciem nowego
        client = self._client
        if client is None:
            client = self._client = self._make_client(timeout)
            port, self._spare_port = self._spare_port, None
            if port is not None and hasattr(client, "socket"):
                client.socket = port
        if not self._is_open(client):
            self._applied = None
            try:
                if hasattr(client, "socket") and self.cfg.kind != "udp":
                    client.socket = self._open_serial(timeout) if self.cfg.is_serial else self._open_tcp(timeout)
                if not client.connect():
                    raise _conn_error(f"Nie można połączyć: {self.cfg.describe()}")
            except ModbusError as e:
                self._close_client()
                self._link_failed(e)
                raise
            with self._stats_lock:
                self._st["connects"] += 1
            if self._link_ok is not True:
                log.info("Połączono: %s", self.cfg.describe())
            self._link_ok = True
        self._apply_timeout(client, timeout)
        return client

    def _link_failed(self, e):
        if self._link_ok is not False:
            log.warning("%s", e)
        self._link_ok = False

    def _make_client(self, timeout):
        pm, c = _pm(), self.cfg
        common = {"timeout": timeout, "retries": pm["retries"], "retry_on_empty": False}
        try:
            if c.kind in ("tcp", "rtu_over_tcp"):
                framer = pm["framers"]["rtu" if c.kind == "rtu_over_tcp" else "socket"]
                cls, args = pm["tcp"], (c.host,)
                kw = {"port": c.port, "framer": framer, **common}
            elif c.kind == "udp":
                cls, args = pm["udp"], (c.host,)
                kw = {"port": c.port, "framer": pm["framers"]["socket"], **common}
            else:
                cls, args = pm["serial"], (c.serial_port,)
                kw = {"framer": pm["framers"][c.kind], "baudrate": c.baudrate, "bytesize": c.bytesize,
                      "parity": c.parity, "stopbits": c.stopbits, **common}
            if cls is None:
                raise RuntimeError("ta wersja pymodbus nie obsługuje tego rodzaju połączenia")
            return cls(*args, **_client_kwargs(cls, kw))
        except Exception as e:  # noqa: BLE001
            raise _conn_error(f"Nie można utworzyć klienta pymodbus ({c.describe()}): {e}") from None

    def _open_tcp(self, timeout):
        c = self.cfg
        addr = c._hostport()
        try:
            sock = socket.create_connection((c.host, c.port), timeout=timeout)
        except socket.gaierror:
            raise _conn_error(f"Brak połączenia z {addr}: nieznany host") from None
        except ConnectionRefusedError:
            raise _conn_error(f"Brak połączenia z {addr}: połączenie odrzucone") from None
        except (socket.timeout, TimeoutError):
            raise _conn_error(f"Brak połączenia z {addr}: przekroczono czas ({timeout:g} s)") from None
        except OSError as e:
            reason = {errno.EHOSTUNREACH: "host nieosiągalny", errno.ENETUNREACH: "sieć nieosiągalna"}.get(
                e.errno, e.strerror or str(e))
            raise _conn_error(f"Brak połączenia z {addr}: {reason}") from None
        with contextlib.suppress(OSError, AttributeError):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)  # małe ramki - bez opóźnień Nagle'a
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            for opt, val in (("TCP_KEEPIDLE", 30), ("TCP_KEEPINTVL", 10), ("TCP_KEEPCNT", 3)):
                if hasattr(socket, opt):
                    sock.setsockopt(socket.IPPROTO_TCP, getattr(socket, opt), val)
        return sock

    def _open_serial(self, timeout):
        c, port = self.cfg, self.cfg.serial_port
        try:
            import serial
        except ImportError:
            raise _conn_error("brak biblioteki pyserial - pip install pyserial") from None
        if port.startswith("/") and not os.path.exists(port):
            raise _conn_error(f"Port {port} nie istnieje (sprawdź podłączenie adaptera)")
        try:
            ser = serial.serial_for_url(port, baudrate=c.baudrate, bytesize=c.bytesize, parity=c.parity,
                                        stopbits=c.stopbits, timeout=timeout, exclusive=True)
        except Exception as e:  # noqa: BLE001 - pyserial rzuca różne wyjątki (także termios)
            code = getattr(e, "errno", None)
            if code in (errno.EACCES, errno.EPERM) or isinstance(e, PermissionError):
                msg = f"Brak uprawnień do portu {port} (dodaj użytkownika do grupy dialout)"
            elif code in (errno.EBUSY, errno.EAGAIN, errno.EWOULDBLOCK) or "lock" in str(e).lower():
                msg = f"Port {port} jest zajęty przez inny program"
            elif code in (errno.ENOENT, errno.ENODEV, errno.ENXIO):
                msg = f"Port {port} nie istnieje (sprawdź podłączenie adaptera)"
            else:
                msg = f"Nie można otworzyć portu {port}: {e}"
            raise _conn_error(msg) from None
        # bez limitu przerw między bajtami: adaptery USB oddają ramki kawałkami
        with contextlib.suppress(Exception):
            ser.inter_byte_timeout = None
        return ser

    def _frame_time(self):
        """Czas najdłuższej ramki + zapas na opóźnienia adapterów USB."""
        c = self.cfg
        bits = 1 + c.bytesize + (c.parity != "N") + c.stopbits
        return (513 if c.kind == "ascii" else 256) * bits / c.baudrate + 0.1

    def _apply_timeout(self, client, timeout):
        sock = getattr(client, "socket", None)
        key = (timeout, id(sock))
        if key == self._applied:
            return
        params = getattr(client, "comm_params", None)
        if params is not None and hasattr(params, "timeout_connect"):
            params.timeout_connect = timeout
        if sock is not None:
            with contextlib.suppress(Exception):
                if isinstance(sock, socket.socket):
                    sock.settimeout(timeout)
                elif hasattr(sock, "timeout"):
                    # pymodbus najpierw czeka na dane (timeout), potem czyta resztę ramki z limitem
                    # portu - bez tego starsze wersje czekają na ciszę 2x timeout
                    t = min(timeout, self._frame_time())
                    if sock.timeout != t:
                        sock.timeout = t
        self._applied = key

    def _close_client(self):
        client, self._client = self._client, None
        port, self._spare_port = self._spare_port, None
        self._applied = None
        if port is not None:
            with contextlib.suppress(Exception):
                port.close()
        if client is None:
            return
        sock = getattr(client, "socket", None)
        if isinstance(sock, socket.socket):
            with contextlib.suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)  # budzi wątek czekający na odpowiedź
        with contextlib.suppress(Exception):
            client.close()
        if sock is not None:
            with contextlib.suppress(Exception):
                sock.close()  # niektóre wersje (UDP) tylko zapominają gniazdo

    def _retire(self):
        self._retired = True
        self.close()

    def _close_if_idle(self, max_idle):
        """Zamyka połączenie po max_idle s bezczynności. True = magistrala bezczynna i zamknięta."""
        if time.monotonic() - self._used < max_idle or not self.lock.acquire(blocking=False):
            return False
        try:
            self._close_client()
            return True
        finally:
            self.lock.release()

    def _record(self, t_start, err, attempts):
        ms = (time.monotonic() - t_start) * 1000.0
        with self._stats_lock:
            st = self._st
            st["requests"] += 1
            st["retries"] += attempts
            if err is not None and err.kind == "exception":
                st["exceptions"] += 1
            elif err is not None:
                st["errors"] += 1
                st["timeouts"] += err.kind == "timeout"
                st["last_error"], st["last_error_ts"] = str(err), time.time()
                return
            else:
                st["last_ok_ts"] = time.time()
            st["avg_ms"] = ms if st["avg_ms"] is None else st["avg_ms"] * 0.8 + ms * 0.2


def _ms(t0):
    return round((time.monotonic() - t0) * 1000.0, 1)


class BusManager:
    """Rejestr magistral: ten sam klucz łącza -> ta sama instancja Bus."""

    def __init__(self):
        self._buses = {}
        self._lock = threading.Lock()

    def get(self, cfg):
        """Bus dla konfiguracji. Zmiana timeout/retries/delay_ms - w locie; zmiana parametrów
        łącza (np. prędkości portu) - stara instancja jest zamykana i zastępowana."""
        if isinstance(cfg, dict):
            cfg = TransportConfig.from_dict(cfg)
        key = cfg.key()
        old = None
        with self._lock:
            self._reap(key)
            bus = self._buses.get(key)
            if bus is not None and bus.cfg != cfg:
                if bus.cfg.link() == cfg.link():
                    bus.cfg = cfg
                else:
                    old, bus = bus, None
            if bus is None:
                bus = self._buses[key] = Bus(cfg, _previous=old)
        if old is not None:
            old._retire()  # poza blokadą menedżera - może czekać na trwające zapytanie
        return bus

    def _reap(self, keep):
        """Zamyka długo nieużywane połączenia i zapomina porzucone magistrale."""
        for key, bus in list(self._buses.items()):
            if key != keep and bus._close_if_idle(IDLE_CLOSE) and time.monotonic() - bus._used > IDLE_DROP:
                del self._buses[key]
                bus._retired = True

    def close_all(self):
        with self._lock:
            buses = list(self._buses.values())
            self._buses.clear()
        for bus in buses:
            bus._retire()

    def snapshot(self):
        with self._lock:
            buses = sorted(self._buses.items())
        return [{"key": k, "describe": b.cfg.describe(), "stats": b.stats()} for k, b in buses]


# ── porty szeregowe ───────────────────────────────────────────
def _clean(text):
    text = str(text or "").strip()
    return "" if text.lower() == "n/a" else text


def list_serial_ports():
    """Porty szeregowe: aliasy Raspberry Pi, stałe nazwy USB (/dev/serial/by-id) i porty z pyserial."""
    ports, seen = [], set()

    def add(device, description="", hwid=""):
        if device not in seen:
            seen.add(device)
            ports.append({"device": device, "description": description, "hwid": hwid})

    if os.name == "posix":
        for alias, desc in (("/dev/serial0", "UART Raspberry Pi (GPIO 14/15)"),
                            ("/dev/serial1", "UART Raspberry Pi (drugi)")):
            if os.path.exists(alias):
                real = os.path.realpath(alias)
                add(alias, f"{desc} -> {os.path.basename(real)}")
                add(real, f"{desc} (bezpośrednio)")
        with contextlib.suppress(OSError):
            for name in sorted(os.listdir("/dev")):
                if name.startswith("ttyAMA"):
                    add(f"/dev/{name}", "UART PL011")
        by_id = "/dev/serial/by-id"
        with contextlib.suppress(OSError):
            for name in sorted(os.listdir(by_id)):
                path = os.path.join(by_id, name)
                add(path, f"USB, stała nazwa -> {os.path.basename(os.path.realpath(path))}")
    try:
        from serial.tools import list_ports
        found = sorted(list_ports.comports(), key=lambda p: p.device)
    except Exception:  # noqa: BLE001 - brak pyserial albo błąd wyliczania
        found = []
    for p in found:
        add(p.device, _clean(p.description), _clean(p.hwid))
    return ports
