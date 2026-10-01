"""Testy pollera, historii, metryk i skanera (na magistrali w pamięci)."""

import time

import pytest

from conftest import FakeBus
from modbus_dash import codec, metrics
from modbus_dash.history import HistoryDB
from modbus_dash.planner import PresetReader
from modbus_dash.poller import Poller
from modbus_dash.presets import PresetStore
from modbus_dash.scanner import JobManager, read_range, scan_units


class FakeTC:
    @staticmethod
    def from_dict(d):
        return d


class FakeManager:
    def __init__(self, bus):
        self.bus = bus

    def get(self, cfg):
        return self.bus


class FakeConfig:
    def __init__(self, devices, points=100):
        self.data = {"buses": {"default": {"name": "x", "kind": "tcp"}}, "devices": devices,
                     "history": {"memory_points": points}, "mqtt": {}}

    def get(self):
        return self.data


@pytest.fixture
def setup(tmp_path):
    store = PresetStore(tmp_path)
    store.save("m", {"name": "Miernik", "manufacturer": "Test", "registers": {
        "voltage_l1": {"address": 0, "unit": "V", "decimals": 1, "group": "voltage", "label": "Napięcie L1"},
        "frequency": {"address": 2, "unit": "Hz", "decimals": 2, "group": "system", "label": "Częstotliwość"},
    }})
    bus = FakeBus()
    bus.put("input", 0, codec.encode(231.2, "float32"))
    bus.put("input", 2, codec.encode(50.01, "float32"))
    cfg = FakeConfig({"d1": {"name": "Licznik", "bus": "default", "unit": 1, "preset": "m",
                             "interval": 0.2, "enabled": True}})
    poller = Poller(cfg, store, FakeManager(bus), PresetReader, FakeTC)
    return store, bus, cfg, poller


def test_poller_read_now_and_values(setup):
    store, bus, cfg, poller = setup
    poller.reload()
    sample = poller.read_now("d1")
    assert sample["ok"]
    data = poller.values("d1")
    assert data["values"] == {"voltage_l1": 231.2, "frequency": 50.01}
    assert data["meta"]["voltage_l1"]["unit"] == "V"
    assert data["status"]["state"] == "ok"
    hist = poller.history("d1", 60)
    assert hist["keys"] == ["voltage_l1", "frequency"] and len(hist["points"]) == 1
    assert poller.history("d1", 60, ["frequency"])["points"][0][1:] == [50.01]


def test_poller_background_thread_and_listener(setup):
    store, bus, cfg, poller = setup
    seen = []
    poller.add_listener(lambda dev, rt, s: seen.append(s["ok"]))
    poller.start()
    try:
        deadline = time.time() + 3
        while len(seen) < 3 and time.time() < deadline:
            time.sleep(0.05)
    finally:
        poller.stop()
    assert len(seen) >= 3 and all(seen)


def test_poller_errors_and_status(setup):
    store, bus, cfg, poller = setup
    bus.silent_units.add(1)
    poller.reload()
    s = poller.read_now("d1")
    assert not s["ok"] and "timeout" in s["error"]
    assert poller.values("d1")["status"]["state"] == "error"
    cfg.data["devices"]["d1"]["preset"] = "brak"
    poller.reload()
    st = poller.values("d1")["status"]
    assert st["state"] == "no_preset" and "brak" in st["error"]


def test_poller_preset_change_resets_history(setup):
    store, bus, cfg, poller = setup
    poller.reload()
    poller.read_now("d1")
    store.save("m", {"name": "Miernik", "registers": {"voltage_l1": {"address": 0}}})
    poller.read_now("d1")
    assert poller.history("d1", 60)["keys"] == ["voltage_l1"]


def test_metrics_render(setup):
    store, bus, cfg, poller = setup
    poller.reload()
    poller.read_now("d1")
    text = metrics.render(poller)
    assert 'modbus_dash_value{device="d1",device_name="Licznik",key="voltage_l1"' in text
    assert "modbus_dash_up{" in text and text.endswith("\n")


def test_history_aggregation_and_query(tmp_path):
    db = HistoryDB(tmp_path / "h.sqlite", bucket_seconds=60, retention_days=1)
    base = (int(time.time()) // 60 - 3) * 60
    for i, v in enumerate([1.0, 3.0, 10.0, 20.0]):
        ts = base + (0 if i < 2 else 60) + i
        db.on_sample("d", None, {"ok": True, "ts": ts, "values": {"p": v, "q": None}})
    db.on_sample("d", None, {"ok": False, "ts": base + 130, "values": {"p": 999}})
    res = db.query("d", ["p"], base - 10)
    assert res["keys"] == ["p"]
    assert [row[1] for row in res["points"]] == [2.0, 15.0]
    db.close()


def test_history_downsampling(tmp_path):
    db = HistoryDB(tmp_path / "h.sqlite", bucket_seconds=10)
    base = int(time.time()) - 5000
    base -= base % 10
    for i in range(400):
        db.on_sample("d", None, {"ok": True, "ts": base + i * 10, "values": {"p": float(i)}})
    res = db.query("d", ["p"], base, max_points=50)
    assert len(res["points"]) <= 50 and res["bucket"] >= 80
    db.close()


def test_read_range_skips_holes():
    bus = FakeBus(strict=True)
    bus.put("input", 0, [1, 2, 3, 4])
    bus.put("input", 10, [5, 6])
    vals, err, req, _, covered = read_range(bus, 1, "input", 0, 12)
    assert err is None
    assert vals == [1, 2, 3, 4, None, None, None, None, None, None, 5, 6]


def test_read_range_stops_on_timeout():
    bus = FakeBus(silent_units={3})
    vals, err, req, _, _ = read_range(bus, 3, "input", 0, 300)
    assert "timeout" in err and req == 1 and all(v is None for v in vals)


def test_scan_units_job():
    bus = FakeBus(strict=False, silent_units=set(range(1, 248)) - {5, 9})
    jobs = JobManager()
    job = jobs.start("units", scan_units, bus, 1, 12, "input", 0, 1, 0.1)
    deadline = time.time() + 3
    while job.state == "running" and time.time() < deadline:
        time.sleep(0.02)
    assert job.state == "done"
    assert [f["unit"] for f in job.result["found"]] == [5, 9]
    with pytest.raises(RuntimeError):
        jobs.start("x", lambda j: time.sleep(0.3))
        jobs.start("x", lambda j: None)


def test_scan_reports_exception_and_stops_on_illegal_function():
    from conftest import FakeModbusError
    from modbus_dash.scanner import scan

    class Fc1Bus(FakeBus):
        def read_registers(self, unit, function, address, count):
            self.calls.append((unit, function, address, count))
            raise FakeModbusError("exception", 1, "Wyjątek Modbus 01: niedozwolona funkcja")

    bus = Fc1Bus()
    res = scan(bus, 1, "holding", 0, 200)
    assert res["readable"] == 0 and res["exception_code"] == 1 and "nie obsługuje" in res["error"]
    assert len(bus.calls) == 1


def test_scan_all_rejected_reports_last_exception():
    from modbus_dash.scanner import scan
    bus = FakeBus(strict=True)
    res = scan(bus, 1, "input", 0, 4)
    assert res["readable"] == 0 and "odrzucone" in res["error"] and res["exception_code"] == 2


def test_cancelled_job_does_not_block_new_one():
    jobs = JobManager()
    ev = __import__("threading").Event()
    job = jobs.start("units", lambda j: ev.wait(2))
    jobs.cancel(job.id)
    job2 = jobs.start("units", lambda j: None)
    assert job2.id != job.id
    ev.set()


# ── regresje z przeglądu ──────────────────────────────────────
def test_history_bucket_continues_after_restart(tmp_path):
    path = tmp_path / "h.sqlite"
    b = (int(time.time()) // 900 - 1) * 900
    db = HistoryDB(path, bucket_seconds=900)
    for i, v in enumerate([10.0, 20.0, 30.0]):
        db.on_sample("d", None, {"ok": True, "ts": b + i, "values": {"p": v}})
    db.close()  # SIGTERM w połowie przedziału
    db = HistoryDB(path, bucket_seconds=900)
    db._seed_before = b + 900  # przedział zaczął się przed "startem"
    for i, v in enumerate([40.0, 50.0]):
        db.on_sample("d", None, {"ok": True, "ts": b + 10 + i, "values": {"p": v}})
    db.flush()
    row = db._db.execute("SELECT avg, min, max, n FROM samples WHERE device='d' AND key='p'").fetchone()
    assert row == (30.0, 10.0, 50.0, 5)
    db.close()


def test_history_query_does_not_block_samples(tmp_path, monkeypatch):
    import threading
    db = HistoryDB(tmp_path / "h.sqlite", bucket_seconds=10)
    db.on_sample("d", None, {"ok": True, "ts": time.time(), "values": {"p": 1.0}})
    started, release = threading.Event(), threading.Event()
    real = db._rdb

    class SlowConn:
        def execute(self, *a):
            started.set()
            release.wait(2)
            return real.execute(*a)
    db._rdb = SlowConn()
    th = threading.Thread(target=db.query, args=("d", ["p"], time.time() - 60))
    th.start()
    assert started.wait(2)
    t0 = time.monotonic()
    db.on_sample("d", None, {"ok": True, "ts": time.time() + 20, "values": {"p": 2.0}})
    assert time.monotonic() - t0 < 0.5  # zapis próbki nie czeka na trwający odczyt
    release.set()
    th.join(2)
    db._rdb = real
    db.close()


def test_backoff_never_shortens_interval(setup):
    store, bus, cfg, poller = setup
    cfg.data["devices"]["d1"]["interval"] = 600.0
    bus.silent_units.add(1)
    poller.reload()
    rt = poller.runtime("d1")
    for _ in range(4):
        t0 = time.monotonic()
        poller.poll(rt)
        assert rt.next_due - t0 >= 599
    cfg.data["devices"]["d1"]["interval"] = 5.0
    poller.reload()
    rt = poller.runtime("d1")
    rt.fail_count = 0
    delays = []
    for _ in range(5):
        t0 = time.monotonic()
        poller.poll(rt)
        delays.append(round(rt.next_due - t0))
    assert delays[0] == 5 and delays[1:] == [10, 20, 30, 30]


def test_broken_preset_keeps_history_and_notifies(setup):
    store, bus, cfg, poller = setup
    seen = []
    poller.add_preset_listener(seen.append)
    poller.reload()
    for _ in range(3):
        poller.read_now("d1")
    assert len(poller.history("d1", 60)["points"]) == 3 and seen == ["d1"]
    path = store.user_dir / "m.json"
    good = path.read_text(encoding="utf-8")
    path.write_text(good[:20], encoding="utf-8")
    import os
    os.utime(path, ns=(time.time_ns() + 10**9, time.time_ns() + 10**9))
    assert poller.read_now("d1") is None
    st = poller.values("d1")
    assert st["status"]["state"] == "no_preset" and st["meta"] and seen == ["d1", "d1"]
    assert len(poller.history("d1", 60)["points"]) == 3  # bufor nie zniknął
    path.write_text(good, encoding="utf-8")
    os.utime(path, ns=(time.time_ns() + 2 * 10**9, time.time_ns() + 2 * 10**9))
    assert poller.read_now("d1")["ok"]
    assert len(poller.history("d1", 60)["points"]) == 4 and seen == ["d1", "d1", "d1"]


def test_config_store_types_and_listeners_outside_lock(tmp_path):
    import threading
    from modbus_dash.config import ConfigError, ConfigStore
    store = ConfigStore(tmp_path / "c.json", overrides={"buses": {"cli": {"kind": "tcp", "host": "127.0.0.1"}}})
    with pytest.raises(ConfigError):
        store.put_section("history", {"memory_points": 3600.5})
    assert store.put_section("history", {"memory_points": 7200.0})["memory_points"] == 7200
    with pytest.raises(ConfigError):
        store.delete_bus("cli")
    with pytest.raises(ConfigError):
        store.put_device("x", {"bus": "default", "enabled": "false"})
    assert store.put_device("x", {"bus": "default", "enabled": None})["enabled"] is True
    # słuchacz nie trzyma blokady konfiguracji - odczyt z innego wątku nie czeka
    got = []

    def slow(section):
        th = threading.Thread(target=lambda: got.append(store.get()["mqtt"]["interval"]))
        th.start()
        th.join(1)
    store.on_change(slow)
    store.put_section("mqtt", {"interval": 7})
    assert got == [7]
    # wartość spoza zakresu w pliku nie blokuje startu
    import json as _json
    data = _json.loads((tmp_path / "c.json").read_text(encoding="utf-8"))
    data["history"]["memory_points"] = 5
    (tmp_path / "c.json").write_text(_json.dumps(data), encoding="utf-8")
    assert ConfigStore(tmp_path / "c.json").get()["history"]["memory_points"] == 3600
