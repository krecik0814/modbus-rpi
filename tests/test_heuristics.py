"""Testy heurystyk skanera: zgadywanie wielkości, analiza rejestrów, szkic presetu, ocena odczytu."""

import json
import math
import random

import pytest

from modbus_dash import codec, heuristics as H
from modbus_dash.planner import PresetReader, decode_value
from modbus_dash.presets import PresetError, normalize_preset

# Układ podobny do Eastron SDM630 (float32, adresy 0-based)
SDM = {
    0: 230.1, 2: 229.5, 4: 231.2,              # napięcia L1-L3
    6: 2.5, 8: 1.8, 10: 3.2,                   # prądy
    12: 546.5, 14: 380.4, 16: 651.0,           # moc czynna
    18: 575.3, 20: 413.1, 22: 739.8,           # moc pozorna
    24: 179.6, 26: -161.2, 28: 351.5,          # moc bierna
    30: 0.95, 32: 0.921, 34: 0.88,             # cos φ
    42: 230.27, 46: 2.5, 48: 7.5,              # śr. napięcie, śr. prąd, suma prądów
    52: 1577.9, 56: 1728.2, 60: 369.9, 62: 0.913,
    70: 50.01, 72: 12345.6, 74: 234.5, 76: 1023.4, 78: 12.3,
    200: 398.5, 202: 399.1, 204: 400.2, 206: 399.27,
    234: 2.1, 236: 2.4, 238: 2.2,              # THD U
}
CANONICAL = {
    0: "voltage_l1", 2: "voltage_l2", 4: "voltage_l3",
    6: "current_l1", 8: "current_l2", 10: "current_l3",
    12: "power_l1", 14: "power_l2", 16: "power_l3",
    18: "apparent_l1", 20: "apparent_l2", 22: "apparent_l3",
    24: "reactive_l1", 26: "reactive_l2", 28: "reactive_l3",
    30: "pf_l1", 32: "pf_l2", 34: "pf_l3",
    42: "voltage_ln_avg", 46: "current_avg", 48: "current_total",
    52: "power_total", 56: "apparent_total", 60: "reactive_total", 62: "pf_total",
    70: "frequency",
    200: "voltage_l12", 202: "voltage_l23", 204: "voltage_l31", 206: "voltage_ll_avg",
}


def sdm_image(order="ABCD", size=240):
    regs = [0] * size
    for addr, v in SDM.items():
        regs[addr:addr + 2] = codec.encode(v, "float32", order)
    return regs


def hint_of(value, order="ABCD", dtype="float32"):
    return H.analyze_registers(0, codec.encode(value, dtype, order))[0]["hint"]


# ── guess() ──────────────────────────────────────────────────────────

def guess(value, dtype="float32"):
    """Najlepsze dopasowanie wielkości z alternatywami albo None."""
    gs = H._guesses(value, codec.normalize_data_type(dtype).startswith("float"))
    return {**gs[0], "alternatives": gs[1:]} if gs else None


@pytest.mark.parametrize("value, kind", [
    (50.0, "frequency"), (49.98, "frequency"), (60.02, "frequency"), (47.0, "frequency"),
    (0.95, "pf"), (-0.9, "pf"), (1.0, "pf"), (0.45, "pf"),
    (230.1, "voltage"), (185.0, "voltage"), (120.3, "voltage"),
    (400.0, "line_volt"), (345.0, "line_volt"),
    (2.5, "current"), (0.2, "current"), (0.05, "current"), (150.0, "current"),
    (550.2, "power"), (-1500.0, "power"), (8000.0, "power"),
    (12345.6, "energy"), (250000.5, "energy"),
])
def test_guess_ranges_and_priority(value, kind):
    g = guess(value)
    assert g is not None and g["guess"] == kind
    assert 0 < g["score"] <= 1
    assert g["label"] and isinstance(g["unit"], str)


def test_guess_alternatives_keep_ambiguous_kinds():
    g = guess(3.2)
    assert g["guess"] == "current"
    alts = [a["guess"] for a in g["alternatives"]]
    assert "thd" in alts and "power" in alts          # THD osiągalne jako alternatywa
    assert all(a["score"] <= g["score"] for a in g["alternatives"])
    assert [a["guess"] for a in guess(12345.6)["alternatives"]] == ["power"]
    assert "frequency" not in [a["guess"] for a in guess(230.1)["alternatives"]]


def test_guess_scores_reflect_specificity():
    assert guess(50.0)["score"] > guess(230.1)["score"] > guess(0.95)["score"]
    assert guess(0.95)["score"] > guess(2.5)["score"] > guess(550.0)["score"]
    assert guess(550.0)["score"] > guess(12345.6)["score"] > guess(50.0)["alternatives"][-1]["score"]
    assert guess(230.0)["score"] > guess(120.0)["score"]      # 120 V (USA) mniej pewne
    assert guess(50.0)["group"] == "system" and guess(50.0)["unit"] == "Hz"


def test_guess_pf_only_for_float_types():
    assert guess(0.95, "float32")["guess"] == "pf"
    assert guess(0.95, "float64")["guess"] == "pf"
    assert guess(0.95, "int16")["guess"] == "current"
    assert guess(-0.9, "int32") is None


@pytest.mark.parametrize("value", [0.0, -0.0, 1e-9, -3e-7, 2e9, -5e12, math.nan, math.inf, -math.inf, None, "x", True])
def test_guess_rejects_zero_and_garbage(value):
    assert guess(value) is None


def test_classify():
    assert H.classify(None) == "missing"
    assert H.classify(0.0) == "zero" and H.classify(0) == "zero"
    assert H.classify(1e-30) == "garbage" and H.classify(1e-30, "int32") == "ok"
    assert H.classify(3e9) == "garbage" and H.classify(math.nan) == "garbage"
    assert H.classify(230.0) == "ok" and H.classify(0.001) == "ok"


# ── analyze_registers() ──────────────────────────────────────────────

def test_row_structure():
    row = H.analyze_registers(0x10, [0x4366, 0x0000])[0]
    assert row["address"] == 0x10 and row["address_hex"] == "0x0010"
    assert row["raw"] == [0x4366, 0] and row["raw_hex"] == "0x4366 0x0000"
    assert row["u16"] == 0x4366 and row["i16"] == 0x4366
    assert set(row["decoded"]) == {"float32", "int32", "uint32"}
    assert set(row["decoded"]["float32"]) == set(codec.BYTE_ORDERS)
    assert set(row["decoded"]["int32"]) == {"ABCD", "CDAB"}
    assert row["decoded"]["float32"]["ABCD"] == 230.0
    assert row["decoded"]["uint32"]["ABCD"] == 0x43660000
    assert row["decoded"]["int32"]["CDAB"] == 0x4366
    h = row["hint"]
    assert {"guess", "label", "unit", "value", "type", "byte_order", "score"} <= set(h)
    assert (h["guess"], h["value"], h["type"], h["byte_order"]) == ("voltage", 230.0, "float32", "ABCD")
    json.dumps(row)                                   # gotowe do JSON
    assert H.analyze_registers(0, [0xFFFE])[0]["i16"] == -2


@pytest.mark.parametrize("order", codec.BYTE_ORDERS)
@pytest.mark.parametrize("value, kind", [(230.1, "voltage"), (50.0, "frequency"), (0.95, "pf"),
                                         (2.5, "current"), (400.0, "line_volt"), (-161.2, "power")])
def test_all_float_byte_orders(order, value, kind):
    row = H.analyze_registers(0, codec.encode(value, "float32", order))[0]
    assert row["decoded"]["float32"][order] == pytest.approx(value, rel=1e-6)
    h = row["hint"]
    assert h["byte_order"] == order and h["guess"] == kind and h["type"] == "float32"
    assert h["value"] == pytest.approx(value, rel=1e-6)


def test_ambiguous_row_prefers_common_order():
    # 12345.6 ABCD -> w BADC wychodzi 3.1 (wygląda na prąd); słaba energia vs prąd: wygrywa ABCD
    row = H.analyze_registers(0, codec.encode(12345.6, "float32", "ABCD"))[0]
    assert guess(row["decoded"]["float32"]["BADC"])["guess"] == "current"
    assert (row["hint"]["byte_order"], row["hint"]["guess"]) == ("ABCD", "energy")
    row = H.analyze_registers(0, codec.encode(12345.6, "float32", "CDAB"))[0]
    assert (row["hint"]["byte_order"], row["hint"]["guess"]) == ("CDAB", "energy")
    # rzadka kolejność: pojedynczy wiersz bywa mylący, ale preset wyrównuje do dominującej
    image = sdm_image("BADC")
    preset = H.suggest_preset(H.analyze_registers(0, image))
    assert preset["byte_order"] == "BADC"
    assert preset["registers"]["energy_0x0048"] == {
        "address": 72, "label": "Energia (0x0048)", "unit": "kWh", "group": "energy", "decimals": 2}


def test_little_endian_is_not_word_swap():
    regs = codec.encode(230.1, "float32", "DCBA")
    row = H.analyze_registers(0, regs)[0]
    assert row["hint"]["byte_order"] == "DCBA"
    assert row["decoded"]["float32"]["CDAB"] != row["decoded"]["float32"]["DCBA"]


def test_best_order_wins_not_first_plausible():
    # ABCD daje "moc" 2052 W, ale CDAB daje napięcie 230.27 V - wygrywa lepsze dopasowanie
    regs = [0x4500, 0x4366]
    row = H.analyze_registers(0, regs)[0]
    assert guess(row["decoded"]["float32"]["ABCD"])["guess"] == "power"
    assert row["hint"]["byte_order"] == "CDAB" and row["hint"]["guess"] == "voltage"


def test_rounding_and_non_finite():
    row = H.analyze_registers(0, [0x7FC0, 0x0000])[0]             # NaN w ABCD
    assert row["decoded"]["float32"]["ABCD"] is None
    row = H.analyze_registers(0, codec.encode(0.1, "float32"))[0]
    assert row["decoded"]["float32"]["ABCD"] == 0.1                # 0.10000000149 -> 6 cyfr
    big = H.analyze_registers(0, codec.encode(123456789.0, "float32"))[0]
    assert big["decoded"]["float32"]["ABCD"] == 123457000.0


def test_step_1_vs_2_and_odd_alignment():
    regs = [0] + codec.encode(230.1, "float32") + codec.encode(50.0, "float32") + [0]   # wartości od adresu 1
    rows1 = H.analyze_registers(100, regs, step=1)
    rows2 = H.analyze_registers(100, regs, step=2)
    assert [r["address"] for r in rows1] == list(range(100, 106))
    assert [r["address"] for r in rows2] == [100, 102, 104]
    by_addr = {r["address"]: r["hint"] for r in rows1}
    assert by_addr[101]["guess"] == "voltage" and by_addr[101]["byte_order"] == "ABCD"
    assert by_addr[101]["value"] == pytest.approx(230.1)
    assert by_addr[103]["guess"] == "frequency" and by_addr[103]["value"] == 50.0
    assert rows1[-1]["raw"] == [0, None]                           # ostatni wiersz bez pary
    # skan od nieparzystego adresu parami trafia w wyrównanie
    rows_odd = H.analyze_registers(101, regs[1:], step=2)
    assert [(r["address"], r["hint"]["guess"]) for r in rows_odd[:2]] == [(101, "voltage"), (103, "frequency")]
    with pytest.raises(ValueError):
        H.analyze_registers(0, regs, step=0)


def test_none_registers():
    rows = H.analyze_registers(0, [None, None, 2300, None, 0x4366, 0x0000, 7], step=2)
    assert len(rows) == 4
    assert rows[0]["hint"] is None and rows[0]["u16"] is None and rows[0]["raw_hex"] == "---- ----"
    assert all(v is None for v in rows[0]["decoded"]["float32"].values())
    # brak drugiego rejestru: brak float32, ale uint16 ze skalą nadal możliwy
    assert all(v is None for v in rows[1]["decoded"]["float32"].values())
    assert rows[1]["decoded"]["int32"]["ABCD"] is None
    h = rows[1]["hint"]
    assert (h["guess"], h["type"], h["scale"], h["value"]) == ("voltage", "uint16", 0.1, 230.0)
    assert rows[2]["hint"]["guess"] == "voltage" and rows[2]["hint"]["type"] == "float32"
    assert rows[3]["raw"] == [7, None] and rows[3]["hint"] is None    # 7 < 10 - bez podpowiedzi


@pytest.mark.parametrize("regs", [[0, 0], [0, 1], [1, 0], [0x8000, 0], [3, 0], [0, 7], [9, None]])
def test_zero_and_garbage_pairs_have_no_hint(regs):
    assert H.analyze_registers(0, regs)[0]["hint"] is None


@pytest.mark.parametrize("regs, kind, value, dtype, scale", [
    ([2300, 0], "voltage", 230.0, "uint16", 0.1),
    ([23012, 0], "voltage", 230.12, "uint16", 0.01),
    ([4005, 0], "line_volt", 400.5, "uint16", 0.1),
    ([5001, 0], "frequency", 50.01, "uint16", 0.01),
    ([65036, 0], "power", -500, "int16", 1),
    ([0, 2301], "voltage", 230.1, "uint32", 0.1),
])
def test_integer_hints_with_scale(regs, kind, value, dtype, scale):
    h = H.analyze_registers(0, regs)[0]["hint"]
    assert (h["guess"], h["type"], h["scale"]) == (kind, dtype, scale)
    assert h["value"] == pytest.approx(value)
    assert h["score"] < guess(value)["score"]       # zgadnięta skala obniża pewność


def test_integer_hint_only_when_floats_are_garbage():
    # 0x4366 0x0000 to też uint16 17254, ale float32 230.0 jest wiarygodny
    h = H.analyze_registers(0, [0x4366, 0])[0]["hint"]
    assert h["type"] == "float32" and "scale" not in h
    # mała, "normalna" liczba float32 (0.005 A) blokuje interpretacje całkowite
    assert H.analyze_registers(0, codec.encode(0.005, "float32"))[0]["hint"] is None


def test_old_bug_energy_not_power():
    assert hint_of(12345.6)["guess"] == "energy"
    assert hint_of(50.0)["guess"] == "frequency"
    assert hint_of(0.95)["guess"] == "pf"


# ── suggest_preset() ─────────────────────────────────────────────────

@pytest.mark.parametrize("step", [1, 2])
@pytest.mark.parametrize("order", codec.BYTE_ORDERS)
def test_suggest_preset_sdm630_layout(order, step):
    rows = H.analyze_registers(0, sdm_image(order), step=step)
    preset = H.suggest_preset(rows, name="Test SDM")
    norm = normalize_preset(preset)
    assert preset["byte_order"] == order and preset["data_type"] == "float32"
    assert preset["register_type"] == "input" and preset["phases"] == 3
    assert preset["name"] == "Test SDM" and preset["probe"] == "voltage_l1"
    by_addr = {s["address"]: k for k, s in preset["registers"].items()}
    assert set(by_addr) == set(SDM)                     # bez fałszywych par "na zakładkę"
    for addr, key in CANONICAL.items():
        assert by_addr[addr] == key, (addr, by_addr[addr])
    # reszta: klucze ogólne z adresem szesnastkowym
    assert by_addr[72] == "energy_0x0048"
    assert preset["registers"]["energy_0x0048"]["label"] == "Energia (0x0048)"
    assert preset["registers"]["energy_0x0048"]["unit"] == "kWh"
    assert preset["registers"]["voltage_l1"]["label"] == "Napięcie L1"
    assert norm["registers"]["frequency"]["group"] == "system"
    # domyślne typ/kolejność nie są powtarzane w rejestrach
    assert not any({"type", "byte_order", "scale"} & set(s) for s in preset["registers"].values())


def test_suggest_preset_random_layouts():
    rnd = random.Random(7)
    gens = [lambda: rnd.uniform(225, 236), lambda: rnd.uniform(0.2, 30), lambda: rnd.uniform(-3000, 5000),
            lambda: rnd.uniform(0.7, 1.0), lambda: rnd.uniform(49.9, 50.1), lambda: rnd.uniform(100, 99999),
            lambda: rnd.uniform(390, 410), lambda: 0.0]
    for _ in range(40):
        order = rnd.choice(codec.BYTE_ORDERS)
        regs = []
        for _ in range(rnd.randrange(8, 40)):
            regs += codec.encode(rnd.choice(gens)(), "float32", order)
        for step in (1, 2):
            preset = H.suggest_preset(H.analyze_registers(0, regs + [0, 0], step=step))
            normalize_preset(preset)
            assert preset["byte_order"] == order
            assert all(s["address"] % 2 == 0 and not {"type", "byte_order"} & set(s)
                       for s in preset["registers"].values())


def test_suggested_preset_reads_back_values(fake_bus):
    image = sdm_image("CDAB")
    fake_bus.put("input", 0, image)
    preset = normalize_preset(H.suggest_preset(H.analyze_registers(0, image, step=1)))
    res = PresetReader(preset).read(fake_bus, 1)
    assert res["ok"] and not res["errors"]
    for key, spec in preset["registers"].items():
        assert res["values"][key] == pytest.approx(SDM[spec["address"]], abs=10 ** -spec["decimals"])
    assert H.plausibility(res["values"], preset) > 0.9


def test_suggest_preset_explicit_order_and_function():
    rows = H.analyze_registers(0, sdm_image("CDAB")[:12])
    preset = H.suggest_preset(rows, byte_order="word_swap", register_type="holding", name="  X ")
    assert preset["byte_order"] == "CDAB" and preset["register_type"] == "holding"
    assert preset["name"] == "X"
    normalize_preset(preset)
    # wymuszona inna kolejność: rejestry, które się w niej nie dekodują, dostają własną
    forced = H.suggest_preset(rows, byte_order="ABCD")
    assert forced["byte_order"] == "ABCD"
    assert all(s.get("byte_order") == "CDAB" for s in forced["registers"].values())
    normalize_preset(forced)
    with pytest.raises(PresetError):
        H.suggest_preset(rows, register_type="coil")
    with pytest.raises(PresetError):
        H.suggest_preset(rows, byte_order="middle")


def test_suggest_preset_integer_meter():
    regs = [0] * 0x14
    regs[0x0C], regs[0x0D] = 2301, 5001                   # 230.1 V, 50.01 Hz
    regs[0x0E] = codec.encode(-310, "int16")[0]            # moc ze znakiem
    regs[0x10:0x12] = codec.encode(1234567, "uint32")      # nie trafia w zakresy - pomijany wybór
    rows = H.analyze_registers(0, regs, step=1)
    preset = H.suggest_preset(rows, register_type="holding")
    norm = normalize_preset(preset)
    regs_by_addr = {s["address"]: (k, s) for k, s in preset["registers"].items()}
    assert preset["data_type"] == "uint16" and preset["byte_order"] == "ABCD"
    key, spec = regs_by_addr[0x0C]
    assert key == "voltage_l1" and spec["scale"] == 0.1 and spec["decimals"] == 1
    assert "type" not in spec
    key, spec = regs_by_addr[0x0D]
    assert key == "frequency" and spec["scale"] == 0.01 and spec["decimals"] == 2
    key, spec = regs_by_addr[0x0E]
    assert key == "power_0x000E" and spec["type"] == "int16" and "scale" not in spec
    assert preset["phases"] == 1 and preset["probe"] == "voltage_l1"
    assert decode_value(norm["registers"]["voltage_l1"], [2301]) == 230.1


def test_suggest_preset_ignores_bad_rows():
    preset = H.suggest_preset([None, "x", {"address": "abc", "hint": {"guess": "voltage"}},
                               {"address": 5, "hint": {"guess": "nonsense", "type": "float32"}},
                               {"address": 70000, "hint": {"guess": "voltage", "type": "float32"}},
                               {"address": 3, "hint": None}])
    assert preset["registers"] == {} and "probe" not in preset
    normalize_preset(preset)
    assert normalize_preset(H.suggest_preset([]))["registers"] == {}


def test_suggest_preset_respects_edited_hint_without_raw():
    rows = [{"address": 10, "hint": {"guess": "thd", "type": "float32", "byte_order": "big_endian",
                                     "score": 0.5, "value": 2.5}}]
    preset = H.suggest_preset(rows)
    assert list(preset["registers"]) == ["thd_0x000A"]
    assert preset["registers"]["thd_0x000A"]["unit"] == "%" and preset["byte_order"] == "ABCD"
    normalize_preset(preset)


def test_suggest_preset_power_physics_order():
    # układ P, Q, S (inny niż SDM) - rozpoznany dzięki S = sqrt(P² + Q²)
    vals = [546.5, 380.4, 651.0, 179.6, 161.2, 351.5, 575.3, 413.1, 739.8]
    regs = []
    for v in vals:
        regs += codec.encode(v, "float32")
    preset = H.suggest_preset(H.analyze_registers(0x100, regs))
    keys = [k for k, _ in sorted(preset["registers"].items(), key=lambda kv: kv[1]["address"])]
    assert keys == ["power_l1", "power_l2", "power_l3", "reactive_l1", "reactive_l2", "reactive_l3",
                    "apparent_l1", "apparent_l2", "apparent_l3"]


# ── plausibility() ───────────────────────────────────────────────────

def _read(preset, image):
    return {k: decode_value(s, image[s["address"]:s["address"] + s["count"]])
            for k, s in preset["registers"].items()}


@pytest.mark.parametrize("order", codec.BYTE_ORDERS)
def test_plausibility_ranks_correct_byte_order_first(order):
    image = sdm_image(order)
    raw = H.suggest_preset(H.analyze_registers(0, sdm_image("ABCD")))
    scores = {}
    for o in codec.BYTE_ORDERS:
        preset = normalize_preset({**raw, "byte_order": o})
        scores[o] = H.plausibility(_read(preset, image), preset)
    assert scores[order] >= 0.95
    assert all(scores[order] > scores[o] + 0.5 for o in codec.BYTE_ORDERS if o != order)


def test_plausibility_subset_and_edge_cases():
    preset = normalize_preset(H.suggest_preset(H.analyze_registers(0, sdm_image())))
    good = {"voltage_l1": 230.1, "voltage_l2": 229.5, "frequency": 50.0, "pf_l1": 0.95, "current_l1": 2.5}
    assert H.plausibility(good, preset) == 1.0
    assert H.plausibility({k: 0.0 for k in preset["registers"]}, preset) < 0.15     # same zera
    assert H.plausibility({k: None for k in preset["registers"]}, preset) == 0.0
    assert H.plausibility({}, preset) == 0.0
    assert H.plausibility({"unknown_key": 230.0}, preset) == 0.0
    bad = {**good, "pf_l1": 3.5, "frequency": 0.0}
    assert 0 < H.plausibility(bad, preset) < H.plausibility(good, preset)
    assert H.plausibility({**good, "voltage_l1": 1e-20}, preset) < 1.0              # śmieci
    # prądy: ujemne podejrzane, ponad 1000 A nieprawdopodobne
    assert H.plausibility({"current_l1": -2.0, "voltage_l1": 230}, preset) < 1.0
    assert H.plausibility({"current_l1": 5000.0, "voltage_l1": 230}, preset) < 1.0


def test_plausibility_uses_units_for_custom_keys():
    preset = normalize_preset({"registers": {
        "u": {"address": 0, "unit": "V"}, "ul": {"address": 2, "unit": "V", "group": "line_volt"},
        "f": {"address": 4, "unit": "Hz"}, "e": {"address": 6, "unit": "kWh"},
        "cos": {"address": 8, "group": "pf", "unit": ""}, "x": {"address": 10},
    }})
    good = {"u": 231.0, "ul": 400.0, "f": 50.0, "e": 10.5, "cos": -0.9, "x": 12.0}
    assert H.plausibility(good, preset) == 1.0
    assert H.plausibility({**good, "u": 400.0}, preset) == 1.0          # V bez grupy: fazowe lub międzyfazowe
    assert H.plausibility({**good, "ul": 230.0}, preset) < 1.0          # międzyfazowe 230 V - USA, mniej pewne
    assert H.plausibility({**good, "e": -5.0}, preset) < 1.0
    assert H.plausibility({"x": 12.0}, preset) == 0.5                   # nieznana wielkość


# ── regresje z przeglądu ──────────────────────────────────────
def _image_rows(values, order, start, end, step=1):
    regs = [0] * (end - start)
    for a, v in values.items():
        for i, r in enumerate(codec.encode(v, "float32", order)):
            if start <= a + i < end:
                regs[a + i - start] = r
    return [r for r in H.analyze_registers(start, regs, step=step) if r["hint"]]


@pytest.mark.parametrize("order,parity", [("ABCD", 1), ("CDAB", 0), ("ABCD", 0), ("CDAB", 1)])
def test_step1_scan_picks_real_alignment(order, parity):
    base = {0: 230.1, 2: 229.5, 4: 231.2, 6: 2.5, 8: 1.8, 10: 3.2, 12: 546.5, 14: 50.01,
            20: 12345.6, 22: 0.95, 24: 0.93}
    vals = {a + parity: v for a, v in base.items()}
    rows = _image_rows(vals, order, 0, 40)
    d = H.suggest_preset(rows)
    wide = {(s["address"], s.get("byte_order", d["byte_order"])) for s in d["registers"].values()
            if s.get("type", d["data_type"]) == "float32"}
    assert wide <= {(a, order) for a in vals}, (d["byte_order"], sorted(wide))
    assert len(wide) >= len(vals) - 1


def test_ambiguous_alignment_is_reported_and_alternative_can_be_forced():
    vals = {a: 200.0 + a * 1.37 for a in range(0, 60, 2)}
    rows = _image_rows(vals, "ABCD", 0, 60)
    d = H.suggest_preset(rows)
    assert d["byte_order"] == "ABCD" and d["_warnings"] and "UWAGA" in d["description"]
    assert d["_alternative"] == {"byte_order": "CDAB", "alignment": "odd"}
    alt = H.suggest_preset(rows, byte_order="CDAB", alignment="odd")
    assert all(s["address"] % 2 == 1 for s in alt["registers"].values()) and "_warnings" not in alt
    with pytest.raises(PresetError):
        H.suggest_preset(rows, alignment="środek")


def test_integer_meter_is_not_swapped_by_one_lucky_float():
    # rejestry 16-bit nie mogą przegrać z jednym przypadkowym "ładnym" floatem BADC
    rows = []
    for a, v in ((0, 2301), (1, 2295), (2, 2312), (3, 5001), (4, 125), (5, 98)):
        rows += H.analyze_registers(a, [v, 0], step=2)[:1]
    rows += H.analyze_registers(10, [0x0043, 0x0025], step=2)
    rows = [r for r in rows if r["hint"]]
    d = H.suggest_preset(rows)
    assert d["byte_order"] == "ABCD"


def test_suggest_preset_skips_malformed_hints():
    good = {"address": 0, "raw": [17254, 0], "hint": {"guess": "voltage", "type": "float32",
                                                       "byte_order": "ABCD", "score": 0.9, "value": 230}}
    bad = [{**good, "hint": {**good["hint"], **h}} for h in (
        {"score": "x"}, {"guess": []}, {"scale": float("nan")}, {"score": True}, {"alternatives": [{"guess": {}}]})]
    d = H.suggest_preset([good] + [{**r, "address": 10 + 2 * i} for i, r in enumerate(bad)])
    # wiersz 18 ma poprawną podpowiedź - pomijana jest tylko zepsuta alternatywa
    assert [s["address"] for s in d["registers"].values()] == [0, 18]


def test_dc_meter_detected_at_battery_voltage():
    p = normalize_preset(json.load(open(H.__file__.replace("modbus_dash/heuristics.py",
                                                           "presets/library/peacefair_pzem_017.json"))))
    q = normalize_preset(json.load(open(H.__file__.replace("modbus_dash/heuristics.py",
                                                           "presets/library/peacefair_pzem_004t.json"))))
    for volts in (12.8, 26.4, 53.0, 150.0):
        regs = {0: round(volts * 100), 1: 320, 2: round(volts * 3.2 * 10)}
        def read(preset, regs=regs):
            values = {}
            for k, s in preset["registers"].items():
                chunk = [regs.get(s["address"] + i, 0) for i in range(s["count"])]
                values[k] = decode_value(s, chunk)
            return values
        assert H.plausibility(read(p), p) > H.plausibility(read(q), q), volts


@pytest.mark.parametrize("pid", ["eltako_dsz16", "carlo_gavazzi_em24", "orno_or_we_525"])
def test_integer_draft_alignment_comes_from_integer_hints(pid):
    import os
    from collections import Counter
    from modbus_dash.presets import PresetStore
    from modbus_dash.simulator import Physics, SimDevice
    lib = os.path.join(os.path.dirname(os.path.dirname(H.__file__)), "presets", "library")
    p = PresetStore(lib, lib).get(pid)
    dev = SimDevice(1, p)
    dev.update(Physics(seed=1).tick(0))
    func = Counter(s["function"] for s in p["registers"].values()).most_common(1)[0][0]
    end = max(s["address"] + s["count"] for s in p["registers"].values() if s["function"] == func) + 2
    regs = []
    for a in range(0, end, 100):
        try:
            regs += dev.read(func, a, min(100, end - a))
        except Exception:  # noqa: BLE001 - niezmapowany zakres
            regs += [None] * min(100, end - a)
    rows = [r for r in H.analyze_registers(0, regs, step=1) if r["hint"]]
    d = H.suggest_preset(rows, register_type=func)
    # licznik "całkowity": bez fałszywego ostrzeżenia o wyrównaniu wziętego z przypadkowych floatów
    assert "_warnings" not in d and "UWAGA" not in d["description"]


def test_dc_preset_needs_power_to_match_voltage_times_current():
    p = normalize_preset(json.load(open(H.__file__.replace("modbus_dash/heuristics.py",
                                                           "presets/library/peacefair_pzem_017.json"))))
    ok = {"voltage_dc": 22.93, "current_dc": 10.0, "power_dc": 229.3, "energy_import": 2.31}
    bad = {**ok, "current_dc": 0.0}  # moc bez prądu: to nie jest licznik DC
    assert H.plausibility(ok, p) > 0.9 > H.plausibility(bad, p)
