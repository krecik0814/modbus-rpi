"""Testy symulatora: model fizyczny, obsługa PDU, ramki MBAP / RTU, odczyt presetów."""

import cmath
import json
import math
import os
import socket
import struct
import threading
import time
from pathlib import Path

import pytest

from modbus_dash import codec, planner, presets, quantities
from modbus_dash import simulator as S

ROOT = Path(__file__).resolve().parent.parent


# ── pomocnicze ────────────────────────────────────────────────
class BusError(Exception):
    def __init__(self, kind, code=None):
        super().__init__(f"{kind} {code}" if code else kind)
        self.kind, self.code = kind, code


def _recv(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise BusError("connection")
        buf += chunk
    return buf


class RawBus:
    """Minimalny klient Modbus (MBAP albo RTU over TCP) - adapter dla PresetReader."""

    def __init__(self, address, framing="tcp", timeout=2.0):
        self.sock = socket.create_connection(address, timeout=timeout)
        self.framing, self.tid = framing, 0

    def close(self):
        self.sock.close()

    def request(self, unit, pdu):
        try:
            if self.framing == "tcp":
                self.tid = (self.tid + 1) & 0xFFFF
                self.sock.sendall(struct.pack(">HHHB", self.tid, 0, len(pdu) + 1, unit) + pdu)
                tid, pid, length, u = struct.unpack(">HHHB", _recv(self.sock, 7))
                assert (tid, pid, u) == (self.tid, 0, unit)
                return _recv(self.sock, length - 1)
            self.sock.sendall(S.add_crc(bytes([unit]) + pdu))
            head = _recv(self.sock, 2)
            fc = head[1]
            if fc & 0x80:
                rest = _recv(self.sock, 3)
            elif fc in (1, 2, 3, 4, 17):
                bc = _recv(self.sock, 1)
                rest = bc + _recv(self.sock, bc[0] + 2)
            else:
                rest = _recv(self.sock, 6)
            frame = head + rest
            if not S.check_crc(frame):
                raise BusError("io")
            assert frame[0] == unit
            return frame[1:-2]
        except socket.timeout:
            raise BusError("timeout") from None

    def read_registers(self, unit, function, address, count):
        fc = 4 if function == "input" else 3
        resp = self.request(unit, struct.pack(">BHH", fc, address, count))
        if resp[0] & 0x80:
            raise BusError("exception", resp[1])
        return list(struct.unpack(f">{resp[1] // 2}H", resp[2:]))


def legacy_preset():
    for p in (ROOT / "presets" / "simulator_3f.json", ROOT / "presets" / "library" / "simulator_3f.json"):
        if p.is_file():
            return presets.normalize_preset(json.loads(p.read_text(encoding="utf-8")))
    pytest.skip("brak presets/simulator_3f.json")


# typ -> [(klucz, scale, offset)]; po jednym na każdą kolejność bajtów
_POOLS = {
    "int8": [("pf_l2", 0.01, 0), ("thd_v_l2", 0.1, 0), ("thd_i_l1", 0.2, 0), ("thd_i_avg", 0.2, 0)],
    "uint8": [("thd_v_l3", 0.1, 0), ("thd_i_l3", 0.1, 0), ("current_l2", 0.1, 0), ("thd_v_avg", 0.1, 0)],
    "int16": [("voltage_l1", 0.1, 0), ("pf_l3", 0.001, 0), ("power_l3", 1, 0), ("thd_v_l1", 0.1, 0)],
    "uint16": [("voltage_l2", 0.1, 0), ("current_l1", 0.01, 0), ("frequency", 0.01, 0),
               ("temperature", 0.1, -40)],
    "int32": [("power_total", 0.1, 0), ("energy_net", 0.01, 0), ("reactive_total", 1, 0),
              ("phase_angle_l3", 0.1, 0)],
    "uint32": [("energy_import", 0.01, 0), ("energy_export", 0.1, 0), ("apparent_total", 1, 0),
               ("voltage_l12", 0.001, 0)],
    "int64": [("energy_import_l1", 0.001, 0), ("power_l1", 0.001, 0), ("power_demand", 1, 0),
              ("pf_total", 0.0001, 0)],
    "uint64": [("energy_total", 0.0001, 0), ("current_n", 0.001, 0), ("energy_reactive_import", 1, 0),
               ("voltage_ll_avg", 0.01, 0)],
    "float32": [("voltage_l3", 1, 0), ("current_l3", 1, 0), ("power_l2", 1, 0), ("pf_l1", 1, 0)],
    "float64": [("energy_apparent", 1, 0), ("energy_export_l3", 1, 0), ("current_total", 1, 0),
                ("phase_angle_total", 1, 0)],
}


def synthetic_raw():
    """Preset z każdym typem i każdą kolejnością bajtów, input i holding, skale i offset."""
    regs, addr = {}, {"input": 0, "holding": 1000}
    i = 0
    for dtype, pool in _POOLS.items():
        for order, (key, scale, offset) in zip(codec.BYTE_ORDERS, pool):
            func = "holding" if i % 2 else "input"
            regs[key] = {"address": addr[func], "type": dtype, "byte_order": order,
                         "register_type": func, "scale": scale, "offset": offset, "decimals": 4}
            addr[func] += codec.register_count(dtype) + (i % 3)  # z przerwami
            i += 1
    regs["config_reg"] = {"address": 2000, "type": "uint16", "register_type": "holding"}
    return {"name": "Syntetyczny", "manufacturer": "Test", "model": "T-1", "byte_order": "ABCD",
            "registers": regs}


def expected_ok(spec, got, want):
    tol = 0.5 * 10 ** -spec["decimals"] + 0.5 * abs(spec["scale"]) + abs(want) * 1e-6 + 1e-9
    return got is not None and abs(got - want) <= tol


def assert_roundtrip(preset, values, q, aliases=None):
    aliases = aliases or {}
    assert not [k for k, v in values.items() if v is None and k != "config_reg"], values
    for key, spec in preset["registers"].items():
        if key == "config_reg":
            assert values[key] == 0
            continue
        want = q[aliases.get(key, key)]
        assert expected_ok(spec, values[key], want), (key, values[key], want)


@pytest.fixture
def physics_values():
    ph = S.Physics(seed=7)
    ph.tick(0)
    return ph.tick(75)  # szczyt PV - L3 oddaje energię


@pytest.fixture
def server():
    started = []

    def make(framing="tcp", **kw):
        srv = S.SimServer("127.0.0.1", 0, framing, **kw)
        srv.start()
        started.append(srv)
        return srv

    yield make
    for srv in started:
        srv.stop()


def pdu_device(strict=False):
    dev = S.SimDevice(1, presets.normalize_preset(synthetic_raw()), strict=strict)
    ph = S.Physics(seed=1)
    dev.update(ph.tick(0))
    return dev


# ── CRC i ramki ───────────────────────────────────────────────
def test_crc16_known_vector():
    assert S.add_crc(bytes.fromhex("01030000000A")) == bytes.fromhex("01030000000AC5CD")
    assert S.check_crc(bytes.fromhex("01030000000AC5CD"))
    assert not S.check_crc(bytes.fromhex("01030000000AC5CE"))


def test_rtu_request_length():
    assert S.rtu_request_length(b"\x01") is None
    assert S.rtu_request_length(bytes.fromhex("0103")) == 8
    assert S.rtu_request_length(bytes.fromhex("011000000002")) is None
    assert S.rtu_request_length(bytes.fromhex("01100000000204")) == 13
    assert S.rtu_request_length(bytes.fromhex("012B")) == 7
    assert S.rtu_request_length(bytes.fromhex("0111")) == 4
    assert S.rtu_request_length(bytes.fromhex("0141")) == 0


# ── fizyka ────────────────────────────────────────────────────
def test_physics_all_quantities_and_determinism():
    a, b = S.Physics(seed=3), S.Physics(seed=3)
    for t in (0, 1, 2, 30):
        qa, qb = a.tick(t), b.tick(t)
        assert qa == qb
    assert set(qa) == set(quantities.QUANTITIES)
    assert all(math.isfinite(v) for v in qa.values())


def test_physics_relations(physics_values):
    q = physics_values
    for ph in ("l1", "l2", "l3"):
        v, i, p = q[f"voltage_{ph}"], q[f"current_{ph}"], q[f"power_{ph}"]
        s, r, pf = q[f"apparent_{ph}"], q[f"reactive_{ph}"], q[f"pf_{ph}"]
        assert p == pytest.approx(v * i * pf, rel=1e-9)
        assert s == pytest.approx(v * i, rel=1e-9)
        assert s ** 2 == pytest.approx(p ** 2 + r ** 2, rel=1e-9)
        assert q[f"phase_angle_{ph}"] == pytest.approx(math.degrees(math.atan2(r, p)))
    assert 200 < q["voltage_l1"] < 260
    for a, b in (("l1", "l2"), ("l2", "l3"), ("l3", "l1")):
        ll = q[f"voltage_{a}{b[-1]}"]
        assert ll == pytest.approx(math.sqrt(3) * (q[f"voltage_{a}"] + q[f"voltage_{b}"]) / 2, rel=0.03)
    assert q["power_total"] == pytest.approx(q["power_l1"] + q["power_l2"] + q["power_l3"])
    assert q["pf_total"] == pytest.approx(q["power_total"] / q["apparent_total"])
    assert q["current_avg"] == pytest.approx(q["current_total"] / 3)
    # prąd N = suma fazorów prądów (fazy co 120 stopni; szum kąta napięcia ~0.2 stopnia)
    n = abs(sum(cmath.rect(q[f"current_{ph}"], math.radians(base - q[f"phase_angle_{ph}"]))
                for ph, base in (("l1", 0), ("l2", -120), ("l3", 120))))
    assert q["current_n"] == pytest.approx(n, rel=0.03, abs=0.05)
    assert q["energy_net"] == pytest.approx(q["energy_import"] - q["energy_export"])
    assert q["energy_import"] == pytest.approx(q["energy_import_t1"] + q["energy_import_t2"])
    assert q["energy_export"] == pytest.approx(q["energy_export_t1"] + q["energy_export_t2"])
    assert q["power_import"] - q["power_export"] == pytest.approx(q["power_total"])
    assert q["energy_total_l3"] == pytest.approx(q["energy_import_l3"] + q["energy_export_l3"])


def test_physics_export_and_energy_accumulation():
    ph = S.Physics(seed=11)
    ph.appliance_rate = 1e12  # bez losowych odbiorników
    prev = ph.tick(0)
    exported = False
    for t in range(5, 301, 5):
        q = ph.tick(t)
        p3 = q["power_l3"]
        if p3 < 0:
            exported = True
            assert q["pf_l3"] < 0
            assert q["energy_export_l3"] == pytest.approx(prev["energy_export_l3"] - p3 * 5 / 3.6e6)
            assert q["energy_import_l3"] == prev["energy_import_l3"]
        else:
            assert q["energy_import_l3"] == pytest.approx(prev["energy_import_l3"] + p3 * 5 / 3.6e6)
        for k in ("energy_import", "energy_export", "energy_reactive_import", "energy_apparent"):
            assert q[k] >= prev[k]
        prev = q
    assert exported
    assert prev["energy_export"] > 234.567  # suma po bilansowaniu też oddawała
    assert prev["power_demand_max"] >= prev["power_demand"] > 0


def test_phase_view_single_phase(physics_values):
    q = S.phase_view(physics_values, 1)
    assert q["power_total"] == q["power_l1"]
    assert q["energy_import"] == q["energy_import_l1"]
    assert S.phase_view(physics_values, 3) is physics_values


# ── PDU ───────────────────────────────────────────────────────
def test_pdu_read_registers_and_exceptions():
    dev = pdu_device()
    resp = dev.handle_pdu(struct.pack(">BHH", 4, 0, 10))
    assert resp[:2] == bytes([4, 20]) and len(resp) == 22
    assert list(struct.unpack(">10H", resp[2:])) == dev.read("input", 0, 10)
    assert dev.handle_pdu(struct.pack(">BHH", 3, 5000, 125))[:2] == bytes([3, 250])
    assert dev.handle_pdu(struct.pack(">BHH", 3, 0, 0)) == b"\x83\x03"
    assert dev.handle_pdu(struct.pack(">BHH", 4, 0, 126)) == b"\x84\x03"
    assert dev.handle_pdu(struct.pack(">BHH", 4, 65535, 2)) == b"\x84\x02"
    assert dev.handle_pdu(struct.pack(">BHH", 4, 65535, 1))[:2] == b"\x04\x02"
    assert dev.handle_pdu(struct.pack(">BHH", 1, 0, 2001)) == b"\x81\x03"
    assert dev.handle_pdu(b"\x03\x00\x00") == b"\x83\x03"   # za krótkie
    assert dev.handle_pdu(b"\x41\x00\x00\x00\x01") == b"\xc1\x01"
    assert dev.handle_pdu(b"\x07") == b"\x87\x01"
    assert dev.handle_pdu(b"\x08\x00\x00\x12\x34") == b"\x08\x00\x00\x12\x34"


def test_pdu_writes():
    dev = pdu_device()
    assert dev.handle_pdu(struct.pack(">BHH", 6, 2000, 0xBEEF)) == struct.pack(">BHH", 6, 2000, 0xBEEF)
    assert dev.read("holding", 2000, 1) == [0xBEEF]
    dev.update(S.Physics(seed=2).tick(0))
    assert dev.read("holding", 2000, 1) == [0xBEEF]  # rejestr spoza słownika - zapis trwały
    req = struct.pack(">BHHB3H", 16, 3000, 3, 6, 1, 2, 3)
    assert dev.handle_pdu(req) == req[:5]
    assert dev.read("holding", 3000, 3) == [1, 2, 3]
    assert dev.handle_pdu(struct.pack(">BHHB3H", 16, 3000, 3, 5, 1, 2, 3)) == b"\x90\x03"
    assert dev.handle_pdu(struct.pack(">BHHB", 16, 3000, 0, 0)) == b"\x90\x03"
    assert dev.handle_pdu(struct.pack(">BHHB2H", 16, 65535, 2, 4, 1, 2)) == b"\x90\x02"
    # cewki
    assert dev.handle_pdu(struct.pack(">BHH", 5, 3, 0x1234)) == b"\x85\x03"
    assert dev.handle_pdu(struct.pack(">BHH", 5, 3, 0xFF00)) == struct.pack(">BHH", 5, 3, 0xFF00)
    assert dev.handle_pdu(struct.pack(">BHHBB", 15, 8, 4, 1, 0b1010)) == struct.pack(">BHH", 15, 8, 4)
    assert dev.handle_pdu(struct.pack(">BHHBB", 15, 8, 9, 1, 0)) == b"\x8f\x03"
    resp = dev.handle_pdu(struct.pack(">BHH", 1, 0, 12))
    assert resp == bytes([1, 2, 0b00001000, 0b00001010])
    assert dev.handle_pdu(struct.pack(">BHH", 2, 0, 3)) == bytes([2, 1, 0])


def test_pdu_device_identification_and_server_id():
    dev = pdu_device()
    resp = dev.handle_pdu(bytes([0x2B, 0x0E, 1, 0]))
    assert resp[:7] == bytes([0x2B, 0x0E, 1, 0x82, 0, 0, 3])
    objs, i = {}, 7
    while i < len(resp):
        oid, n = resp[i], resp[i + 1]
        objs[oid] = resp[i + 2:i + 2 + n].decode()
        i += 2 + n
    assert objs == {0: "Test", 1: "T-1", 2: S.__version__}
    resp = dev.handle_pdu(bytes([0x2B, 0x0E, 4, 1]))
    assert resp == bytes([0x2B, 0x0E, 4, 0x82, 0, 0, 1, 1, 3]) + b"T-1"
    assert dev.handle_pdu(bytes([0x2B, 0x0E, 2, 0]))[6] == 6       # regular: 0-2 i 4-6
    assert dev.handle_pdu(bytes([0x2B, 0x0E, 4, 0x50])) == b"\xab\x02"
    assert dev.handle_pdu(bytes([0x2B, 0x0E, 5, 0])) == b"\xab\x03"
    assert dev.handle_pdu(bytes([0x2B, 0x0D, 1, 0])) == b"\xab\x01"
    resp = dev.handle_pdu(b"\x11")
    assert resp[0] == 0x11 and resp[1] == len(resp) - 2 and resp[2:4] == b"\x01\xff"
    assert b"Test T-1" in resp


def test_pdu_strict_mode():
    dev = pdu_device(strict=True)
    spec = dev.preset["registers"]["voltage_l1"]
    ok = dev.handle_pdu(struct.pack(">BHH", 4, spec["address"], 1))
    assert ok[0] == 4
    assert dev.handle_pdu(struct.pack(">BHH", 4, 0, 125)) == b"\x84\x02"   # przerwy w mapie
    assert dev.handle_pdu(struct.pack(">BHH", 4, 60000, 1)) == b"\x84\x02"
    assert dev.handle_pdu(struct.pack(">BHH", 6, 2001, 1)) == b"\x86\x02"
    assert dev.handle_pdu(struct.pack(">BHH", 6, 2000, 1)) == struct.pack(">BHH", 6, 2000, 1)
    assert dev.handle_pdu(struct.pack(">BHH", 1, 0, 1)) == b"\x81\x01"


def test_unit_routing_and_broadcast():
    srv = S.SimServer(framing="tcp")
    d1, d5 = pdu_device(), S.SimDevice(5, legacy_preset())
    srv.add_device(d1)
    srv.add_device(d5)
    req = struct.pack(">BHH", 4, 0, 2)
    assert srv.process_pdu(5, req) == d5.handle_pdu(req)
    assert srv.process_pdu(9, req) is None
    assert srv.process_pdu(0, req) == d1.handle_pdu(req)       # TCP: 0/255 = dowolne
    assert srv.process_pdu(255, req) == d1.handle_pdu(req)
    assert srv.process_pdu(255, req, tcp=False) is None
    srv.gateway_errors = True
    assert srv.process_pdu(9, req) == b"\x84\x0b"
    # broadcast RTU: zapis bez odpowiedzi
    assert srv.process_rtu_frame(S.add_crc(b"\x00" + struct.pack(">BHH", 6, 4000, 77))) is None
    assert d1.read("holding", 4000, 1) == [77] and d5.read("holding", 4000, 1) == [77]
    resp = srv.process_rtu_frame(S.add_crc(b"\x05" + req))
    assert S.check_crc(resp) and resp[:-2] == b"\x05" + d5.handle_pdu(req)
    bad = bytearray(S.add_crc(b"\x05" + req))
    bad[-1] ^= 0xFF
    assert srv.process_rtu_frame(bytes(bad)) is None
    assert srv.process_rtu_frame(S.add_crc(b"\x09" + req)) is None


def test_unit_conversion_and_raw_preset(physics_values):
    raw = {"name": "kW", "phases": 3, "registers": {
        "power_total": {"address": 0, "unit": "kW", "decimals": 3},
        "energy_import": {"address": 2, "unit": "Wh", "type": "uint64", "decimals": 0},
        "reactive_total": {"address": 6, "unit": "kVAr", "decimals": 3},
        "current_neutral": {"address": 8, "unit": "A", "decimals": 3},
    }}
    dev = S.SimDevice(1, raw)  # surowy preset jest normalizowany
    dev.update(physics_values)
    q = physics_values
    assert codec.decode(dev.read("input", 0, 2)) == pytest.approx(q["power_total"] / 1000, rel=1e-6)
    assert codec.decode(dev.read("input", 2, 4), "uint64") == round(q["energy_import"] * 1000)
    assert codec.decode(dev.read("input", 6, 2)) == pytest.approx(q["reactive_total"] / 1000, rel=1e-6)
    assert codec.decode(dev.read("input", 8, 2)) == pytest.approx(q["current_n"], rel=1e-6)


def test_update_is_atomic():
    raw = {"name": "atom", "registers": {
        "energy_import": {"address": 0, "type": "float64", "byte_order": "DCBA"},
        "energy_export": {"address": 4, "type": "int64", "byte_order": "CDAB"},
    }}
    dev = S.SimDevice(1, raw)
    sets = [{"energy_import": 1e10 + i, "energy_export": -(10 ** 15) - i} for i in range(4)]
    stop = threading.Event()

    def writer():
        i = 0
        while not stop.is_set():
            dev.update(sets[i % 4])
            i += 1

    th = threading.Thread(target=writer)
    th.start()
    try:
        req = struct.pack(">BHH", 4, 0, 8)
        for _ in range(3000):
            regs = struct.unpack(">8H", dev.handle_pdu(req)[2:])
            a, b = codec.decode(regs[:4], "float64", "DCBA"), codec.decode(regs[4:], "int64", "CDAB")
            assert a in (0.0, 1e10, 1e10 + 1, 1e10 + 2, 1e10 + 3)
            if a:
                assert b == -(10 ** 15) - (a - 1e10)
    finally:
        stop.set()
        th.join()


# ── serwer TCP (MBAP) ─────────────────────────────────────────
def test_mbap_socket_framing(server):
    srv = server("tcp")
    srv.add_device(pdu_device())
    sock = socket.create_connection(srv.address, timeout=2)
    try:
        req = struct.pack(">BHH", 4, 0, 2)
        # dwa zapytania w jednym pakiecie + transaction id
        sock.sendall(struct.pack(">HHHB", 0x1234, 0, 6, 1) + req + struct.pack(">HHHB", 0xABCD, 0, 6, 1) + req)
        for tid in (0x1234, 0xABCD):
            hdr = _recv(sock, 7)
            assert struct.unpack(">HHHB", hdr) == (tid, 0, 7, 1)
            assert _recv(sock, 6)[:2] == b"\x04\x04"
        # zapytanie bajt po bajcie
        frame = struct.pack(">HHHB", 7, 0, 6, 1) + req
        for b in frame:
            sock.send(bytes([b]))
            time.sleep(0.002)
        assert struct.unpack(">HHHB", _recv(sock, 7))[0] == 7
        _recv(sock, 6)
        # zły protocol id - ramka pominięta; nieznane urządzenie - cisza
        sock.sendall(struct.pack(">HHHB", 8, 1, 6, 1) + req)
        sock.sendall(struct.pack(">HHHB", 9, 0, 6, 77) + req)
        sock.sendall(struct.pack(">HHHB", 10, 0, 6, 1) + req)
        assert struct.unpack(">HHHB", _recv(sock, 7))[0] == 10
        _recv(sock, 6)
        # wyjątek
        sock.sendall(struct.pack(">HHHB", 11, 0, 6, 1) + struct.pack(">BHH", 4, 0, 0))
        assert _recv(sock, 9) == struct.pack(">HHHB", 11, 0, 3, 1) + b"\x84\x03"
    finally:
        sock.close()


def test_mbap_concurrent_clients(server):
    srv = server("tcp")
    dev = pdu_device()
    srv.add_device(dev)
    expected = dev.read("input", 0, 20)
    errors = []

    def client():
        try:
            bus = RawBus(srv.address)
            for _ in range(30):
                assert bus.read_registers(1, "input", 0, 20) == expected
            bus.close()
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=client) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors


def test_server_stop_closes_connections(server):
    srv = S.SimServer("127.0.0.1", 0)
    srv.add_device(pdu_device())
    srv.start()
    bus = RawBus(srv.address)
    assert bus.read_registers(1, "input", 0, 2)
    srv.stop()
    with pytest.raises((BusError, OSError)):
        bus.read_registers(1, "input", 0, 2)
    bus.close()


# ── RTU over TCP ──────────────────────────────────────────────
def test_rtu_over_tcp_framing(server):
    srv = server("rtu")
    dev = pdu_device()
    srv.add_device(dev)
    sock = socket.create_connection(srv.address, timeout=2)
    try:
        frame = S.add_crc(b"\x01" + struct.pack(">BHH", 4, 0, 2))
        want = S.add_crc(b"\x01" + dev.handle_pdu(frame[1:-2]))
        sock.sendall(frame[:3])
        time.sleep(0.02)
        sock.sendall(frame[3:])
        assert _recv(sock, len(want)) == want
        # zły CRC - brak odpowiedzi, następna ramka obsłużona
        bad = bytearray(frame)
        bad[-1] ^= 0x55
        sock.sendall(bytes(bad))
        sock.settimeout(0.3)
        with pytest.raises(socket.timeout):
            sock.recv(10)
        sock.settimeout(2)
        sock.sendall(frame)
        assert _recv(sock, len(want)) == want
        # FC16 (długość z licznika bajtów) i nieznana funkcja (wykrywana po przerwie)
        sock.sendall(S.add_crc(b"\x01" + struct.pack(">BHHB2H", 16, 10, 2, 4, 5, 6)))
        assert _recv(sock, 8) == S.add_crc(b"\x01" + struct.pack(">BHH", 16, 10, 2))
        sock.sendall(S.add_crc(b"\x01\x41\x00"))
        assert _recv(sock, 5) == S.add_crc(b"\x01\xc1\x01")
        # nieznane urządzenie - cisza
        sock.sendall(S.add_crc(b"\x09" + struct.pack(">BHH", 4, 0, 2)))
        sock.settimeout(0.3)
        with pytest.raises(socket.timeout):
            sock.recv(10)
    finally:
        sock.close()


# ── odczyt presetów przez PresetReader ────────────────────────
@pytest.mark.parametrize("framing", ["tcp", "rtu"])
def test_roundtrip_legacy_preset(server, physics_values, framing):
    preset = legacy_preset()
    assert len(preset["registers"]) == 35
    dev = S.SimDevice(1, preset)
    dev.update(physics_values)
    srv = server(framing)
    srv.add_device(dev)
    bus = RawBus(srv.address, framing)
    try:
        res = planner.PresetReader(preset).read(bus, 1)
    finally:
        bus.close()
    assert res["ok"] and not res["errors"]
    assert_roundtrip(preset, res["values"], physics_values,
                     {"current_neutral": "current_n", "energy_reactive": "energy_reactive_import"})
    assert res["values"]["power_l3"] < 0  # szczyt PV


@pytest.mark.parametrize("framing,strict", [("tcp", False), ("rtu", False), ("tcp", True)])
def test_roundtrip_synthetic_preset(server, physics_values, framing, strict):
    preset = presets.normalize_preset(synthetic_raw())
    used = {(s["type"], s["order"]) for s in preset["registers"].values() if s["key"] != "config_reg"}
    assert len(used) == len(codec.DATA_TYPES) * len(codec.BYTE_ORDERS)
    dev = S.SimDevice(3, preset, strict=strict)
    dev.update(physics_values)
    srv = server(framing)
    srv.add_device(dev)
    bus = RawBus(srv.address, framing)
    try:
        reader = planner.PresetReader(preset)
        res = reader.read(bus, 3)
        if strict:
            # licznik odrzuca bloki z przerwami - czytnik dzieli je aż do sukcesu
            assert res["requests"] > len(planner.plan_reads(preset["registers"]))
        res = reader.read(bus, 3)
    finally:
        bus.close()
    assert not res["errors"], res["errors"]
    assert_roundtrip(preset, res["values"], physics_values)


# ── Simulator (całość) ────────────────────────────────────────
def test_simulator_wrapper_updates():
    sim = S.Simulator("127.0.0.1", 0, interval=0.05, seed=5)
    sim.add_preset(1, legacy_preset())
    sim.add_preset(2, synthetic_raw(), strict=True)
    sim.start()
    try:
        bus = RawBus(sim.server.address)
        first = bus.read_registers(1, "input", 0, 70)
        deadline = time.monotonic() + 3
        while bus.read_registers(1, "input", 0, 70) == first:
            assert time.monotonic() < deadline, "rejestry się nie zmieniają"
            time.sleep(0.05)
        v = codec.decode(bus.read_registers(1, "input", 0, 2))
        assert 200 < v < 260
        with pytest.raises(BusError) as e:
            bus.read_registers(2, "input", 0, 125)
        assert e.value.code == 2
        assert set(sim.physics.values) == set(quantities.QUANTITIES)
        bus.close()
    finally:
        sim.stop()
    assert sim.server._srv is None


# ── port szeregowy (pty) ──────────────────────────────────────
pty_only = pytest.mark.skipif(not hasattr(os, "openpty"), reason="pty tylko na POSIX")


def _pty_request(fd, frame, n, timeout=2.0):
    import select
    os.write(fd, frame)
    buf, deadline = b"", time.monotonic() + timeout
    while len(buf) < n and time.monotonic() < deadline:
        r, _, _ = select.select([fd], [], [], 0.05)
        if r:
            buf += os.read(fd, 256)
    return buf


@pty_only
def test_serial_server_on_pty_master():
    master, slave, path = S.make_pty()
    srv = S.SerialSimServer(master, 9600)
    dev = pdu_device()
    srv.add_device(dev)
    srv.start()
    try:
        frame = S.add_crc(b"\x01" + struct.pack(">BHH", 4, 0, 4))
        want = S.add_crc(b"\x01" + dev.handle_pdu(frame[1:-2]))
        assert _pty_request(slave, frame, len(want)) == want
        bad = frame[:-1] + bytes([frame[-1] ^ 1])
        assert _pty_request(slave, bad, 1, timeout=0.3) == b""
        assert _pty_request(slave, frame, len(want)) == want
    finally:
        srv.stop()
        os.close(master)
        os.close(slave)


@pty_only
def test_serial_server_pyserial_path_shared_devices():
    pytest.importorskip("serial")
    master, slave, path = S.make_pty()
    tcp = S.SimServer("127.0.0.1", 0)
    tcp.add_device(pdu_device())
    srv = tcp.serve_serial(path, 19200)  # te same urządzenia co serwer TCP
    try:
        frame = S.add_crc(b"\x01" + struct.pack(">BHH", 3, 2000, 1))
        assert _pty_request(master, frame, 7) == S.add_crc(b"\x01\x03\x02\x00\x00")
    finally:
        srv.stop()
        os.close(master)
        os.close(slave)


@pty_only
def test_pymodbus_serial_client_against_pty():
    pytest.importorskip("serial")
    pm = pytest.importorskip("pymodbus.client")
    import inspect
    master, slave, path = S.make_pty()
    srv = S.SerialSimServer(master, 9600)
    dev = S.SimDevice(1, legacy_preset())
    ph = S.Physics(seed=4)
    dev.update(ph.tick(0))
    srv.add_device(dev)
    srv.start()
    client = pm.ModbusSerialClient(port=path, baudrate=9600, timeout=1)
    try:
        assert client.connect()
        params = inspect.signature(client.read_input_registers).parameters
        kw = {"device_id": 1} if "device_id" in params else {"slave": 1}
        rr = client.read_input_registers(0, count=10, **kw)
        assert not rr.isError()
        assert rr.registers == dev.read("input", 0, 10)
    finally:
        client.close()
        srv.stop()
        os.close(master)
        os.close(slave)


def test_simulator_scale_from_registers_roundtrip():
    from modbus_dash.planner import PresetReader
    from modbus_dash.presets import normalize_preset
    from modbus_dash.simulator import Physics, SimDevice
    p = normalize_preset({"registers": {
        "voltage_l1": {"address": 0, "type": "int16", "scale_from": "u_sf", "decimals": 2},
        "power_total": {"address": 1, "type": "int16", "scale_from": "p_sf", "decimals": 0},
        "u_sf": {"address": 2, "type": "int16"},
        "p_sf": {"address": 3, "type": "int8"},
        "energy_import": {"address": 4, "type": "uint32", "scale": 0.001, "scale_from": "e_fac",
                          "scale_from_mode": "multiply", "decimals": 3},
        "e_fac": {"address": 6, "type": "uint32"},
    }})
    dev = SimDevice(1, p)
    q = Physics(seed=3).tick(now=500.0)
    dev.update(q)

    class Bus:
        def read_registers(self, unit, function, address, count):
            return dev.read(function, address, count)

    res = PresetReader(p).read(Bus(), 1)
    assert abs(res["values"]["voltage_l1"] - q["voltage_l1"]) < 0.02
    assert abs(res["values"]["power_total"] - q["power_total"]) <= max(1.0, abs(q["power_total"]) * 1e-3)
    assert abs(res["values"]["energy_import"] - q["energy_import"]) < 0.002
