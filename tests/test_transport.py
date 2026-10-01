"""Testy transportu: prawdziwe klienty pymodbus przeciw własnemu symulatorowi (TCP, RTU over TCP, UDP, pty)."""

import inspect
import logging
import os
import select
import socket
import struct
import sys
import threading
import time

import pytest

from modbus_dash import codec
from modbus_dash import simulator as S
from modbus_dash import transport as T
from modbus_dash.transport import Bus, BusManager, ModbusError, TransportConfig

PRESET = {
    "name": "Test", "register_type": "input", "data_type": "float32", "phases": 3,
    "registers": {
        "voltage_l1": {"address": 0, "unit": "V"},
        "current_l1": {"address": 2, "unit": "A"},
        "frequency": {"address": 4, "unit": "Hz"},
        "power_total": {"address": 6, "unit": "W"},
        "energy_import": {"address": 100, "unit": "kWh"},
        "cfg_a": {"address": 0, "register_type": "holding", "type": "uint16"},
        "cfg_b": {"address": 1, "register_type": "holding", "type": "uint16"},
        "cfg_c": {"address": 2, "register_type": "holding", "type": "uint16"},
    },
}
QUANT = {"voltage_l1": 230.5, "current_l1": 1.25, "frequency": 50.0, "power_total": 1234.0,
         "energy_import": 4321.5}
EXPECTED = codec.encode(230.5) + codec.encode(1.25) + codec.encode(50.0) + codec.encode(1234.0)


def make_device(unit=1, strict=False):
    dev = S.SimDevice(unit, PRESET, strict=strict)
    dev.update(QUANT)
    return dev


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def server():
    """Symulator Modbus TCP: unit 1 (zwykły), unit 2 (strict)."""
    srv = S.SimServer("127.0.0.1", 0, "tcp")
    srv.add_device(make_device(1))
    srv.add_device(make_device(2, strict=True))
    srv.start()
    yield srv
    srv.stop()


def tcp_bus(srv, **kw):
    host, port = srv.address
    return Bus(TransportConfig(kind=kw.pop("kind", "tcp"), host=host, port=port, **kw))


# ── konfiguracja ──────────────────────────────────────────────
def test_config_from_dict_and_describe():
    c = TransportConfig.from_dict({"kind": "TCP", "host": " 10.0.0.5 ", "port": "1502", "timeout": "0.5"})
    assert (c.kind, c.host, c.port, c.timeout) == ("tcp", "10.0.0.5", 1502, 0.5)
    assert c.key() == "tcp:10.0.0.5:1502" and c.describe() == "TCP 10.0.0.5:1502"
    assert TransportConfig.from_dict(c.to_dict()) == c
    assert TransportConfig.from_dict({"host": "gw.local:503"}).port == 503
    v6 = TransportConfig.from_dict({"kind": "udp", "host": "[fe80::1]:1502"})
    assert (v6.host, v6.port, v6.describe()) == ("fe80::1", 1502, "UDP [fe80::1]:1502")
    r = TransportConfig.from_dict({"kind": "serial", "serial_port": "/dev/ttyUSB0", "parity": "even",
                                   "baudrate": 19200, "port": "", "host": ""})
    assert (r.kind, r.parity, r.key()) == ("rtu", "E", "serial:/dev/ttyUSB0")
    assert r.describe() == "RS-485 /dev/ttyUSB0 19200 8E1"
    a = TransportConfig.from_dict({"kind": "ascii", "serial_port": "/dev/ttyUSB0", "bytesize": 7})
    assert a.key() == r.key() and a.describe() == "RS-485 ASCII /dev/ttyUSB0 9600 7N1"
    assert TransportConfig.from_dict({"kind": "rtu-over-tcp", "host": "h"}).describe() == "RTU over TCP h:502"
    # pola bez znaczenia dla danego rodzaju łącza nie blokują zapisu
    assert TransportConfig.from_dict({"kind": "tcp", "baudrate": "abc", "parity": "?"}).baudrate == 9600
    assert TransportConfig.from_dict({"kind": "tcp", "timeout": "", "retries": None}).timeout == 1.0


@pytest.mark.parametrize("data,frag", [
    ({"kind": "modbus"}, "rodzaj"),
    ({"kind": "tcp", "host": ""}, "hosta"),
    ({"kind": "tcp", "host": "a b"}, "hosta"),
    ({"kind": "tcp", "port": 70000}, "port"),
    ({"kind": "tcp", "port": True}, "port"),
    ({"kind": "tcp", "host": "h:1", "port": 2}, "osobnym"),
    ({"kind": "rtu", "serial_port": ""}, "port szeregowy"),
    ({"kind": "rtu", "serial_port": "/dev/x", "baudrate": 50}, "prędkość"),
    ({"kind": "rtu", "serial_port": "/dev/x", "parity": "X"}, "parzystość"),
    ({"kind": "rtu", "serial_port": "/dev/x", "stopbits": 3}, "stopu"),
    ({"kind": "ascii", "serial_port": "/dev/x", "bytesize": 6}, "danych"),
    ({"timeout": 0}, "timeout"),
    ({"timeout": "nan"}, "timeout"),
    ({"retries": 11}, "ponowień"),
    ({"delay_ms": -1}, "przerwa"),
    ([], "obiektem"),
])
def test_config_validation(data, frag):
    with pytest.raises(ValueError) as e:
        TransportConfig.from_dict(data)
    assert frag in str(e.value)


def test_exception_mapping():
    for code, kind, text in ((1, "exception", "niedozwolona funkcja"), (2, "exception", "niedozwolony adres"),
                             (3, "exception", "niedozwolona wartość"), (4, "exception", "błąd urządzenia"),
                             (6, "exception", "zajęte"), (0x0A, "connection", "bramka"),
                             (0x0B, "timeout", "nie odpowiada")):
        e = T.exception_error(code)
        assert (e.kind, e.code) == (kind, code) and text in str(e)
    assert str(T.exception_error(2)) == "Wyjątek Modbus 02: niedozwolony adres"
    assert str(T.exception_error(0x0B)).startswith("Wyjątek Modbus 0B")


def test_pymodbus_api_detected():
    info = T.pymodbus_info()
    assert info["version"] == T.pymodbus_version()
    from pymodbus.client import ModbusTcpClient
    assert info["unit_kw"] in inspect.signature(ModbusTcpClient.read_holding_registers).parameters
    assert info["client_retries"] in (0, 1)
    ports = T.list_serial_ports()
    assert isinstance(ports, list) and all({"device", "description", "hwid"} <= set(p) for p in ports)


# ── TCP ───────────────────────────────────────────────────────
def test_tcp_read_write(server):
    bus = tcp_bus(server)
    try:
        assert bus.read_registers(1, "input", 0, 8) == EXPECTED
        assert bus.read_registers(1, "input", 4, 2) == codec.encode(50.0)
        assert bus.stats()["connected"]
        assert bus.read_registers(1, "holding", 0, 3) == [0, 0, 0]
        bus.write_register(1, 1, 0xBEEF)
        bus.write_registers(1, 10, [1, 2, -1])
        assert bus.read_registers(1, "holding", 0, 2) == [0, 0xBEEF]
        assert bus.read_registers(1, "holding", 10, 3) == [1, 2, 0xFFFF]
        bus.write_coil(1, 5, True)
        assert bus.read_bits(1, "coil", 0, 10) == [False] * 5 + [True] + [False] * 4
        assert bus.read_bits(1, "discrete", 0, 3) == [False] * 3
        assert bus.read_registers(1, "input", 0, 125)[:8] == EXPECTED
        st = bus.stats()
        assert st["requests"] == 11 and st["errors"] == 0 and st["connects"] == 1  # jedno połączenie
        assert st["avg_ms"] is not None and st["last_ok_ts"]
    finally:
        bus.close()
    assert not bus.stats()["connected"]


def test_exception_responses(server):
    bus = tcp_bus(server)
    try:
        assert bus.read_registers(2, "input", 0, 8) == EXPECTED  # strict, ale zakres zmapowany
        with pytest.raises(ModbusError) as e:
            bus.read_registers(2, "input", 8, 2)
        assert (e.value.kind, e.value.code) == ("exception", 2)
        assert str(e.value) == "Wyjątek Modbus 02: niedozwolony adres"
        with pytest.raises(ModbusError) as e:
            bus.read_bits(2, "coil", 0, 1)  # strict: funkcje bitowe -> 01
        assert (e.value.kind, e.value.code) == ("exception", 1)
        assert bus.read_registers(2, "input", 2, 2) == codec.encode(1.25)  # łącze dalej sprawne
        st = bus.stats()
        assert st["exceptions"] == 2 and st["errors"] == 0 and st["retries"] == 0 and st["connects"] == 1
    finally:
        bus.close()


def test_invalid_requests_are_rejected_without_io(server):
    bus = tcp_bus(server)
    rtu = Bus(TransportConfig(kind="rtu_over_tcp", host="127.0.0.1", port=server.address[1]))
    cases = [(bus.read_registers, (1, "input", 0, 0)), (bus.read_registers, (1, "input", 0, 126)),
             (bus.read_registers, (1, "input", 65535, 2)), (bus.read_registers, (1, "inputs", 0, 1)),
             (bus.read_registers, (256, "input", 0, 1)), (bus.read_registers, (True, "input", 0, 1)),
             (bus.read_bits, (1, "coil", 0, 2001)), (bus.read_bits, (1, "holding", 0, 1)),
             (bus.write_register, (1, 0, 65536)), (bus.write_registers, (1, 0, [])),
             (bus.write_registers, (1, 0, [1] * 124)), (bus.write_registers, (1, 65535, [1, 2])),
             (rtu.read_registers, (0, "input", 0, 1)), (rtu.read_registers, (248, "input", 0, 1))]
    for fn, args in cases:
        with pytest.raises(T.InvalidRequest) as e:
            fn(*args)
        assert isinstance(e.value, ValueError) and e.value.kind == "invalid", args
    assert bus.stats()["requests"] == 0 and not bus.stats()["connected"]
    assert bus.read_registers(0, "input", 0, 2) == EXPECTED[:2]  # unit 0 w TCP: dowolne urządzenie


def test_timeout_respects_retries(server, caplog):
    bus = tcp_bus(server, timeout=0.3, retries=1)
    try:
        t0 = time.monotonic()
        with pytest.raises(ModbusError) as e:
            bus.read_registers(9, "input", 0, 2)  # brak takiego urządzenia - cisza
        dt = time.monotonic() - t0
        assert e.value.kind == "timeout" and "timeout" in str(e.value) and "Unit ID 9" in str(e.value)
        assert 0.55 <= dt < 1.2, dt  # 2 próby po 0.3 s, bez wewnętrznych ponowień pymodbus
        st = bus.stats()
        assert (st["errors"], st["timeouts"], st["retries"]) == (1, 1, 1) and "timeout" in st["last_error"]
        assert bus.read_registers(1, "input", 0, 2) == EXPECTED[:2]  # urządzenie obok działa
        bus0 = tcp_bus(server, timeout=0.3, retries=0)
        t0 = time.monotonic()
        with pytest.raises(ModbusError):
            bus0.read_registers(9, "input", 0, 2)
        assert time.monotonic() - t0 < 0.55
        bus0.close()
    finally:
        bus.close()
    assert not [r for r in caplog.records if r.name.startswith("pymodbus")]  # wyciszone logi pymodbus


def test_gateway_exception_counts_as_timeout():
    srv = S.SimServer("127.0.0.1", 0, "tcp", gateway_errors=True)
    srv.add_device(make_device(1))
    srv.start()
    bus = tcp_bus(srv, timeout=1.0, retries=1)
    try:
        t0 = time.monotonic()
        with pytest.raises(ModbusError) as e:
            bus.read_registers(7, "input", 0, 2)
        assert (e.value.kind, e.value.code) == ("timeout", 0x0B) and time.monotonic() - t0 < 0.5
        assert bus.stats()["retries"] == 1
        assert bus.read_registers(1, "input", 0, 2) == EXPECTED[:2]
    finally:
        bus.close()
        srv.stop()


def test_connection_refused():
    bus = Bus(TransportConfig(host="127.0.0.1", port=_free_port(), timeout=0.5, retries=3))
    t0 = time.monotonic()
    with pytest.raises(ModbusError) as e:
        bus.read_registers(1, "input", 0, 2)
    assert e.value.kind == "connection" and "odrzucone" in str(e.value)
    assert time.monotonic() - t0 < 0.5  # bez ponowień przy nieudanym łączeniu
    res = bus.ping()
    assert not res["ok"] and res["kind"] == "connection" and res["ms"] >= 0
    assert bus.stats()["errors"] == 1


def test_reconnect_after_server_restart():
    srv = S.SimServer("127.0.0.1", 0, "tcp")
    srv.add_device(make_device(1))
    srv.start()
    port = srv.address[1]
    bus = Bus(TransportConfig(host="127.0.0.1", port=port, timeout=0.5, retries=0))
    try:
        assert bus.read_registers(1, "input", 0, 2) == EXPECTED[:2]
        srv.stop()
        with pytest.raises(ModbusError) as e:
            bus.read_registers(1, "input", 0, 2)
        assert e.value.kind == "connection"
        assert not bus.connected
        srv = S.SimServer("127.0.0.1", port, "tcp")
        srv.add_device(make_device(1))
        srv.start()
        assert bus.read_registers(1, "input", 0, 2) == EXPECTED[:2]
        # serwer zamyka bezczynne połączenie - jedna darmowa próba, mimo retries=0
        for s in list(srv._conns):
            s.shutdown(socket.SHUT_RDWR)
        time.sleep(0.1)
        assert bus.read_registers(1, "input", 2, 2) == EXPECTED[2:4]
        assert bus.stats()["connects"] == 3
    finally:
        bus.close()
        srv.stop()


def test_concurrent_reads_one_bus(server):
    bus = tcp_bus(server, timeout=2.0)
    errors, done = [], []

    def worker(i):
        try:
            for n in range(25):
                addr = (i + n) % 4 * 2
                assert bus.read_registers(1, "input", addr, 2) == EXPECTED[addr:addr + 2]
                if n % 5 == 0:
                    with pytest.raises(ModbusError):
                        bus.read_registers(2, "input", 50, 1)
            done.append(i)
        except Exception as e:  # noqa: BLE001
            errors.append(repr(e))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    bus.close()
    assert not errors and len(done) == 8
    st = bus.stats()
    assert st["requests"] == 8 * 30 and st["errors"] == 0 and st["connects"] == 1


def test_override_is_thread_local(server):
    bus = tcp_bus(server, timeout=1.0, retries=2)
    seen = {}

    def other():
        seen["other"] = bus._settings()

    with bus.override(timeout=0.2, retries=0):
        assert bus._settings() == (0.2, 0)
        th = threading.Thread(target=other)
        th.start()
        th.join()
        t0 = time.monotonic()
        with pytest.raises(ModbusError):
            bus.read_registers(9, "input", 0, 1)
        assert time.monotonic() - t0 < 0.5
    assert seen["other"] == (1.0, 2) and bus._settings() == (1.0, 2)
    assert bus.read_registers(1, "input", 0, 2) == EXPECTED[:2]
    bus.close()


def test_close_interrupts_request_in_flight(server, monkeypatch):
    monkeypatch.setattr(T, "CLOSE_WAIT", 0.2)
    bus = tcp_bus(server, timeout=5.0, retries=2)
    bus.read_registers(1, "input", 0, 2)
    res = {}

    def silent():
        t0 = time.monotonic()
        try:
            bus.read_registers(9, "input", 0, 2)
        except ModbusError as e:
            res["err"] = e
        res["dt"] = time.monotonic() - t0

    th = threading.Thread(target=silent)
    th.start()
    time.sleep(0.2)
    bus.close()  # nie czeka 5 s na odpowiedź, przerywa zapytanie
    th.join(5)
    assert res["err"].kind == "connection" and res["dt"] < 1.5, res
    assert bus.read_registers(1, "input", 0, 2) == EXPECTED[:2]  # kolejne zapytanie łączy się od nowa
    bus.close()


def test_ping(server):
    bus = tcp_bus(server, timeout=0.3, retries=0)
    try:
        assert bus.ping()["ok"] and bus.ping(1)["ok"]
        assert bus.ping(2)["ok"]  # strict: wyjątek 02 też oznacza, że urządzenie żyje
        res = bus.ping("9")
        assert not res["ok"] and res["kind"] == "timeout" and "Unit ID 9" in res["error"]
        res = bus.ping("abc")
        assert not res["ok"] and res["kind"] == "invalid"
    finally:
        bus.close()


def test_delay_between_frames(server):
    bus = tcp_bus(server, delay_ms=100)
    try:
        bus.read_registers(1, "input", 0, 2)
        t0 = time.monotonic()
        for _ in range(3):
            bus.read_registers(1, "input", 0, 2)
        assert time.monotonic() - t0 >= 0.29
    finally:
        bus.close()


def test_rtu_over_tcp():
    srv = S.SimServer("127.0.0.1", 0, "rtu")
    srv.add_device(make_device(1))
    srv.add_device(make_device(2, strict=True))
    srv.start()
    bus = tcp_bus(srv, kind="rtu_over_tcp", timeout=0.4, retries=1)
    try:
        assert bus.read_registers(1, "input", 0, 8) == EXPECTED
        bus.write_registers(1, 0, [7, 8])
        assert bus.read_registers(1, "holding", 0, 2) == [7, 8]
        with pytest.raises(ModbusError) as e:
            bus.read_registers(2, "input", 90, 2)
        assert (e.value.kind, e.value.code) == ("exception", 2)
        t0 = time.monotonic()
        with pytest.raises(ModbusError) as e:
            bus.read_registers(5, "input", 0, 2)
        assert e.value.kind == "timeout" and time.monotonic() - t0 < 1.3
        assert bus.read_registers(1, "input", 6, 2) == EXPECTED[6:8]
    finally:
        bus.close()
        srv.stop()


class UdpSim:
    """Modbus UDP (MBAP w datagramach) na urządzeniach symulatora."""

    def __init__(self, devices):
        self.base = S.SimServer("127.0.0.1", 0, devices=devices)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(0.1)
        self.address = self.sock.getsockname()
        self.running = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        while self.running:
            try:
                data, peer = self.sock.recvfrom(512)
            except OSError:
                continue
            tid, _, length, unit = struct.unpack(">HHHB", data[:7])
            resp = self.base.process_pdu(unit, data[7:6 + length], tcp=True)
            if resp:
                self.sock.sendto(struct.pack(">HHHB", tid, 0, len(resp) + 1, unit) + resp, peer)

    def stop(self):
        self.running = False
        self.thread.join(2)
        self.sock.close()


def test_udp():
    sim = UdpSim({1: make_device(1)})
    host, port = sim.address
    bus = Bus(TransportConfig(kind="udp", host=host, port=port, timeout=0.3, retries=0))
    try:
        assert bus.ping()["ok"]
        assert bus.read_registers(1, "input", 0, 8) == EXPECTED
        t0 = time.monotonic()
        with pytest.raises(ModbusError) as e:
            bus.read_registers(4, "input", 0, 2)
        assert e.value.kind == "timeout" and time.monotonic() - t0 < 0.9
        assert bus.read_registers(1, "input", 2, 2) == EXPECTED[2:4]
    finally:
        bus.close()
        sim.stop()


# ── BusManager ────────────────────────────────────────────────
def test_bus_manager_identity_and_replacement(server):
    mgr = BusManager()
    host, port = server.address
    cfg = TransportConfig(host=host, port=port)
    bus = mgr.get(cfg)
    assert mgr.get(TransportConfig.from_dict(cfg.to_dict())) is bus
    assert mgr.get(cfg.to_dict()) is bus
    assert bus.read_registers(1, "input", 0, 2) == EXPECTED[:2]
    # timeout/retries/delay zmieniają się w locie, bez zrywania połączenia
    soft = TransportConfig(host=host, port=port, timeout=0.5, retries=0, delay_ms=5)
    assert mgr.get(soft) is bus and bus.cfg == soft and bus.connected
    # inny rodzaj ramek na tym samym adresie to osobne łącze
    other = mgr.get(TransportConfig(kind="rtu_over_tcp", host=host, port=port))
    assert other is not bus
    # zmiana parametrów łącza -> nowa instancja, stara zamknięta
    s1 = mgr.get(TransportConfig(kind="rtu", serial_port="/dev/ttyNOPE0"))
    s2 = mgr.get(TransportConfig(kind="ascii", serial_port="/dev/ttyNOPE0", baudrate=19200))
    assert s2 is not s1 and s1._retired and s2.cfg.kind == "ascii"
    with pytest.raises(ModbusError) as e:
        s1.read_registers(1, "input", 0, 1)
    assert e.value.kind == "connection" and "zamknięta" in str(e.value)
    with pytest.raises(ModbusError) as e:
        s2.read_registers(1, "input", 0, 1)
    assert e.value.kind == "connection" and "nie istnieje" in str(e.value)
    snap = {s["key"]: s for s in mgr.snapshot()}
    assert set(snap) == {f"tcp:{host}:{port}", f"rtu_over_tcp:{host}:{port}", "serial:/dev/ttyNOPE0"}
    assert snap[f"tcp:{host}:{port}"]["stats"]["requests"] == 1
    assert snap["serial:/dev/ttyNOPE0"]["describe"].startswith("RS-485 ASCII")
    mgr.close_all()
    assert not bus.connected and mgr.snapshot() == []
    assert mgr.get(cfg) is not bus


def test_bus_manager_closes_idle(server, monkeypatch):
    mgr = BusManager()
    host, port = server.address
    a = mgr.get(TransportConfig(host=host, port=port))
    a.read_registers(1, "input", 0, 2)
    b = mgr.get(TransportConfig(kind="udp", host=host, port=port))
    monkeypatch.setattr(T, "IDLE_CLOSE", 0.0)
    mgr.get(b.cfg)
    assert not a.connected and not a._retired  # tylko zamknięte - otworzy się przy kolejnym zapytaniu
    monkeypatch.setattr(T, "IDLE_DROP", 0.0)
    time.sleep(0.01)
    mgr.get(b.cfg)
    assert a._retired and len(mgr.snapshot()) == 1
    mgr.close_all()


# ── port szeregowy (pty) ──────────────────────────────────────
linux_pty = pytest.mark.skipif(
    not sys.platform.startswith("linux") or not hasattr(os, "openpty"), reason="pty tylko w Linuksie")


@pytest.fixture
def pty_pair():
    pytest.importorskip("serial")
    master, slave, path = S.make_pty()
    yield master, path
    os.close(slave)
    os.close(master)


def serial_bus(path, **kw):
    return Bus(TransportConfig(kind=kw.pop("kind", "rtu"), serial_port=path, baudrate=kw.pop("baudrate", 38400),
                               **kw))


@linux_pty
def test_serial_rtu(pty_pair):
    master, path = pty_pair
    srv = S.SerialSimServer(master)
    srv.add_device(make_device(1))
    srv.add_device(make_device(2, strict=True))
    srv.start()
    bus = serial_bus(path, timeout=0.3, retries=1)
    try:
        assert bus.ping()["ok"]
        assert bus.read_registers(1, "input", 0, 8) == EXPECTED
        bus.write_register(1, 2, 513)
        assert bus.read_registers(1, "holding", 2, 1) == [513]
        with pytest.raises(ModbusError) as e:
            bus.read_registers(2, "input", 20, 4)
        assert (e.value.kind, e.value.code) == ("exception", 2)
        t0 = time.monotonic()
        with pytest.raises(ModbusError) as e:
            bus.read_registers(7, "input", 0, 2)
        dt = time.monotonic() - t0
        assert e.value.kind == "timeout" and dt < 0.3 * 2 * 2 + 0.3, dt
        assert bus.read_registers(1, "input", 2, 6) == EXPECTED[2:]
        legacy = T.pymodbus_info()["client_retries"] == 1  # 3.6/3.7 same zamykają port po timeoucie
        assert bus.stats()["connects"] == 1 or legacy  # port zostaje otwarty mimo błędów
    finally:
        bus.close()
        srv.stop()


@linux_pty
def test_serial_late_response_is_flushed(pty_pair):
    master, path = pty_pair
    srv = S.SerialSimServer(master)
    srv.add_device(make_device(1))
    srv.start()
    bus = serial_bus(path, timeout=0.2, retries=1)
    try:
        srv.response_delay = 0.5  # dłużej niż timeout (3.6/3.7 czekają timeout + czas ramki)
        with bus.override(retries=0), pytest.raises(ModbusError) as e:
            bus.read_registers(1, "input", 0, 8)
        assert e.value.kind == "timeout"
        srv.response_delay = 0.0
        # spóźniona odpowiedź (8 rejestrów) nie może zostać wzięta za odpowiedź na nowe zapytanie
        assert bus.read_registers(1, "input", 2, 2) == EXPECTED[2:4]
        assert bus.read_registers(1, "input", 4, 4) == EXPECTED[4:8]
    finally:
        bus.close()
        srv.stop()


def _ascii_slave(fd, devices, stop):
    base = S.SerialSimServer(fd, devices=devices)
    buf = b""
    while not stop.is_set():
        r, _, _ = select.select([fd], [], [], 0.05)
        if not r:
            continue
        try:
            buf += os.read(fd, 512)
        except OSError:
            return
        while b"\r\n" in buf:
            line, buf = buf.split(b"\r\n", 1)
            raw = bytes.fromhex(line[line.find(b":") + 1:].decode())
            if (-sum(raw[:-1])) & 0xFF != raw[-1]:
                continue
            resp = base.process_pdu(raw[0], raw[1:-1], tcp=False)
            if resp:
                body = bytes([raw[0]]) + resp
                os.write(fd, b":" + (body + bytes([(-sum(body)) & 0xFF])).hex().upper().encode() + b"\r\n")


def _start_ascii(master, devices):
    stop = threading.Event()
    th = threading.Thread(target=_ascii_slave, args=(master, devices, stop), daemon=True)
    th.start()
    return stop, th


@linux_pty
def test_serial_ascii(pty_pair):
    master, path = pty_pair
    stop, th = _start_ascii(master, {3: make_device(3)})
    bus = serial_bus(path, kind="ascii", timeout=0.3, retries=0)
    try:
        assert bus.read_registers(3, "input", 0, 8) == EXPECTED
        bus.write_registers(3, 0, [11, 12, 13])
        assert bus.read_registers(3, "holding", 0, 3) == [11, 12, 13]
        with pytest.raises(ModbusError) as e:
            bus.read_registers(4, "input", 0, 2)
        assert e.value.kind == "timeout"
        assert bus.read_registers(3, "input", 6, 2) == EXPECTED[6:8]
    finally:
        bus.close()
        stop.set()
        th.join(2)


@linux_pty
def test_serial_ascii_7e1(pty_pair):
    # pty w Linuksie nie pozwala ponownie otworzyć portu z ustawieniami innymi niż 8N1 - bez timeoutów
    master, path = pty_pair
    stop, th = _start_ascii(master, {1: make_device(1)})
    bus = serial_bus(path, kind="ascii", bytesize=7, parity="E", baudrate=9600, timeout=0.5)
    try:
        assert bus.read_registers(1, "input", 0, 8) == EXPECTED
        assert bus.read_registers(1, "input", 0, 125)[100:102] == codec.encode(4321.5)
    finally:
        bus.close()
        stop.set()
        th.join(2)


@linux_pty
def test_serial_port_errors(tmp_path):
    pytest.importorskip("serial")
    bus = serial_bus("/dev/ttyNOPE0", timeout=0.2)
    with pytest.raises(ModbusError) as e:
        bus.read_registers(1, "input", 0, 1)
    assert e.value.kind == "connection" and "nie istnieje" in str(e.value)
    assert not bus.ping()["ok"]
    master, slave, path = S.make_pty()
    try:
        first = serial_bus(path, timeout=0.2)
        assert first.ping()["ok"]
        second = serial_bus(path, timeout=0.2)  # port otwarty na wyłączność
        res = second.ping()
        assert not res["ok"] and "zajęty" in res["error"]
        first.close()
        assert second.ping()["ok"]
        second.close()
    finally:
        os.close(slave)
        os.close(master)


def test_pymodbus_logger_is_quiet():
    T._pm()
    assert logging.getLogger("pymodbus").getEffectiveLevel() >= logging.WARNING


def _echo_rtu_slave(fd, devices, stop):
    """Slave RTU na pty, który - jak adapter z lokalnym echem - najpierw odsyła zapytanie."""
    base = S.SerialSimServer(fd)
    for dev in devices:
        base.add_device(dev)
    buf = b""
    while not stop.is_set():
        r, _, _ = select.select([fd], [], [], 0.02)
        if not r:
            continue
        try:
            buf += os.read(fd, 512)
        except OSError:
            return
        while buf:
            n = S.rtu_request_length(buf)
            if not n or len(buf) < n:
                break
            frame, buf = buf[:n], buf[n:]
            os.write(fd, frame)              # echo własnej transmisji
            time.sleep(0.005)
            resp = base.process_rtu_frame(frame)
            if resp:
                os.write(fd, resp)


@linux_pty
def test_serial_local_echo(pty_pair):
    master, path = pty_pair
    stop = threading.Event()
    th = threading.Thread(target=_echo_rtu_slave, args=(master, [make_device(1)], stop), daemon=True)
    th.start()
    try:
        bus = serial_bus(path, timeout=0.3, retries=0, local_echo=True)
        try:
            assert bus.read_registers(1, "input", 0, 8) == EXPECTED
            assert bus.read_registers(1, "input", 2, 2) == EXPECTED[2:4]
            assert "(echo)" in bus.cfg.describe()
        finally:
            bus.close()
        # bez opcji echo: błąd, nigdy cicha pusta odpowiedź
        bus = serial_bus(path, timeout=0.3, retries=0)
        try:
            with pytest.raises(ModbusError):
                bus.read_registers(1, "input", 0, 8)
        finally:
            bus.close()
    finally:
        stop.set()
        th.join(timeout=2)


def test_local_echo_config_parsing():
    assert TransportConfig.from_dict({"kind": "rtu", "serial_port": "/dev/x", "local_echo": "true"}).local_echo
    assert not TransportConfig.from_dict({"kind": "tcp", "host": "h", "local_echo": True}).local_echo
    with pytest.raises(ValueError):
        TransportConfig(kind="rtu", serial_port="/dev/x", local_echo="tak")


def test_mini_uart_parity_is_reported(monkeypatch, tmp_path):
    import builtins
    from modbus_dash import transport as T
    real_open = builtins.open

    def fake_open(path, *a, **kw):
        if path == "/proc/device-tree/model":
            from io import BytesIO
            return BytesIO(b"Raspberry Pi 4 Model B Rev 1.4\0")
        return real_open(path, *a, **kw)

    monkeypatch.setattr(builtins, "open", fake_open)
    monkeypatch.setattr(T.os.path, "realpath", lambda p: "/dev/ttyS0")
    cfg = TransportConfig(kind="rtu", serial_port="/dev/serial0", parity="E")
    msg = T._mini_uart_problem("/dev/serial0", cfg)
    assert msg and "disable-bt" in msg and "8E1" in msg
    assert T._mini_uart_problem("/dev/serial0", TransportConfig(kind="rtu", serial_port="/dev/serial0")) is None
