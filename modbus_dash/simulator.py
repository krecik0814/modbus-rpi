"""Symulator licznika energii: model fizyczny + własny serwer Modbus.

Serwer nie korzysta z pymodbus (API serwera zmieniało się niekompatybilnie),
obsługuje trzy warianty ramek:

    tcp     Modbus TCP (nagłówek MBAP)
    rtu     ramki RTU z CRC16 przesyłane po TCP (jak bramki "RTU over TCP")
    serial  ramki RTU na porcie szeregowym (pyserial) - SerialSimServer;
            para pty (make_pty) albo wirtualna para COM pozwala testować
            klienta RTU bez sprzętu

Rejestry urządzeń (SimDevice) wypełniane są z presetu: każdy rejestr presetu
dostaje wartość wielkości kanonicznej (quantities.py) zakodowaną typem,
kolejnością bajtów i skalą z presetu.
"""

import argparse
import cmath
import json
import logging
import math
import os
import random
import select
import socket
import socketserver
import struct
import threading
import time
from pathlib import Path

from . import __version__, codec
from .presets import normalize_preset
from .quantities import QUANTITIES, normalize_unit

log = logging.getLogger("modbus-dash.sim")

MAX_READ_REGS = 125
MAX_READ_BITS = 2000
MAX_WRITE_REGS = 123
MAX_WRITE_BITS = 1968
WRITE_FCS = (5, 6, 15, 16)

# Modbus - kody wyjątków
ILLEGAL_FUNCTION = 1
ILLEGAL_ADDRESS = 2
ILLEGAL_VALUE = 3
GATEWAY_NO_RESPONSE = 0x0B


# ── CRC16 (Modbus RTU, wielomian 0xA001) ──────────────────────
def _crc_table():
    table = []
    for i in range(256):
        c = i
        for _ in range(8):
            c = (c >> 1) ^ 0xA001 if c & 1 else c >> 1
        table.append(c)
    return table


_CRC_TABLE = _crc_table()


def crc16(data):
    crc = 0xFFFF
    for b in data:
        crc = (crc >> 8) ^ _CRC_TABLE[(crc ^ b) & 0xFF]
    return crc


def add_crc(frame):
    """Dokleja CRC (młodszy bajt pierwszy) do ramki RTU."""
    frame = bytes(frame)
    return frame + crc16(frame).to_bytes(2, "little")


def check_crc(frame):
    return len(frame) >= 4 and crc16(frame[:-2]) == int.from_bytes(frame[-2:], "little")


def rtu_request_length(buf):
    """Długość ramki zapytania RTU na podstawie kodu funkcji.

    None - za mało bajtów, by ją ustalić; 0 - nieznana funkcja (pozostaje
    wykrywanie po przerwie w transmisji).
    """
    if len(buf) < 2:
        return None
    fc = buf[1]
    if fc in (1, 2, 3, 4, 5, 6, 8):
        return 8
    if fc in (15, 16):
        return 9 + buf[6] if len(buf) >= 7 else None
    if fc == 43:
        return 7
    if fc in (7, 11, 12, 17):
        return 4
    if fc == 22:
        return 10
    if fc == 23:
        return 13 + buf[10] if len(buf) >= 11 else None
    return 0


# ── model fizyczny ────────────────────────────────────────────
# (prąd bazowy A, amplituda A, okres s, cos φ) - stałe odbiorniki faz
_BASE_LOAD = ((2.5, 0.8, 60.0, 0.95), (1.8, 0.5, 90.0, 0.92), (3.2, 1.0, 45.0, 0.88))
# (napięcie V, amplituda V, okres s)
_BASE_VOLT = ((230.0, 3.0, 120.0), (229.5, 2.5, 150.0), (231.0, 2.0, 180.0))
# włączane losowo odbiorniki: (prąd A, cos φ, min s, max s)
_APPLIANCES = (
    (8.7, 1.0, 30, 120),    # czajnik / grzałka 2 kW
    (4.3, 0.99, 60, 240),   # grzejnik 1 kW
    (2.2, 0.72, 40, 200),   # sprężarka
    (1.1, 0.6, 30, 120),    # pompa
)
_V_ANGLES = (0.0, -120.0, 120.0)


class Physics:
    """Symulowana instalacja 3-fazowa: odbiorniki na L1-L3 i fotowoltaika na L3.

    PV ma "dobę" pv_period sekund (dodatnia połówka sinusa), więc L3 i suma
    okresowo oddają energię (moc i cos φ ujemne). Energia całkowita liczona
    z bilansowaniem międzyfazowym (jak w polskich licznikach prosumenckich),
    energie fazowe - osobno dla każdej fazy. Taryfa T2 trwa ostatnią 1/3
    każdego okresu tariff_period.
    """

    pv_peak = 3200.0        # W, szczyt PV (L3)
    pv_period = 300.0       # s
    demand_period = 900.0   # s, stała czasowa mocy szczytowej (demand)
    appliance_rate = 120.0  # s, średni odstęp między włączeniami odbiorników
    tariff_period = 600.0   # s, cykl taryf T1/T2

    def __init__(self, seed=None):
        self.rng = random.Random(seed)
        self.t0 = None
        self.last = None
        self.appliances = [None, None, None]  # (prąd, cos φ, koniec t) albo None
        self.energy = {
            "energy_import": 12345.678, "energy_export": 234.567,
            "energy_import_t1": 9000.0, "energy_import_t2": 3345.678,
            "energy_export_t1": 200.0, "energy_export_t2": 34.567,
            "energy_import_l1": 4400.0, "energy_import_l2": 3900.0, "energy_import_l3": 4600.0,
            "energy_export_l1": 0.0, "energy_export_l2": 0.0, "energy_export_l3": 780.0,
            "energy_reactive_import": 1023.456, "energy_reactive_export": 12.345,
            "energy_apparent": 13500.0,
        }
        self.p_demand = None
        self.p_demand_max = 0.0
        self.i_demand = None
        self.values = {}

    def _appliance(self, k, t, dt):
        cur = self.appliances[k]
        if cur is not None and t >= cur[2]:
            cur = self.appliances[k] = None
        if cur is None and dt > 0 and self.rng.random() < dt / self.appliance_rate:
            amps, pf, lo, hi = self.rng.choice(_APPLIANCES)
            cur = self.appliances[k] = (amps, pf, t + self.rng.uniform(lo, hi))
        return (cur[0], cur[1]) if cur else (0.0, 1.0)

    def tick(self, now=None):
        """Krok symulacji; now - czas w sekundach (domyślnie time.monotonic())."""
        now = time.monotonic() if now is None else float(now)
        if self.t0 is None:
            self.t0 = self.last = now
        dt = min(max(0.0, now - self.last), 3600.0)
        self.last = now
        t = now - self.t0
        g = self.rng.gauss

        def wave(base, amp, period):
            return base + amp * math.sin(2 * math.pi * t / period)

        pv = max(0.0, self.pv_peak * math.sin(2 * math.pi * t / self.pv_period))
        if pv:
            pv *= max(0.0, 1 + g(0, 0.02))

        q = {}
        v_ph, i_ph, P, Q, S, I = [], [], [], [], [], []
        for k, ph in enumerate(("l1", "l2", "l3")):
            ib, amp, period, pfb = _BASE_LOAD[k]
            i_base = max(0.01, wave(ib, amp, period) * (1 + g(0, 0.03)))
            pf_base = min(1.0, max(0.7, pfb + g(0, 0.01)))
            a_i, a_pf = self._appliance(k, t, dt)
            v = wave(*_BASE_VOLT[k]) * (1 + g(0, 0.002)) - 0.12 * (i_base + a_i)
            if k == 2:
                v += pv / 1000 * 1.5  # wzrost napięcia przy oddawaniu energii
            p = v * (i_base * pf_base + a_i * a_pf)
            qr = v * (i_base * math.sin(math.acos(pf_base)) + a_i * math.sin(math.acos(a_pf)))
            if k == 2:
                p -= pv
            s = math.hypot(p, qr)
            i = s / v
            phi = math.degrees(math.atan2(qr, p))
            ang = _V_ANGLES[k] + g(0, 0.2)
            v_ph.append(cmath.rect(v, math.radians(ang)))
            i_ph.append(cmath.rect(i, math.radians(ang - phi)))
            P.append(p)
            Q.append(qr)
            S.append(s)
            I.append(i)
            q[f"voltage_{ph}"] = v
            q[f"current_{ph}"] = i
            q[f"power_{ph}"] = p
            q[f"reactive_{ph}"] = qr
            q[f"apparent_{ph}"] = s
            q[f"pf_{ph}"] = p / s if s else 1.0
            q[f"phase_angle_{ph}"] = phi

        q["voltage_l12"] = abs(v_ph[0] - v_ph[1])
        q["voltage_l23"] = abs(v_ph[1] - v_ph[2])
        q["voltage_l31"] = abs(v_ph[2] - v_ph[0])
        q["voltage_ln_avg"] = sum(abs(x) for x in v_ph) / 3
        q["voltage_ll_avg"] = (q["voltage_l12"] + q["voltage_l23"] + q["voltage_l31"]) / 3
        q["current_n"] = abs(sum(i_ph))
        q["current_total"] = sum(I)
        q["current_avg"] = sum(I) / 3
        p_tot, q_tot, s_tot = sum(P), sum(Q), sum(S)
        q["power_total"] = p_tot
        q["power_import"], q["power_export"] = max(0.0, p_tot), max(0.0, -p_tot)
        q["reactive_total"] = q_tot
        q["apparent_total"] = s_tot
        q["pf_total"] = p_tot / s_tot if s_tot else 1.0
        q["phase_angle_total"] = math.degrees(math.atan2(q_tot, p_tot))
        q["frequency"] = wave(50.0, 0.02, 200.0) + g(0, 0.005)

        # energie [kWh]: W * h / 1000
        e, h = self.energy, dt / 3600.0 / 1000.0
        for k, ph in enumerate(("l1", "l2", "l3")):
            e[f"energy_import_{ph}" if P[k] > 0 else f"energy_export_{ph}"] += abs(P[k]) * h
        tariff = "t2" if t % self.tariff_period >= self.tariff_period * 2 / 3 else "t1"
        for k in ("energy_import", f"energy_import_{tariff}") if p_tot > 0 else \
                 ("energy_export", f"energy_export_{tariff}"):
            e[k] += abs(p_tot) * h
        e["energy_reactive_import" if q_tot >= 0 else "energy_reactive_export"] += abs(q_tot) * h
        e["energy_apparent"] += s_tot * h
        q.update(e)
        for ph in ("l1", "l2", "l3"):
            q[f"energy_total_{ph}"] = e[f"energy_import_{ph}"] + e[f"energy_export_{ph}"]
        for tr in ("t1", "t2"):
            q[f"energy_total_{tr}"] = e[f"energy_import_{tr}"] + e[f"energy_export_{tr}"]
        q["energy_total"] = e["energy_import"] + e["energy_export"]
        q["energy_net"] = e["energy_import"] - e["energy_export"]
        q["energy_reactive_total"] = e["energy_reactive_import"] + e["energy_reactive_export"]

        thd_v = [max(0.0, b + g(0, 0.1)) for b in (2.5, 2.8, 2.3)]
        thd_i = [max(0.0, b + g(0, 0.3)) for b in (8.5, 12.0, 6.5)]
        for k, ph in enumerate(("l1", "l2", "l3")):
            q[f"thd_v_{ph}"] = thd_v[k]
            q[f"thd_i_{ph}"] = thd_i[k]
        q["thd_v_avg"] = sum(thd_v) / 3
        q["thd_i_avg"] = sum(thd_i) / 3

        # moc szczytowa: średnia wykładnicza mocy pobieranej i największego prądu fazowego
        a = 1 - math.exp(-dt / self.demand_period)
        p_imp, i_max = max(0.0, p_tot), max(I)
        self.p_demand = p_imp if self.p_demand is None else self.p_demand + (p_imp - self.p_demand) * a
        self.i_demand = i_max if self.i_demand is None else self.i_demand + (i_max - self.i_demand) * a
        self.p_demand_max = max(self.p_demand_max, self.p_demand)
        q["power_demand"] = self.p_demand
        q["power_demand_max"] = self.p_demand_max
        q["current_demand"] = self.i_demand
        q["temperature"] = wave(27.0, 3.0, 900.0) + 0.05 * sum(I) + g(0, 0.05)
        self.values = q
        return dict(q)


def phase_view(q, phases):
    """Wartości dla licznika 1-fazowego: sumy = wartości L1."""
    if phases != 1:
        return q
    q = dict(q)
    for name in ("power", "apparent", "reactive", "pf", "phase_angle"):
        q[f"{name}_total"] = q[f"{name}_l1"]
    q["current_total"] = q["current_avg"] = q["current_n"] = q["current_l1"]
    q["voltage_ln_avg"] = q["voltage_l1"]
    q["thd_v_avg"], q["thd_i_avg"] = q["thd_v_l1"], q["thd_i_l1"]
    q["energy_import"], q["energy_export"] = q["energy_import_l1"], q["energy_export_l1"]
    q["energy_total"] = q["energy_total_l1"]
    q["energy_net"] = q["energy_import_l1"] - q["energy_export_l1"]
    return q


# ── urządzenie (obrazy rejestrów) ─────────────────────────────
# stare / skrótowe klucze presetów -> klucze kanoniczne
KEY_ALIASES = {
    "current_neutral": "current_n", "neutral_current": "current_n",
    "energy_reactive": "energy_reactive_import",
    "voltage_l1_l2": "voltage_l12", "voltage_l2_l3": "voltage_l23", "voltage_l3_l1": "voltage_l31",
    "freq": "frequency",
    "voltage": "voltage_l1", "current": "current_l1",
    "power": "power_total", "apparent": "apparent_total", "reactive": "reactive_total",
    "pf": "pf_total", "power_factor": "pf_total",
}

# (jednostka kanoniczna, jednostka presetu) -> mnożnik
_UNIT_FACTORS = {
    ("W", "kW"): 1e-3, ("VA", "kVA"): 1e-3, ("var", "kvar"): 1e-3,
    ("kWh", "Wh"): 1e3, ("kWh", "MWh"): 1e-3, ("kvarh", "varh"): 1e3, ("kVAh", "VAh"): 1e3,
    ("A", "mA"): 1e3, ("V", "kV"): 1e-3, ("Hz", "mHz"): 1e3,
}
_UNIT_SPELLING = {u.lower(): u for u in ("W", "kW", "VA", "kVA", "var", "kvar", "Wh", "kWh", "MWh",
                                         "varh", "kvarh", "VAh", "kVAh", "A", "mA", "V", "kV", "Hz")}


def _norm_unit(unit):
    unit = normalize_unit((unit or "").strip())
    return _UNIT_SPELLING.get(unit.lower(), unit)


def resolve_key(key):
    """Klucz rejestru presetu -> klucz kanoniczny albo None."""
    if key in QUANTITIES:
        return key
    k = str(key).lower()
    if k in QUANTITIES:
        return k
    return KEY_ALIASES.get(k)


def unit_factor(qkey, unit):
    """Mnożnik przeliczenia wartości kanonicznej na jednostkę z presetu (np. W -> kW)."""
    canon = QUANTITIES[qkey]["unit"]
    return _UNIT_FACTORS.get((canon, _norm_unit(unit)), 1.0)


def _pack_bits(bits):
    out = bytearray((len(bits) + 7) // 8)
    for i, b in enumerate(bits):
        if b:
            out[i // 8] |= 1 << (i % 8)
    return bytes(out)


def _type_limit(dtype):
    """Największa bezpieczna wartość bezwzględna mantysy dla typu."""
    if dtype.startswith("float"):
        return float("inf")
    count, _ = codec.DATA_TYPES[dtype]
    bits = 8 if dtype in ("int8", "uint8") else count * 16
    return (1 << (bits - 1)) - 1 if not dtype.startswith("u") else (1 << bits) - 1


class _ModbusEx(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


class SimDevice:
    """Urządzenie Modbus o rejestrach opisanych presetem.

    strict=True: odczyt adresu nieopisanego w presecie zwraca wyjątek 02
    (jak wiele liczników), funkcje bitowe - wyjątek 01.
    Rejestry o kluczach spoza słownika wielkości mają stałą wartość 0 i można
    je nadpisać zapisem (jak rejestry konfiguracyjne).
    """

    def __init__(self, unit, preset, strict=False, identity=None):
        if isinstance(unit, bool) or not isinstance(unit, int) or not 0 <= unit <= 255:
            raise ValueError(f"nieprawidłowy adres urządzenia: {unit!r} (0-255)")
        regs = preset.get("registers") if isinstance(preset, dict) else None
        if not isinstance(regs, dict) or any(not isinstance(s, dict) or "count" not in s
                                             for s in regs.values()):
            preset = normalize_preset(preset)  # surowy preset - znormalizuj
        self.unit = unit
        self.preset = preset
        self.strict = bool(strict)
        self.phases = preset.get("phases", 3)
        self.lock = threading.Lock()
        self._regs = {"input": {}, "holding": {}}
        self._bits = {"coil": {}, "discrete": {}}
        self._mapped = {"input": set(), "holding": set()}
        self._sources = []  # (spec, klucz kanoniczny, mnożnik jednostki)
        for key, spec in preset["registers"].items():
            self._mapped[spec["function"]].update(range(spec["address"], spec["address"] + spec["count"]))
            qkey = resolve_key(key)
            if qkey is None:
                self._store(spec, 0)  # stały rejestr, zapisywalny
            else:
                self._sources.append((spec, qkey, unit_factor(qkey, spec.get("unit"))))
        name = preset.get("name") or "Symulator"
        self.identity = {
            0x00: preset.get("manufacturer") or "SimMeter",
            0x01: preset.get("model") or "SIM",
            0x02: __version__,
            0x04: name,
            0x05: preset.get("model") or name,
            0x06: "Modbus Dash symulator",
        }
        if identity:
            self.identity.update(identity)
        self.requests = 0

    def _store(self, spec, raw, img=None):
        try:
            words = codec.encode(raw, spec["type"], spec["order"])
        except (OverflowError, ValueError, struct.error):
            words = codec.encode(0, spec["type"], spec["order"])
        img = self._regs[spec["function"]] if img is None else img[spec["function"]]
        for i, w in enumerate(words):
            img[spec["address"] + i] = w

    def update(self, quantities):
        """Koduje wartości wielkości do rejestrów presetu (atomowo względem odczytów)."""
        q = phase_view(quantities, self.phases)
        new = {"input": {}, "holding": {}}
        values = {}
        for spec, qkey, factor in self._sources:
            v = q.get(qkey)
            values[spec["key"]] = 0.0 if v is None else v * factor
        factors = self._scale_registers(values, new)
        for spec, qkey, factor in self._sources:
            v = values[spec["key"]]
            f = factors.get(spec.get("scale_from"), 1.0) if spec.get("scale_from") else 1.0
            self._store(spec, codec.unscaled(v, spec["scale"] * f, spec["offset"]), new)
        with self.lock:
            self._regs["input"].update(new["input"])
            self._regs["holding"].update(new["holding"])

    def _scale_registers(self, values, new):
        """Rejestry skali ("scale_from"): wykładnik dobrany tak, aby mantysy zależnych
        rejestrów mieściły się w swoim typie; tryb multiply - mnożnik 1. Zwraca {klucz: mnożnik}."""
        regs = self.preset["registers"]
        deps = {}
        for spec in regs.values():
            if spec.get("scale_from"):
                deps.setdefault(spec["scale_from"], []).append(spec)
        factors = {}
        for src, specs in deps.items():
            sspec = regs[src]
            if any(d.get("scale_mode") == "multiply" for d in specs):
                factors[src] = 1.0
                self._store(sspec, codec.unscaled(1, sspec["scale"], sspec["offset"]), new)
                continue
            exp = -6
            for d in specs:
                limit = _type_limit(d["type"])
                v = abs(values.get(d["key"], 0.0) - d["offset"])
                while exp < 9 and v / (abs(d["scale"]) * 10.0 ** exp) > limit:
                    exp += 1
            factors[src] = 10.0 ** exp
            self._store(sspec, codec.unscaled(exp, sspec["scale"], sspec["offset"]), new)
        return factors

    def read(self, function, address, count):
        """Bezpośredni odczyt obrazu rejestrów (bez kontroli strict)."""
        with self.lock:
            img = self._regs[function]
            return [img.get(a, 0) for a in range(address, address + count)]

    def write(self, address, values):
        with self.lock:
            for i, v in enumerate(values):
                self._regs["holding"][address + i] = int(v) & 0xFFFF

    # ── obsługa PDU ────────────────────────────────────────────
    def handle_pdu(self, pdu):
        """Zapytanie PDU (kod funkcji + dane) -> odpowiedź PDU (także wyjątek)."""
        pdu = bytes(pdu)
        fc = pdu[0]
        self.requests += 1
        try:
            handler = self._HANDLERS.get(fc)
            if handler is None:
                raise _ModbusEx(ILLEGAL_FUNCTION)
            return handler(self, pdu)
        except _ModbusEx as e:
            return bytes([fc | 0x80, e.code])
        except (struct.error, IndexError):
            return bytes([fc | 0x80, ILLEGAL_VALUE])

    @staticmethod
    def _addr_count(pdu, limit):
        if len(pdu) != 5:
            raise _ModbusEx(ILLEGAL_VALUE)
        addr, count = struct.unpack(">HH", pdu[1:5])
        if not 1 <= count <= limit:
            raise _ModbusEx(ILLEGAL_VALUE)
        if addr + count > 0x10000:
            raise _ModbusEx(ILLEGAL_ADDRESS)
        return addr, count

    def _check_mapped(self, function, addr, count):
        if self.strict:
            mapped = self._mapped[function]
            if any(a not in mapped for a in range(addr, addr + count)):
                raise _ModbusEx(ILLEGAL_ADDRESS)

    def _fc_read_bits(self, pdu):
        addr, count = self._addr_count(pdu, MAX_READ_BITS)
        if self.strict:
            raise _ModbusEx(ILLEGAL_FUNCTION)
        with self.lock:
            img = self._bits["coil" if pdu[0] == 1 else "discrete"]
            bits = [img.get(a, False) for a in range(addr, addr + count)]
        data = _pack_bits(bits)
        return bytes([pdu[0], len(data)]) + data

    def _fc_read_regs(self, pdu):
        addr, count = self._addr_count(pdu, MAX_READ_REGS)
        function = "holding" if pdu[0] == 3 else "input"
        with self.lock:
            self._check_mapped(function, addr, count)
            img = self._regs[function]
            regs = [img.get(a, 0) for a in range(addr, addr + count)]
        return bytes([pdu[0], 2 * count]) + struct.pack(f">{count}H", *regs)

    def _fc_write_coil(self, pdu):
        if len(pdu) != 5:
            raise _ModbusEx(ILLEGAL_VALUE)
        addr, value = struct.unpack(">HH", pdu[1:5])
        if value not in (0xFF00, 0x0000):
            raise _ModbusEx(ILLEGAL_VALUE)
        if self.strict:
            raise _ModbusEx(ILLEGAL_FUNCTION)
        with self.lock:
            self._bits["coil"][addr] = value == 0xFF00
        return pdu

    def _fc_write_register(self, pdu):
        if len(pdu) != 5:
            raise _ModbusEx(ILLEGAL_VALUE)
        addr, value = struct.unpack(">HH", pdu[1:5])
        with self.lock:
            self._check_mapped("holding", addr, 1)
            self._regs["holding"][addr] = value
        return pdu

    def _fc_write_coils(self, pdu):
        if len(pdu) < 6:
            raise _ModbusEx(ILLEGAL_VALUE)
        addr, count, nbytes = struct.unpack(">HHB", pdu[1:6])
        if not 1 <= count <= MAX_WRITE_BITS or nbytes != (count + 7) // 8 or len(pdu) != 6 + nbytes:
            raise _ModbusEx(ILLEGAL_VALUE)
        if addr + count > 0x10000:
            raise _ModbusEx(ILLEGAL_ADDRESS)
        if self.strict:
            raise _ModbusEx(ILLEGAL_FUNCTION)
        data = pdu[6:]
        with self.lock:
            img = self._bits["coil"]
            for i in range(count):
                img[addr + i] = bool(data[i // 8] >> (i % 8) & 1)
        return pdu[:5]

    def _fc_write_registers(self, pdu):
        if len(pdu) < 6:
            raise _ModbusEx(ILLEGAL_VALUE)
        addr, count, nbytes = struct.unpack(">HHB", pdu[1:6])
        if not 1 <= count <= MAX_WRITE_REGS or nbytes != 2 * count or len(pdu) != 6 + nbytes:
            raise _ModbusEx(ILLEGAL_VALUE)
        if addr + count > 0x10000:
            raise _ModbusEx(ILLEGAL_ADDRESS)
        values = struct.unpack(f">{count}H", pdu[6:])
        with self.lock:
            self._check_mapped("holding", addr, count)
            img = self._regs["holding"]
            for i, v in enumerate(values):
                img[addr + i] = v
        return pdu[:5]

    def _fc_diagnostics(self, pdu):
        # tylko 00 "Return Query Data" (echo) - przydatne jako ping
        if len(pdu) < 3 or pdu[1:3] != b"\x00\x00":
            raise _ModbusEx(ILLEGAL_FUNCTION)
        return pdu

    def _fc_device_id(self, pdu):
        if len(pdu) < 2 or pdu[1] != 0x0E:
            raise _ModbusEx(ILLEGAL_FUNCTION)
        if len(pdu) != 4:
            raise _ModbusEx(ILLEGAL_VALUE)
        code, oid = pdu[2], pdu[3]
        objs = {k: str(v).encode("utf-8")[:240] for k, v in self.identity.items()}
        level = 0x82 if any(k > 2 for k in objs) else 0x81
        if code == 4:
            if oid not in objs:
                raise _ModbusEx(ILLEGAL_ADDRESS)
            sel = [oid]
        elif code in (1, 2, 3):
            top = {1: 0x02, 2: 0x7F, 3: 0xFF}[code]
            ids = sorted(k for k in objs if k <= top)
            start = oid if oid in ids else 0
            sel = [k for k in ids if k >= start]
        else:
            raise _ModbusEx(ILLEGAL_VALUE)
        body, n, more, nxt = b"", 0, 0, 0
        for k in sel:
            item = bytes([k, len(objs[k])]) + objs[k]
            if 7 + len(body) + len(item) > 253:
                more, nxt = 0xFF, k
                break
            body += item
            n += 1
        return bytes([0x2B, 0x0E, code, level, more, nxt, n]) + body

    def _fc_server_id(self, pdu):
        if len(pdu) != 1:
            raise _ModbusEx(ILLEGAL_VALUE)
        text = f"{self.identity.get(0, '')} {self.identity.get(1, '')}".strip().encode("utf-8")[:200]
        data = bytes([self.unit & 0xFF, 0xFF]) + text
        return bytes([0x11, len(data)]) + data

    _HANDLERS = {
        1: _fc_read_bits, 2: _fc_read_bits, 3: _fc_read_regs, 4: _fc_read_regs,
        5: _fc_write_coil, 6: _fc_write_register, 8: _fc_diagnostics,
        15: _fc_write_coils, 16: _fc_write_registers, 17: _fc_server_id, 43: _fc_device_id,
    }


# ── serwery ───────────────────────────────────────────────────
class _SimBase:
    """Rejestr urządzeń + przetwarzanie zapytań (wspólne dla TCP i portu szeregowego)."""

    framing = "rtu"

    def __init__(self, devices=None):
        self.devices = devices if devices is not None else {}  # unit -> SimDevice
        self.response_delay = 0.0  # s, opóźnienie odpowiedzi (testy timeoutów)
        self.gateway_errors = False

    def add_device(self, dev):
        self.devices[dev.unit] = dev

    def remove_device(self, unit):
        return self.devices.pop(unit, None)

    def process_pdu(self, unit, pdu, tcp=None):
        """Zapytanie PDU dla urządzenia unit -> odpowiedź PDU albo None (brak odpowiedzi)."""
        if not pdu:
            return None
        tcp = (self.framing == "tcp") if tcp is None else tcp
        dev = self.devices.get(unit)
        if dev is None:
            if not tcp and unit == 0:
                # broadcast RTU: zapisy wykonują wszyscy, nikt nie odpowiada
                if pdu[0] in WRITE_FCS:
                    for d in list(self.devices.values()):
                        d.handle_pdu(pdu)
                return None
            if tcp and unit in (0, 255) and self.devices:
                dev = self.devices[min(self.devices)]
            elif tcp and self.gateway_errors:
                return bytes([pdu[0] | 0x80, GATEWAY_NO_RESPONSE])
            else:
                return None
        return dev.handle_pdu(pdu)

    def process_rtu_frame(self, frame):
        """Ramka RTU (z CRC) -> ramka odpowiedzi albo None (błąd CRC, obce urządzenie, broadcast)."""
        frame = bytes(frame)
        if not check_crc(frame):
            return None
        resp = self.process_pdu(frame[0], frame[1:-2], tcp=False)
        return add_crc(bytes([frame[0]]) + resp) if resp else None

    def _rtu_loop(self, read, write, gap, idle, alive, partial):
        """Wykrywa ramki RTU w strumieniu: po długości z kodu funkcji albo po przerwie."""
        buf = bytearray()
        while alive():
            n = rtu_request_length(buf)
            if n and len(buf) >= n:
                frame = bytes(buf[:n])
                del buf[:n]
                if not check_crc(frame):
                    buf.clear()  # utrata synchronizacji - odrzuć resztę
                    continue
                self._reply_rtu(frame, write)
                continue
            if buf:
                wait = gap if n == 0 else partial
            else:
                wait = idle
            data = read(wait)
            if data is None:
                return
            if data:
                buf += data
                if len(buf) > 512:
                    buf.clear()  # śmieci bez przerw (ramka RTU ma max 256 B)
            elif buf:
                # cisza - bufor to (niepełna lub nieznana) ramka
                frame = bytes(buf)
                buf.clear()
                self._reply_rtu(frame, write)

    def _reply_rtu(self, frame, write):
        resp = self.process_rtu_frame(frame)
        if resp:
            if self.response_delay:
                time.sleep(self.response_delay)
            write(resp)

    def serve_serial(self, port, baudrate=9600, parity="N", stopbits=1, bytesize=8):
        """Uruchamia slave RTU na porcie szeregowym z tymi samymi urządzeniami."""
        srv = SerialSimServer(port, baudrate, parity, stopbits, bytesize, devices=self.devices)
        srv.start()
        return srv


def _recv_exact(sock, n):
    buf = bytearray()
    while len(buf) < n:
        try:
            chunk = sock.recv(n - len(buf))
        except OSError:
            return None
        if not chunk:
            return None
        buf += chunk
    return bytes(buf)


class _Handler(socketserver.BaseRequestHandler):
    def handle(self):
        self.server.sim._serve_connection(self.request)


class _TcpServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True
    block_on_close = False


class _TcpServer6(_TcpServer):
    address_family = socket.AF_INET6


class SimServer(_SimBase):
    """Serwer Modbus TCP (framing="tcp") albo RTU over TCP (framing="rtu").

    gateway_errors=True: zapytanie do nieznanego urządzenia (TCP) dostaje
    wyjątek 0x0B zamiast ciszy - jak bramka TCP/RTU.
    """

    def __init__(self, host="0.0.0.0", port=5020, framing="tcp", gateway_errors=False, devices=None):
        if framing not in ("tcp", "rtu"):
            raise ValueError(f"nieznany rodzaj ramek: {framing!r} (tcp/rtu)")
        super().__init__(devices)
        self.host, self.port, self.framing = host, int(port), framing
        self.gateway_errors = gateway_errors
        self._srv = None
        self._thread = None
        self._conns = set()
        self._conns_lock = threading.Lock()

    @property
    def address(self):
        if self._srv is not None:
            return tuple(self._srv.server_address[:2])
        return self.host, self.port

    @property
    def running(self):
        return self._srv is not None

    def start(self):
        if self._srv is not None:
            return
        cls = _TcpServer6 if ":" in self.host else _TcpServer
        srv = cls((self.host, self.port), _Handler)
        srv.sim = self
        self._srv = srv
        self._thread = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.2},
                                        name=f"modbus-sim-{self.framing}", daemon=True)
        self._thread.start()
        log.info("Symulator Modbus %s @ %s:%s", self.framing.upper(), *self.address)

    def stop(self):
        srv, self._srv = self._srv, None
        if srv is None:
            return
        srv.shutdown()
        srv.server_close()
        with self._conns_lock:
            conns = list(self._conns)
        for s in conns:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        if self._thread:
            self._thread.join(timeout=2)

    def _serve_connection(self, sock):
        with self._conns_lock:
            self._conns.add(sock)
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            if self.framing == "tcp":
                self._serve_mbap(sock)
            else:
                self._serve_rtu_tcp(sock)
        except OSError as e:
            log.debug("Symulator: połączenie zamknięte: %s", e)
        finally:
            with self._conns_lock:
                self._conns.discard(sock)

    def _serve_mbap(self, sock):
        while self._srv is not None:
            hdr = _recv_exact(sock, 7)
            if hdr is None:
                return
            tid, pid, length, unit = struct.unpack(">HHHB", hdr)
            if not 2 <= length <= 254:
                return  # nie da się odzyskać synchronizacji - rozłącz
            pdu = _recv_exact(sock, length - 1)
            if pdu is None:
                return
            if pid != 0:
                continue  # to nie Modbus - pomiń ramkę
            resp = self.process_pdu(unit, pdu, tcp=True)
            if resp is None:
                continue
            if self.response_delay:
                time.sleep(self.response_delay)
            sock.sendall(struct.pack(">HHHB", tid, 0, len(resp) + 1, unit) + resp)

    def _serve_rtu_tcp(self, sock):
        def read(timeout):
            sock.settimeout(timeout)
            try:
                data = sock.recv(512)
            except socket.timeout:
                return b""
            except OSError:
                return None
            return data or None

        # TCP może dzielić ramki - na resztę znanej ramki czekamy dłużej
        self._rtu_loop(read, sock.sendall, 0.05, None, lambda: self._srv is not None, 1.0)


class _FdIO:
    """Port jako deskryptor (np. strona master pary pty)."""

    def __init__(self, fd):
        self.fd = fd

    def read(self, timeout):
        try:
            r, _, _ = select.select([self.fd], [], [], timeout)
            return os.read(self.fd, 512) if r else b""
        except OSError:
            time.sleep(timeout or 0.1)  # druga strona pty zamknięta
            return b""

    def write(self, data):
        os.write(self.fd, data)

    def close(self):
        pass  # deskryptor należy do wywołującego


class _SerialIO:
    def __init__(self, ser):
        import serial
        self.ser = ser
        self._timeout = ser.timeout
        self._errors = (serial.SerialException, OSError, TypeError, AttributeError)

    def read(self, timeout):
        try:
            if timeout != self._timeout:
                self.ser.timeout = self._timeout = timeout
            data = self.ser.read(1)
            if data:
                n = self.ser.in_waiting
                if n:
                    data += self.ser.read(n)
            return data
        except self._errors:
            if not self.ser.is_open:
                return None
            time.sleep(timeout or 0.1)
            return b""

    def write(self, data):
        self.ser.write(data)
        self.ser.flush()

    def close(self):
        self.ser.close()


class SerialSimServer(_SimBase):
    """Slave Modbus RTU na porcie szeregowym (pyserial) albo deskryptorze pty.

    port: ścieżka ("/dev/ttyUSB0", "COM5", "/dev/pts/3", URL pyserial "loop://")
    albo int - otwarty deskryptor (np. master z make_pty()).
    devices: słownik urządzeń współdzielony np. z SimServer.
    """

    def __init__(self, port, baudrate=9600, parity="N", stopbits=1, bytesize=8, devices=None):
        super().__init__(devices)
        self.port, self.baudrate = port, int(baudrate)
        self.parity, self.stopbits, self.bytesize = str(parity).upper()[:1], stopbits, int(bytesize)
        bits = 1 + self.bytesize + (self.parity != "N") + (2 if stopbits == 2 else 1)
        # przerwa 3.5 znaku (min. 1.75 ms powyżej 19200), z zapasem na planistę systemu
        self.gap = max(3.5 * bits / self.baudrate if self.baudrate <= 19200 else 0.00175, 0.005)
        self._io = None
        self._thread = None
        self._running = False

    def start(self):
        if self._running:
            return
        if isinstance(self.port, int):
            self._io = _FdIO(self.port)
        else:
            import serial  # pyserial potrzebny tylko tutaj
            ser = serial.serial_for_url(self.port, baudrate=self.baudrate, parity=self.parity,
                                        stopbits=self.stopbits, bytesize=self.bytesize, timeout=0.2)
            self._io = _SerialIO(ser)
        self._running = True
        self._thread = threading.Thread(target=self._run, name="modbus-sim-serial", daemon=True)
        self._thread.start()
        log.info("Symulator Modbus RTU @ %s %s", self.port, self.baudrate)

    def _run(self):
        io = self._io
        try:
            self._rtu_loop(io.read, io.write, self.gap, 0.2, lambda: self._running,
                           max(self.gap, 0.05))
        except Exception as e:  # noqa: BLE001 - wątek nie może zginąć po cichu
            if self._running:
                log.error("Symulator RTU: %s", e)

    def stop(self):
        if not self._running:
            return
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)
        if self._io:
            self._io.close()
            self._io = None


def make_pty():
    """Para pseudoterminali w trybie raw: (master_fd, slave_fd, ścieżka_slave). Tylko POSIX.

    Symulator na master_fd (SerialSimServer(master_fd)), klient RTU na ścieżce
    slave. slave_fd trzeba trzymać otwarty do końca testu (potem os.close obu).
    """
    import tty
    master, slave = os.openpty()
    tty.setraw(master)
    tty.setraw(slave)
    path = os.ttyname(slave)
    return master, slave, path


# ── całość: fizyka + serwer + aktualizacja ────────────────────
class Simulator:
    """Fizyka + serwer + wątek aktualizujący rejestry co interval sekund."""

    def __init__(self, host="0.0.0.0", port=5020, framing="tcp", interval=1.0, seed=None):
        self.physics = Physics(seed)
        self.server = SimServer(host, port, framing)
        self.interval = float(interval)
        self.values = {}
        self._physics_of = {}  # unit -> własna fizyka urządzenia
        self._serial = []
        self._stop = threading.Event()
        self._thread = None
        self._lock = threading.Lock()

    @property
    def address(self):
        return self.server.address

    @property
    def devices(self):
        return self.server.devices

    def add_preset(self, unit, preset, strict=False, physics=None):
        """Dodaje urządzenie z presetem (znormalizowanym lub surowym). Zwraca SimDevice."""
        dev = SimDevice(unit, preset, strict)
        with self._lock:
            if physics is not None:
                self._physics_of[unit] = physics
            else:
                self._physics_of.pop(unit, None)
            ph = physics or self.physics
            dev.update(ph.values or ph.tick())
            self.server.add_device(dev)
        return dev

    def remove_device(self, unit):
        with self._lock:
            self._physics_of.pop(unit, None)
            return self.server.remove_device(unit)

    def update(self):
        """Jeden krok: tick fizyki i zapis rejestrów wszystkich urządzeń."""
        with self._lock:
            ticked = {id(self.physics): self.physics.tick()}
            for unit, dev in list(self.server.devices.items()):
                ph = self._physics_of.get(unit, self.physics)
                if id(ph) not in ticked:
                    ticked[id(ph)] = ph.tick()
                dev.update(ticked[id(ph)])
            self.values = ticked[id(self.physics)]

    def _loop(self):
        while not self._stop.wait(self.interval):
            try:
                self.update()
            except Exception as e:  # noqa: BLE001
                log.error("Symulator: %s", e)

    def start(self, tcp=True):
        """tcp=False: tylko aktualizacja rejestrów (np. gdy działa wyłącznie port szeregowy)."""
        if self._thread is not None:
            return
        self.update()
        if tcp:
            self.server.start()
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="modbus-sim-update", daemon=True)
        self._thread.start()

    def serve_serial(self, port, baudrate=9600, parity="N", stopbits=1, bytesize=8):
        srv = self.server.serve_serial(port, baudrate, parity, stopbits, bytesize)
        self._serial.append(srv)
        return srv

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None
        for srv in self._serial:
            srv.stop()
        self._serial.clear()
        self.server.stop()


# ── uruchomienie samodzielne ──────────────────────────────────
def main(argv=None):
    root = Path(__file__).resolve().parent.parent
    ap = argparse.ArgumentParser(prog="python -m modbus_dash.simulator",
                                 description="Symulator licznika energii Modbus TCP / RTU")
    ap.add_argument("--host", default="0.0.0.0", help="adres nasłuchu TCP (domyślnie 0.0.0.0)")
    ap.add_argument("--port", type=int, default=5020, help="port TCP (domyślnie 5020, 0 = bez TCP)")
    ap.add_argument("--framing", choices=("tcp", "rtu"), default="tcp",
                    help="ramki na TCP: tcp (MBAP) albo rtu (RTU over TCP)")
    ap.add_argument("--preset", action="append", default=[],
                    help="plik lub identyfikator presetu (można powtarzać; adresy kolejno od --unit)")
    ap.add_argument("--unit", type=int, default=1, help="adres pierwszego urządzenia (domyślnie 1)")
    ap.add_argument("--strict", action="store_true", help="wyjątek 02 dla adresów spoza presetu")
    ap.add_argument("--serial", help="port szeregowy dla slave RTU (np. /dev/ttyUSB0, COM5)")
    ap.add_argument("--baudrate", type=int, default=9600)
    ap.add_argument("--parity", choices=("N", "E", "O"), default="N")
    ap.add_argument("--stopbits", type=int, choices=(1, 2), default=1)
    ap.add_argument("--seed", type=int, default=None, help="ziarno losowania (powtarzalne wartości)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")

    def find(name):
        # ścieżka do pliku albo identyfikator presetu (presets/, presets/library/)
        for c in (Path(name), root / "presets" / f"{name}.json", root / "presets" / "library" / f"{name}.json"):
            if c.is_file():
                return c
        raise SystemExit(f"Nie znaleziono presetu: {name}")

    sim = Simulator(args.host, args.port, args.framing, seed=args.seed)
    for i, name in enumerate(args.preset or ["simulator_3f"]):
        p = find(name)
        raw = json.loads(p.read_text(encoding="utf-8"))
        sim.add_preset(args.unit + i, normalize_preset(raw), strict=args.strict)
        log.info("Urządzenie %d: %s", args.unit + i, raw.get("name") or p)
    sim.start(tcp=bool(args.port))
    if args.serial:
        sim.serve_serial(args.serial, args.baudrate, args.parity, args.stopbits)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        sim.stop()


if __name__ == "__main__":
    main()
