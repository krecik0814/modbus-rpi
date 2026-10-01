"""Testy kodeka, presetów i planera odczytów."""

import json
import math
import struct

import pytest

from conftest import FakeBus, ROOT
from modbus_dash import codec
from modbus_dash.planner import PresetReader, plan_reads, split_block
from modbus_dash.presets import PresetError, PresetStore, normalize_preset, slugify, valid_id


# ── codec ──────────────────────────────────────────────────────

@pytest.mark.parametrize("order", codec.BYTE_ORDERS)
@pytest.mark.parametrize("dtype", list(codec.DATA_TYPES))
def test_roundtrip_all_types_and_orders(dtype, order):
    if dtype.endswith("int8"):
        value = 123 if dtype.startswith("u") else -12
    else:
        value = -1234.5 if dtype.startswith("float") else (1234 if dtype.startswith("u") else -1234)
    regs = codec.encode(value, dtype, order)
    assert len(regs) == codec.register_count(dtype)
    assert codec.decode(regs, dtype, order) == value


def test_float32_known_layouts():
    # 230.0 = 0x43660000
    assert codec.encode(230.0, "float32", "ABCD") == [0x4366, 0x0000]
    assert codec.encode(230.0, "float32", "CDAB") == [0x0000, 0x4366]
    assert codec.encode(230.0, "float32", "BADC") == [0x6643, 0x0000]
    assert codec.encode(230.0, "float32", "DCBA") == [0x0000, 0x6643]
    # DCBA = czysty little-endian strumień bajtów
    b = struct.pack("<f", 230.0)
    assert codec.encode(230.0, "float32", "DCBA") == [b[0] << 8 | b[1], b[2] << 8 | b[3]]


def test_little_endian_differs_from_word_swap():
    regs = [0x4366, 0x0000]
    assert codec.decode(regs, "float32", "word_swap") != codec.decode(regs, "float32", "little_endian")
    assert codec.decode([0x0000, 0x6643], "float32", "little_endian") == 230.0


def test_aliases_and_errors():
    assert codec.normalize_byte_order("big_endian") == "ABCD"
    assert codec.normalize_byte_order("word_swap") == "CDAB"
    assert codec.normalize_byte_order("dcba") == "DCBA"
    assert codec.normalize_data_type("float") == "float32"
    assert codec.normalize_data_type("UINT32") == "uint32"
    with pytest.raises(ValueError):
        codec.normalize_byte_order("xyz")
    with pytest.raises(ValueError):
        codec.normalize_data_type("int128")
    with pytest.raises(ValueError):
        codec.decode([1], "float32")


def test_encode_clamps_integers():
    assert codec.encode(70000, "uint16") == [0xFFFF]
    assert codec.encode(-5, "uint16") == [0]
    assert codec.encode(40000, "int16") == [0x7FFF]
    assert codec.encode(float("nan"), "int32") == [0, 0]
    assert codec.decode(codec.encode(2.6, "int16"), "int16") == 3


def test_scaled():
    assert codec.scaled(2301, 0.1) == pytest.approx(230.1)
    assert codec.scaled(5, 1, -2) == 3
    assert codec.scaled(float("nan")) is None
    assert codec.unscaled(230.1, 0.1) == pytest.approx(2301)


# ── presety ────────────────────────────────────────────────────

def test_legacy_preset_normalizes():
    raw = json.loads((ROOT / "presets" / "library" / "simulator_3f.json").read_text(encoding="utf-8"))
    p = normalize_preset(raw)
    assert len(p["registers"]) == 35
    v = p["registers"]["voltage_l1"]
    assert (v["address"], v["type"], v["order"], v["function"], v["count"]) == (0, "float32", "ABCD", "input", 2)


def test_preset_defaults_and_overrides():
    p = normalize_preset({
        "byte_order": "word_swap", "register_type": "holding", "data_type": "int32", "address_offset": -1,
        "registers": {
            "a": {"address": 1},
            "b": {"address": "0x0011", "type": "uint16", "byte_order": "ABCD", "register_type": "input",
                  "scale": 0.1, "offset": -40},
        }})
    a, b = p["registers"]["a"], p["registers"]["b"]
    assert (a["address"], a["type"], a["order"], a["function"], a["count"]) == (0, "int32", "CDAB", "holding", 2)
    assert (b["address"], b["type"], b["order"], b["function"], b["scale"], b["offset"]) == \
        (16, "uint16", "ABCD", "input", 0.1, -40)


def test_preset_validation_collects_errors():
    with pytest.raises(PresetError) as ei:
        normalize_preset({"byte_order": "weird", "phases": 4, "registers": {
            "bad key": {"address": 0},
            "x": {"address": "zz"},
            "y": {"address": 65535, "type": "float32"},
            "z": {"address": 1, "type": "foo"},
            "s": {"address": 2, "scale": 0},
            "d": {"address": 3, "decimals": "2"},
        }, "probe": "nope"})
    msgs = " | ".join(ei.value.errors)
    for frag in ("weird", "phases", "bad key", "'zz'", "65535", "foo", "scale", "decimals", "probe"):
        assert frag in msgs


@pytest.mark.parametrize("pid,ok", [
    ("eastron_sdm630", True), ("Skan 31032026 221951", True), ("Żółw 1", True), ("a.b", True),
    ("../etc", False), ("..\\x", False), ("a/b", False), ("", False), (".hidden", False), ("x" * 81, False),
    ("trailing.", False),
])
def test_valid_id(pid, ok):
    assert valid_id(pid) is ok


def test_slugify():
    assert slugify("Skan 31.03.2026, 22:19:51") == "Skan 31.03.2026 221951"
    assert slugify("../../etc/passwd") == "etcpasswd"
    assert slugify("???") == "preset"


def test_store_crud_and_builtin_protection(tmp_path):
    user, lib = tmp_path / "user", tmp_path / "lib"
    lib.mkdir()
    (lib / "meter_x.json").write_text(json.dumps({"name": "X", "registers": {"v": {"address": 0}}}))
    store = PresetStore(user, lib)
    assert store.is_builtin("meter_x")
    assert store.get("meter_x")["registers"]["v"]["address"] == 0
    assert not store.delete("meter_x")
    # kopia użytkownika przesłania wbudowany
    store.save("meter_x", {"name": "X mój", "registers": {"v": {"address": 2}}})
    assert not store.is_builtin("meter_x")
    assert store.get("meter_x")["registers"]["v"]["address"] == 2
    listed = {p["id"]: p for p in store.list()}
    assert listed["meter_x"]["overrides_builtin"] and listed["meter_x"]["name"] == "X mój"
    # unikalne identyfikatory
    assert store.unique_id("meter_x") == "meter_x-2"
    # błędy walidacji nie zapisują pliku
    with pytest.raises(PresetError):
        store.save("zly", {"registers": {"v": {"address": -1}}})
    assert not (user / "zly.json").exists()
    with pytest.raises(PresetError):
        store.save("../ucieczka", {"registers": {}})
    assert store.get("../ucieczka") is None
    assert store.delete("meter_x") and store.is_builtin("meter_x")


def test_store_lists_broken_files(tmp_path):
    (tmp_path / "zepsuty.json").write_text("{nie json")
    (tmp_path / "zly.json").write_text(json.dumps({"registers": {"v": {"address": "x"}}}))
    store = PresetStore(tmp_path)
    listed = {p["id"]: p for p in store.list()}
    assert listed["zepsuty"]["valid"] is False
    assert listed["zly"]["valid"] is False and listed["zly"]["errors"]
    with pytest.raises(PresetError):
        store.get("zly")


# ── planer ─────────────────────────────────────────────────────

def _preset(regs, **kw):
    return normalize_preset({"registers": regs, **kw})


def test_plan_merges_and_splits_on_gap_and_size():
    p = _preset({f"r{a}": {"address": a} for a in (0, 2, 4, 30, 32, 200, 340)}, read={"max_block": 40, "max_gap": 10})
    blocks = plan_reads(p["registers"], 40, 10)
    assert [(b.start, b.count) for b in blocks] == [(0, 6), (30, 4), (200, 2), (340, 2)]


def test_plan_respects_125_limit():
    p = _preset({f"r{a}": {"address": a} for a in range(0, 400, 2)})
    for b in plan_reads(p["registers"], 125, 10):
        assert b.count <= 125


def test_plan_separates_functions():
    p = _preset({"a": {"address": 0}, "b": {"address": 2, "register_type": "holding"}})
    assert sorted((b.function, b.start) for b in plan_reads(p["registers"])) == [("holding", 2), ("input", 0)]


def test_split_block():
    p = _preset({f"r{a}": {"address": a} for a in (0, 2, 4, 6)})
    b = plan_reads(p["registers"])[0]
    parts = split_block(b, p["registers"])
    assert [(x.start, x.count) for x in parts] == [(0, 4), (4, 4)]


def test_reader_decodes_types_and_scales():
    p = _preset({
        "v": {"address": 0, "decimals": 1},
        "i": {"address": 2, "type": "int32", "byte_order": "CDAB", "scale": 0.001, "decimals": 3},
        "e": {"address": 4, "type": "uint64", "scale": 0.001, "decimals": 3},
        "t": {"address": 8, "type": "int16", "scale": 0.1, "decimals": 1},
    })
    bus = FakeBus()
    bus.put("input", 0, codec.encode(230.04, "float32"))
    bus.put("input", 2, codec.encode(-2500, "int32", "CDAB"))
    bus.put("input", 4, codec.encode(123456789, "uint64"))
    bus.put("input", 8, codec.encode(-125, "int16"))
    res = PresetReader(p).read(bus, 1)
    assert res["ok"] and not res["errors"]
    assert res["values"] == {"v": 230.0, "i": -2.5, "e": 123456.789, "t": -12.5}
    assert res["requests"] == 1


def test_reader_splits_rejected_blocks_and_remembers():
    p = _preset({"a": {"address": 0}, "b": {"address": 10}, "c": {"address": 20}})
    bus = FakeBus(strict=True)
    for a in (0, 10, 20):
        bus.put("input", a, codec.encode(float(a), "float32"))
    reader = PresetReader(p)
    res = reader.read(bus, 1)
    assert res["ok"] and res["values"] == {"a": 0.0, "b": 10.0, "c": 20.0}
    first = len(bus.calls)
    assert first > 1
    bus.calls.clear()
    res = reader.read(bus, 1)
    assert res["values"]["c"] == 20.0
    assert len(bus.calls) == 3  # zapamiętany podział - bez prób odrzucanych bloków


def test_reader_marks_missing_register_and_keeps_others():
    p = _preset({"a": {"address": 0}, "b": {"address": 2}})
    bus = FakeBus(strict=True)
    bus.put("input", 0, codec.encode(1.5, "float32"))
    res = PresetReader(p).read(bus, 1)
    assert res["ok"] and res["values"]["a"] == 1.5 and res["values"]["b"] is None and "b" in res["errors"]


def test_reader_stops_after_timeout():
    p = _preset({"a": {"address": 0}, "b": {"address": 300}})
    bus = FakeBus(silent_units={7})
    res = PresetReader(p).read(bus, 7)
    assert not res["ok"] and len(bus.calls) == 1 and set(res["errors"]) == {"a", "b"}


def test_reader_nan_is_none():
    p = _preset({"a": {"address": 0}})
    bus = FakeBus()
    bus.put("input", 0, [0x7FC0, 0x0000])
    res = PresetReader(p).read(bus, 1)
    assert res["values"]["a"] is None
    assert not math.isnan(0.0)


def test_invalid_sentinel_values():
    p = _preset({"a": {"address": 0, "type": "uint16", "invalid": 65535},
                 "b": {"address": 1, "type": "int32", "invalid": [2147483647]}})
    bus = FakeBus()
    bus.put("input", 0, [0xFFFF])
    bus.put("input", 1, codec.encode(2147483647, "int32"))
    res = PresetReader(p).read(bus, 1)
    assert res["values"] == {"a": None, "b": None}
    with pytest.raises(PresetError):
        normalize_preset({"registers": {"a": {"address": 0, "invalid": "x"}}})


def test_invalid_sentinel_survives_json_precision_loss():
    # przeglądarka zamienia 18446744073709551615 na 18446744073709552000
    p = _preset({"e": {"address": 0, "type": "uint64", "invalid": [18446744073709552000]}})
    bus = FakeBus()
    bus.put("input", 0, [0xFFFF] * 4)
    assert PresetReader(p).read(bus, 1)["values"]["e"] is None


def test_ha_classes_for_net_energy():
    from modbus_dash.quantities import ha_classes
    assert ha_classes("energy_import", "kWh") == ("energy", "total_increasing")
    assert ha_classes("energy_net", "kWh") == ("energy", "total")
    assert ha_classes("energy_reactive_net_l1", "kvarh") == (None, "total")
    assert ha_classes("energy_reactive_import", "kVArh") == (None, "total_increasing")
    assert ha_classes("pf_l1", "") == ("power_factor", "measurement")


@pytest.mark.parametrize("order,expected", [("ABCD", [0x00FE]), ("BADC", [0xFE00])])
def test_int8_low_and_high_byte(order, expected):
    assert codec.encode(-2, "int8", order) == expected
    assert codec.decode([0x12FE], "int8", "ABCD") == -2
    assert codec.decode([0xFE12], "int8", "BADC") == -2
    assert codec.decode([0x12FE], "uint8", "ABCD") == 254


def test_scale_from_register_pow10_and_multiply():
    p = _preset({
        "voltage_l1": {"address": 0, "type": "int16", "scale_from": "u_exp", "decimals": 1},
        "u_exp": {"address": 1, "type": "int8"},
        "energy_import": {"address": 2, "type": "uint32", "scale": 0.001, "scale_from": "e_fac",
                          "scale_from_mode": "multiply", "decimals": 3},
        "e_fac": {"address": 4, "type": "uint32"},
    })
    bus = FakeBus()
    bus.put("input", 0, [2301, 0x00FF])               # 2301 * 10^-1
    bus.put("input", 2, codec.encode(1234, "uint32"))
    bus.put("input", 4, codec.encode(10, "uint32"))     # 1234 Wh * 10 / 1000
    res = PresetReader(p).read(bus, 1)
    assert res["values"]["voltage_l1"] == 230.1
    assert res["values"]["energy_import"] == 12.34
    assert res["values"]["u_exp"] == -1


def test_scale_from_validation_and_missing_source():
    with pytest.raises(PresetError):
        normalize_preset({"registers": {"a": {"address": 0, "scale_from": "nope"}}})
    with pytest.raises(PresetError):
        normalize_preset({"registers": {"a": {"address": 0, "scale_from": "b"},
                                        "b": {"address": 2, "scale_from": "a"}}})
    with pytest.raises(PresetError):
        normalize_preset({"registers": {"a": {"address": 0, "scale_from": "b", "scale_from_mode": "x"},
                                        "b": {"address": 2}}})
    p = _preset({"a": {"address": 0, "type": "int16", "scale_from": "b"}, "b": {"address": 1, "type": "int16"}})
    bus = FakeBus(strict=True)
    bus.put("input", 0, [100])
    res = PresetReader(p).read(bus, 1)
    assert res["values"]["a"] is None and "skali" in res["errors"]["a"]


# ── regresje z przeglądu ──────────────────────────────────────
class _ExcBus(FakeBus):
    """Odpowiada wyjątkiem o danym kodzie (raz albo zawsze)."""

    def __init__(self, code, polls=None, **kw):
        super().__init__(**kw)
        self.code, self.polls = code, polls

    def read_registers(self, unit, function, address, count):
        from conftest import FakeModbusError
        if self.polls is None or self.polls > 0:
            self.calls.append((unit, function, address, count))
            raise FakeModbusError("exception", self.code, f"Wyjątek Modbus {self.code:02X}")
        return super().read_registers(unit, function, address, count)


def test_reader_does_not_split_on_busy():
    p = _preset({k: {"address": a} for k, a in (("a", 0), ("b", 2), ("c", 4), ("d", 6))})
    bus = _ExcBus(0x06, polls=1)
    for a in (0, 2, 4, 6):
        bus.put("input", a, codec.encode(float(a), "float32"))
    reader = PresetReader(p)
    res = reader.read(bus, 1)
    assert not res["ok"] and res["requests"] == 1 and len(reader.blocks) == 1
    bus.polls = 0
    res = reader.read(bus, 1)
    assert res["ok"] and res["requests"] == 1 and res["values"]["d"] == 6.0


def test_reader_keeps_block_when_split_does_not_help():
    p = _preset({k: {"address": a} for k, a in (("a", 0), ("b", 2), ("c", 4), ("d", 6))})
    bus = _ExcBus(0x04)
    reader = PresetReader(p)
    reader.read(bus, 1)
    assert len(reader.blocks) == 1 and reader.blocks[0].rejected
    bus.calls.clear()
    res = reader.read(bus, 1)
    assert res["requests"] == 1 and len(bus.calls) == 1 and set(res["errors"]) == {"a", "b", "c", "d"}


def test_reader_replans_periodically(monkeypatch):
    from modbus_dash import planner
    p = _preset({"a": {"address": 0}, "b": {"address": 10}, "c": {"address": 20}})
    bus = FakeBus(strict=True)
    for a in (0, 10, 20):
        bus.put("input", a, codec.encode(float(a), "float32"))
    reader = PresetReader(p)
    reader.read(bus, 1)
    assert len(reader.blocks) == 3
    for a in range(0, 22):  # licznik "naprawiony": cały zakres czytelny
        bus.image.setdefault(("input", a), 0)
    monkeypatch.setattr(planner, "REPLAN_SECONDS", 0.0)
    reader.read(bus, 1)
    assert len(reader.blocks) == 1


def test_scale_from_multiply_uses_exact_value():
    p = _preset({
        "current_l1": {"address": 0, "type": "int16", "scale": 0.01, "scale_from": "ct_ratio",
                       "scale_from_mode": "multiply"},
        "ct_ratio": {"address": 10, "decimals": 0},
        "power": {"address": 1, "type": "int16", "scale_from": "fine", "scale_from_mode": "multiply"},
        "fine": {"address": 12},
    })
    bus = FakeBus(strict=False)
    bus.put("input", 0, codec.encode(1000, "int16"))
    bus.put("input", 1, codec.encode(10, "int16"))
    bus.put("input", 10, codec.encode(2.5, "float32"))
    bus.put("input", 12, codec.encode(0.125, "float32"))
    res = PresetReader(p).read(bus, 1)
    assert res["values"]["current_l1"] == 25.0 and res["values"]["ct_ratio"] == 2.0
    assert res["values"]["power"] == 1.25 and res["values"]["fine"] == 0.12


def test_preset_ids_and_slugify():
    assert not valid_id("abc\n") and valid_id("abc") and valid_id("SDM630 garaż")
    assert slugify("- SDM630 garaż") == "SDM630 garaż" and slugify("...") == "preset"
    for name in ("- x", " .-x. ", "-" * 5 + "ok", "ąę-ł", "a" * 200):
        assert valid_id(slugify(name)), name


def test_preset_probe_and_nan_are_validation_errors():
    for probe in (["voltage_l1"], {"a": 1}, 5):
        with pytest.raises(PresetError):
            normalize_preset({"probe": probe, "registers": {"voltage_l1": {"address": 0}}})
    for spec in ({"scale": float("nan")}, {"offset": float("inf")}, {"invalid": [float("nan")]}):
        with pytest.raises(PresetError):
            normalize_preset({"registers": {"v": {"address": 0, **spec}}})


def test_broken_user_preset_does_not_break_list(tmp_path, monkeypatch):
    from modbus_dash import presets as P
    store = PresetStore(tmp_path)
    store.save("dobry", {"name": "Dobry", "registers": {"v": {"address": 0}}})
    (tmp_path / "zly.json").write_text('{"name": "Zły", "registers": {"v": {"address": 0}}}', encoding="utf-8")
    real = P.normalize_preset

    def boom(data):
        if data.get("name") == "Zły":
            raise TypeError("niespodziewany błąd")
        return real(data)
    monkeypatch.setattr(P, "normalize_preset", boom)
    listed = {p["id"]: p for p in store.list()}
    assert listed["dobry"]["valid"] and not listed["zly"]["valid"]
    with pytest.raises(PresetError):
        store.save("nan", {"name": "x", "registers": {"v": {"address": 0}}, "extra": float("nan")})


def test_ha_classes_for_other_energy_units():
    from modbus_dash.quantities import ha_classes
    assert ha_classes("energy_import", "MWh") == ("energy", "total_increasing")
    assert ha_classes("energy_reactive_import", "varh") == (None, "total_increasing")
    assert ha_classes("energy_apparent", "VAh") == (None, "total_increasing")
    assert ha_classes("energy_net", "MWh") == ("energy", "total")


def test_read_range_odd_tail_keeps_pairs():
    from modbus_dash.scanner import read_range

    class EvenBus(FakeBus):  # jak Eastron: tylko parzysty adres i parzysta długość
        def read_registers(self, unit, function, address, count):
            from conftest import FakeModbusError
            if address % 2 or count % 2:
                self.calls.append((unit, function, address, count))
                raise FakeModbusError("exception", 2, "Wyjątek Modbus 02")
            return super().read_registers(unit, function, address, count)
    bus = EvenBus(strict=False)
    bus.put("input", 340, [1, 2, 3, 4])
    vals, err, _, _, _ = read_range(bus, 1, "input", 340, 343)
    assert err is None and vals == [1, 2, None]


def test_read_range_request_cap_scales_with_span():
    from modbus_dash.scanner import read_range
    bus = FakeBus(strict=True)
    bus.put("input", 0, [1, 2])
    vals, err, req, _, covered = read_range(bus, 1, "input", 0, 382)
    assert err is None and covered == 382 and req > 400 and vals[:2] == [1, 2]
