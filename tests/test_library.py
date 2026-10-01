"""Testy biblioteki presetów wbudowanych (presets/library)."""

import json
import re

import pytest

from conftest import ROOT
from modbus_dash.planner import PresetReader
from modbus_dash.presets import normalize_preset, valid_id
from modbus_dash.quantities import QUANTITIES
from modbus_dash.simulator import Physics, SimDevice, phase_view, resolve_key, unit_factor

LIB = ROOT / "presets" / "library"
FILES = sorted(LIB.glob("*.json"))
IDS = [f.stem for f in FILES]


def load(f):
    return json.loads(f.read_text(encoding="utf-8"))


def test_library_not_empty():
    assert len(FILES) >= 20


@pytest.mark.parametrize("f", FILES, ids=IDS)
def test_preset_valid_and_documented(f):
    raw = load(f)
    p = normalize_preset(raw)
    assert valid_id(f.stem) and re.fullmatch(r"[a-z0-9_]+", f.stem), "nazwa pliku: małe litery, cyfry, _"
    assert raw.get("name") and raw.get("manufacturer") and raw.get("model")
    assert p["registers"], "preset bez rejestrów"
    if f.stem != "simulator_3f":
        assert raw.get("source"), "brak pola source (skąd mapa rejestrów)"
        assert raw.get("description")
        assert raw.get("phases") in (1, 3)
    if raw.get("serial"):
        s = raw["serial"]
        assert s.get("baudrate") in (1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200)
        assert s.get("parity") in ("N", "E", "O")
        assert s.get("stopbits") in (1, 2)
    if raw.get("probe"):
        assert raw["probe"] in p["registers"]
    text = f.read_text(encoding="utf-8")
    assert "—" not in text and "–" not in text, "bez długich myślników"


@pytest.mark.parametrize("f", FILES, ids=IDS)
def test_no_overlapping_registers(f):
    p = normalize_preset(load(f))
    used = {}
    for key, s in p["registers"].items():
        for a in range(s["address"], s["address"] + s["count"]):
            other = used.get((s["function"], a))
            assert other is None, f"{key} nakłada się z {other} pod adresem {a}"
            used[(s["function"], a)] = key


@pytest.mark.parametrize("f", [f for f in FILES if f.stem != "simulator_3f"], ids=[i for i in IDS if i != "simulator_3f"])
def test_canonical_keys_follow_vocabulary(f):
    raw = load(f)
    for key, spec in raw["registers"].items():
        if key in QUANTITIES:
            q = QUANTITIES[key]
            assert spec.get("unit", "") == q["unit"], f"{key}: jednostka {spec.get('unit')!r} zamiast {q['unit']!r}"
            assert spec.get("group", "other") == q["group"], f"{key}: grupa"


class _DeviceBus:
    def __init__(self, dev):
        self.dev = dev

    def read_registers(self, unit, function, address, count):
        return self.dev.read(function, address, count)


@pytest.mark.parametrize("f", FILES, ids=IDS)
def test_simulator_roundtrip(f):
    """Symulator koduje wartości fizyczne wg presetu; odczyt przez PresetReader musi je odtworzyć."""
    p = normalize_preset(load(f))
    dev = SimDevice(1, p)
    q = Physics(seed=1).tick(now=1000.0)
    dev.update(q)
    res = PresetReader(p).read(_DeviceBus(dev), 1)
    view = phase_view(q, p["phases"])
    checked = 0
    for key, spec in p["registers"].items():
        qkey = resolve_key(key)
        if qkey is None or view.get(qkey) is None:
            continue
        expected = view[qkey] * unit_factor(qkey, spec["unit"])
        got = res["values"][key]
        assert got is not None, f"{key}: brak wartości ({res['errors'].get(key)})"
        # tolerancja: rozdzielczość rejestru (scale) + zaokrąglenie do decimals + float32
        tol = abs(spec["scale"]) * 1.01 + 10 ** -spec["decimals"] + abs(expected) * 1e-6
        if spec["type"].startswith("uint") and expected < 0:
            continue  # typ bez znaku nie zapisze wartości ujemnej (np. moc przy oddawaniu)
        assert abs(got - expected) <= tol, f"{key}: {got} != {expected} (tol {tol})"
        checked += 1
    assert checked >= 1
