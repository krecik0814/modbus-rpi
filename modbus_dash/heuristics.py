"""Heurystyki skanera: dekodowanie surowych rejestrów i zgadywanie wielkości.

Skaner czyta zakres rejestrów i dla każdego adresu (step=1) albo pary adresów
(step=2) pokazuje wszystkie interpretacje: float32 w 4 kolejnościach bajtów,
int32/uint32, uint16/int16 oraz podpowiedź, co to może być (napięcie, prąd...).
Z podpowiedzi suggest_preset() buduje szkic presetu, a plausibility() ocenia,
czy odczytane wartości pasują do presetu (automatyczne rozpoznawanie licznika).
"""

import bisect
import itertools
import math
import time
from collections import Counter

from . import codec
from .presets import PresetError, normalize_function
from .quantities import QUANTITIES, normalize_unit

# rodzaj -> (etykieta, jednostka, grupa presetu, miejsca po przecinku)
KINDS = {
    "frequency": ("Częstotliwość", "Hz", "system", 2),
    "line_volt": ("Napięcie międzyfazowe", "V", "line_volt", 1),
    "voltage": ("Napięcie fazowe", "V", "voltage", 1),
    "pf": ("Współczynnik mocy (cos φ)", "", "pf", 3),
    "current": ("Prąd", "A", "current", 2),
    "thd": ("THD", "%", "thd", 1),
    "power": ("Moc", "W", "power", 0),
    "energy": ("Energia", "kWh", "energy", 2),
}

# Zakresy od najbardziej do najmniej specyficznych. Dla danego rodzaju
# zakresy z wyższym wynikiem są pierwsze, więc pierwsze trafienie = najlepsze.
# (rodzaj, od, do, wynik, porównuj |v|, tylko dla float)
RANGES = (
    ("frequency", 49, 51, 0.95, False, False),
    ("frequency", 59, 61, 0.95, False, False),
    ("frequency", 45, 65, 0.45, False, False),          # sieć rzadko odchodzi o > 1 Hz
    ("line_volt", 380, 420, 0.85, False, False),
    ("line_volt", 340, 440, 0.75, False, False),
    ("voltage", 220, 240, 0.9, False, False),
    ("voltage", 180, 260, 0.8, False, False),
    ("voltage", 100, 130, 0.55, False, False),          # sieci 120 V (USA)
    ("pf", 0.8, 1.0, 0.8, True, True),
    ("pf", 0.3, 0.8, 0.6, True, True),
    ("current", 0.1, 200, 0.4, False, False),
    ("current", 0.01, 0.1, 0.25, False, False),         # prąd jałowy - rzadszy
    ("thd", 0.1, 40, 0.2, False, False),                # niejednoznaczne (wygląda jak prąd)
    ("power", 1, 10000, 0.35, True, False),
    ("power", 10000, 100000, 0.2, True, False),
    ("energy", 10000, 1e9, 0.3, False, False),          # licznik narastający
    ("energy", 0.01, 10000, 0.1, False, False),
)

# typowe skale dla liczb całkowitych (np. 2300 * 0.1 = 230.0 V)
TYPICAL_SCALES = {
    "frequency": (1, 0.1, 0.01),
    "line_volt": (1, 0.1, 0.01),
    "voltage": (1, 0.1, 0.01),
    "pf": (),
    "current": (0.1, 0.01, 0.001),
    "thd": (0.1, 0.01),
    "power": (1, 0.1),
    "energy": (1, 0.1, 0.01, 0.001),
}
# (skala, mnożnik wyniku) - zgadnięta skala obniża pewność
INT_SCALES = ((1, 0.65), (0.1, 0.6), (0.01, 0.55), (0.001, 0.5))
SIGNED_KINDS = ("power",)

# premia przy wyborze kolejności: ABCD/CDAB są dużo częstsze niż zamiana bajtów
ORDER_PRIOR = {"ABCD": 0.1, "CDAB": 0.1}

TINY = 1e-6
HUGE = 1e9


def _is_float_type(dtype):
    return codec.normalize_data_type(dtype).startswith("float")


def classify(value, dtype="float32"):
    """'missing' (brak), 'zero', 'garbage' (NaN/inf, denormal, ogromne) albo 'ok'."""
    if value is None:
        return "missing"
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not codec.is_finite(value):
        return "garbage"
    if value == 0:
        return "zero"
    av = abs(value)
    if av > HUGE or (av < TINY and _is_float_type(dtype)):
        return "garbage"
    return "ok"


def _round(v, digits=6):
    """Zaokrąglenie do cyfr znaczących (JSON); NaN/inf -> None."""
    if v is None or isinstance(v, int):
        return v
    if not math.isfinite(v):
        return None
    return float(f"{v:.{digits}g}")


def _guesses(value, is_float=True):
    """Wszystkie pasujące rodzaje posortowane od najlepszego: [{guess, group, label, unit, score}]."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return []
    if classify(value, "float32" if is_float else "int32") != "ok":
        return []
    found = {}
    for i, (kind, lo, hi, score, absolute, float_only) in enumerate(RANGES):
        if kind in found or (float_only and not is_float):
            continue
        if lo <= (abs(value) if absolute else value) <= hi:
            found[kind] = (score, i)
    out = []
    for kind, (score, i) in sorted(found.items(), key=lambda kv: (-kv[1][0], kv[1][1])):
        label, unit, group, _ = KINDS[kind]
        out.append({"guess": kind, "group": group, "label": label, "unit": unit, "score": score})
    return out


def _short(g, mult=1.0):
    return {"guess": g["guess"], "label": g["label"], "unit": g["unit"], "score": round(g["score"] * mult, 3)}


def guess(value, dtype="float32"):
    """Zgaduje wielkość fizyczną po wartości.

    Zwraca None (zero, śmieci, nic nie pasuje) albo {guess, group, label, unit,
    score, alternatives}. guess = rodzaj (voltage, line_volt, current, power,
    pf, frequency, thd, energy); group = grupa presetu (frequency -> system).
    cos φ tylko dla typów zmiennoprzecinkowych.
    """
    try:
        is_float = _is_float_type(dtype)
    except ValueError:
        is_float = True
    gs = _guesses(value, is_float)
    if not gs:
        return None
    return {**gs[0], "alternatives": [_short(g) for g in gs[1:]]}


def _hint(gs, value, dtype, order, mult=1.0, scale=None, raw=None):
    g = gs[0]
    h = {"guess": g["guess"], "group": g["group"], "label": g["label"], "unit": g["unit"],
         "value": _round(value), "type": dtype, "byte_order": order,
         "score": round(g["score"] * mult, 3)}
    if scale is not None:
        h["scale"] = scale
        h["raw_value"] = raw
    h["alternatives"] = [_short(a, mult) for a in gs[1:]]
    return h


def _int_type(dtype, raw, kind):
    if dtype == "uint16":
        return "int16" if kind in SIGNED_KINDS and raw < 0x8000 else "uint16"
    if dtype == "int32":
        return "int32" if raw < 0 or kind in SIGNED_KINDS else "uint32"
    return dtype


def _floats(r0, r1):
    if r0 is None or r1 is None:
        return {}
    return {o: codec.decode([r0, r1], "float32", o) for o in codec.BYTE_ORDERS}


def _candidates(r0, r1, floats=None):
    """Wszystkie sensowne interpretacje pary rejestrów, od najlepszej.

    Najpierw float32: najwyższy wynik (+ premia dla częstych ABCD/CDAB), remis -> ABCD.
    Liczby całkowite (ze skalą) tylko wtedy, gdy dekodowania float32 to śmieci:
    żadne nie pasuje do zakresów, żadne nie jest zerem, a ABCD nie jest zwykłą
    małą liczbą (np. 0.005).
    """
    if r0 is None:
        return []
    floats = _floats(r0, r1) if floats is None else floats
    out = []
    for i, (order, v) in enumerate(floats.items()):
        gs = _guesses(v)
        if gs:
            prior = ORDER_PRIOR.get(order, 0)
            out.append(((-round(gs[0]["score"] + prior, 3), i), _hint(gs, v, "float32", order)))
    abcd = floats.get("ABCD")
    if out or any(classify(v) == "zero" for v in floats.values()) or (
            classify(abcd) == "ok" and abs(abcd) >= 1e-3):
        return [h for _, h in sorted(out, key=lambda c: c[0])]

    # floaty to śmieci (bez podpowiedzi, zamienione kolejności to szum) -
    # liczby całkowite z typową skalą
    interps = [(0, "uint16", "ABCD", r0)]
    if r0 >= 0x8000:
        interps.append((0, "int16", "ABCD", r0 - 0x10000))
    # 32-bit z zerowym młodszym słowem (x * 65536) to raczej dwa osobne rejestry
    if r1:
        interps.append((1, "int32", "ABCD", codec.decode([r0, r1], "int32", "ABCD")))
    if r0 and r1 is not None:
        interps.append((1, "int32", "CDAB", codec.decode([r0, r1], "int32", "CDAB")))
    for width, dtype, order, raw in interps:
        if not 10 <= abs(raw) <= HUGE:
            continue
        for si, (scale, mult) in enumerate(INT_SCALES):
            gs = [g for g in _guesses(raw * scale, False) if scale in TYPICAL_SCALES[g["guess"]]]
            if not gs:
                continue
            m = mult * (0.95 if width else 1.0)
            h = _hint(gs, raw * scale, _int_type(dtype, raw, gs[0]["guess"]), order, m, scale, raw)
            out.append(((-h["score"], width, si, order != "ABCD"), h))
    return [h for _, h in sorted(out, key=lambda c: c[0])]


def _reg(v):
    if v is None or isinstance(v, bool):
        return None
    try:
        return int(v) & 0xFFFF
    except (TypeError, ValueError):
        return None


def _hex(r):
    return "----" if r is None else f"0x{r:04X}"


def _row(address, r0, r1):
    floats = _floats(r0, r1)
    dec = {"float32": {o: _round(floats.get(o)) for o in codec.BYTE_ORDERS},
           "int32": {"ABCD": None, "CDAB": None}, "uint32": {"ABCD": None, "CDAB": None}}
    if floats:
        for o in ("ABCD", "CDAB"):
            dec["int32"][o] = codec.decode([r0, r1], "int32", o)
            dec["uint32"][o] = codec.decode([r0, r1], "uint32", o)
    cands = _candidates(r0, r1, floats)
    return {
        "address": address,
        "address_hex": f"0x{address:04X}",
        "raw": [r0, r1],
        "raw_hex": f"{_hex(r0)} {_hex(r1)}",
        "u16": r0,
        "i16": None if r0 is None else (r0 - 0x10000 if r0 >= 0x8000 else r0),
        "decoded": dec,
        "hint": cands[0] if cands else None,
    }


def analyze_registers(start, regs, step=2):
    """Wiersz analizy dla adresów start, start+step, ...

    regs[i] = None, gdy rejestru nie udało się odczytać. step=1 analizuje każdy
    adres (wartości 32-bit mogą zaczynać się pod nieparzystym adresem), step=2 pary.
    """
    step = int(step)
    if step < 1:
        raise ValueError("krok skanowania musi być >= 1")
    start = int(start)
    regs = [_reg(v) for v in regs]
    n = len(regs)
    return [_row(start + i, regs[i], regs[i + 1] if i + 1 < n else None)
            for i in range(0, n, step)]


# ── szkic presetu ze skanu ────────────────────────────────────────────

TRIPLETS = (
    ("voltage", ("voltage_l1", "voltage_l2", "voltage_l3")),
    ("line_volt", ("voltage_l12", "voltage_l23", "voltage_l31")),
    ("current", ("current_l1", "current_l2", "current_l3")),
    ("pf", ("pf_l1", "pf_l2", "pf_l3")),
)
PSQ = (("power_l1", "power_l2", "power_l3"), ("apparent_l1", "apparent_l2", "apparent_l3"),
       ("reactive_l1", "reactive_l2", "reactive_l3"))
# wielkości wyliczalne z faz: (klucz, rodzaj, funkcja, składniki, tolerancja względna)
DERIVED = (
    ("voltage_ln_avg", "voltage", "mean", TRIPLETS[0][1], 0.005),
    ("voltage_ll_avg", "line_volt", "mean", TRIPLETS[1][1], 0.005),
    ("current_total", "current", "sum", TRIPLETS[2][1], 0.01),
    ("current_avg", "current", "mean", TRIPLETS[2][1], 0.005),
    ("power_total", "power", "sum", PSQ[0], 0.01),
    ("apparent_total", "power", "sum", PSQ[1], 0.01),
    ("reactive_total", "power", "sum", PSQ[2], 0.02),
)
_SWAPPED = ("BADC", "DCBA")


def _kinds(h):
    alts = h.get("alternatives") if isinstance(h.get("alternatives"), list) else ()
    return {h.get("guess")} | {a.get("guess") for a in alts if isinstance(a, dict)}


def _width(h):
    try:
        return codec.register_count(h.get("type"))
    except ValueError:
        return 2


def _same_order(count, a, b):
    # dla typów 16-bit liczy się tylko zamiana bajtów
    return a == b if count > 1 else (a in _SWAPPED) == (b in _SWAPPED)


def _family(h):
    return "float" if str(h.get("type", "")).startswith("float") else "int"


def _select(items, order, parity, family):
    """Niekolidujące rejestry o największej łącznej wadze (weighted interval scheduling).

    items: [(adres, liczba rejestrów, hint)]. Premia za zgodność z dominującą
    kolejnością bajtów, parzystością adresu i rodziną typu (float/int) eliminuje
    fałszywe trafienia z par "na zakładkę" (step=1). Wartości 32-bit w innej
    kolejności zostają tylko tam, gdzie dominująca nic nie znalazła.
    """
    covered = set()
    for a, n, h in items:
        if n > 1 and h["byte_order"] == order:
            covered.update(range(a, a + n))
    items = sorted(((a, n, h) for a, n, h in items
                    if n == 1 or h["byte_order"] == order or not covered.intersection(range(a, a + n))),
                   key=lambda it: (it[0] + it[1], it[0]))
    ends = [a + n for a, n, _ in items]
    best, take, prev = [0.0], [], []
    for j, (a, n, h) in enumerate(items):
        p = bisect.bisect_right(ends, a, 0, j)
        w = (float(h.get("score") or 0) + (1.0 if _same_order(n, h["byte_order"], order) else 0)
             + (1.0 if n == 1 or a % 2 == parity else 0) + (0.5 if _family(h) == family else 0))
        take.append(best[p] + w > best[j])
        best.append(max(best[j], best[p] + w))
        prev.append(p)
    out, j = [], len(items)
    while j > 0:
        if take[j - 1]:
            out.append(items[j - 1])
            j = prev[j - 1]
        else:
            j -= 1
    return out[::-1]


def _name(chosen):
    """Przypisuje klucze kanoniczne tam, gdzie wzorzec jest pewny. Zwraca {indeks: klucz}."""
    names = {}
    vals = [h.get("value") if isinstance(h.get("value"), (int, float)) else None for _, _, h in chosen]

    def contiguous(i):  # czy chosen[i+1] zaczyna się zaraz za chosen[i]
        return i + 1 < len(chosen) and chosen[i + 1][0] == chosen[i][0] + chosen[i][1]

    def runs(kind):
        out, cur = [], []
        for i, (_, _, h) in enumerate(chosen):
            if h.get("guess") == kind and i not in names and (not cur or contiguous(cur[-1])):
                cur.append(i)
                continue
            if cur:
                out.append(cur)
            cur = [i] if h.get("guess") == kind and i not in names else []
        return out + ([cur] if cur else [])

    def assign(indices, keys):
        for i, k in zip(indices, keys):
            names[i] = k

    for kind, keys in TRIPLETS:
        run = next((r for r in runs(kind) if len(r) >= 3), None)
        if run:
            assign(run, keys)

    # moc: 9 kolejnych wartości P/S/Q sprawdzonych fizyką S = sqrt(P² + Q²)
    pf = [vals[i] for i in sorted(names) if names[i] in TRIPLETS[3][1] and vals[i] is not None]
    for i in range(len(chosen) - 8):
        win = list(range(i, i + 9))
        if (chosen[i][2].get("guess") != "power" or any(k in names for k in win)
                or not all(contiguous(k) for k in win[:-1])
                or not all("power" in _kinds(chosen[k][2]) and vals[k] is not None for k in win)):
            continue
        t = [[vals[i + 3 * a + p] for p in range(3)] for a in range(3)]
        best = None
        for ip, i_s, iq in itertools.permutations(range(3)):
            P, S, Q = t[ip], t[i_s], t[iq]
            if not all(S[p] > 0 and abs(S[p] - math.hypot(P[p], Q[p])) <= 0.03 * S[p] + 0.5
                       for p in range(3)):
                continue
            rank = (sum(len(pf) == 3 and abs(abs(P[p]) / S[p] - abs(pf[p])) <= 0.05 for p in range(3)),
                    sum(abs(P[p]) >= abs(Q[p]) for p in range(3)))
            if best is None or rank > best[0]:
                best = (rank, (ip, i_s, iq))
        if best:
            for a, idx in enumerate(best[1]):
                assign(win[3 * a:3 * a + 3], PSQ[idx])
            break
    if "power_l1" not in names.values():
        run = next((r for r in runs("power") if len(r) % 3 == 0), None)
        if run:
            assign(run[:3], PSQ[0])

    # sumy i średnie faz; trójki "prądów" bez nazwy to raczej THD albo kąty
    by_key = {k: i for i, k in names.items()}
    names.update({i: None for r in runs("current") if len(r) >= 3 for i in r})
    for key, kind, fn, parts, tol in DERIVED:
        if key in by_key or not all(p in by_key and vals[by_key[p]] is not None for p in parts):
            continue
        total = sum(vals[by_key[p]] for p in parts)
        expected = total / len(parts) if fn == "mean" else total
        if abs(expected) < 1e-3:
            continue
        hit = _closest(chosen, names, vals, kind, expected, tol * abs(expected) + 1e-3)
        if hit is not None:
            names[hit] = key
            by_key[key] = hit
    if "pf_total" not in by_key and "power_total" in by_key and "apparent_total" in by_key:
        s = vals[by_key["apparent_total"]]
        if s:
            hit = _closest(chosen, names, vals, "pf", abs(vals[by_key["power_total"]] / s), 0.02, absolute=True)
            if hit is not None:
                names[hit] = "pf_total"

    names = {i: k for i, k in names.items() if k}

    # częstotliwość: najlepsza kandydatka
    freq = [i for i, (_, _, h) in enumerate(chosen) if h.get("guess") == "frequency" and i not in names]
    if freq and "frequency" not in names.values():
        names[max(freq, key=lambda i: (chosen[i][2].get("score") or 0, -i))] = "frequency"

    # licznik 1-fazowy: jedyne napięcie fazowe -> voltage_l1
    volts = [i for i, (_, _, h) in enumerate(chosen) if h.get("guess") == "voltage"]
    lines = [i for i, (_, _, h) in enumerate(chosen) if h.get("guess") == "line_volt"]
    if len(volts) == 1 and not lines and volts[0] not in names:
        names[volts[0]] = "voltage_l1"
    return names


def _closest(chosen, names, vals, kind, expected, tol, absolute=False):
    best = None
    for i, (_, _, h) in enumerate(chosen):
        if i in names or kind not in _kinds(h) or vals[i] is None:
            continue
        err = abs((abs(vals[i]) if absolute else vals[i]) - expected)
        if err <= tol and (best is None or err < best[0]):
            best = (err, i)
    return best[1] if best else None


def suggest_preset(rows, byte_order=None, register_type="input", name=None):
    """Szkic presetu (schemat 2) z wierszy skanu z podpowiedziami.

    Klucze kanoniczne (voltage_l1..l3, current_l1..l3, frequency, sumy...) tylko
    przy pewnym wzorcu, pozostałe "<rodzaj>_0x<adres>". Domyślna kolejność bajtów =
    najczęstsza wśród podpowiedzi (albo podana); typ/kolejność/skala w rejestrze
    tylko gdy różnią się od domyślnych presetu. Złe parametry -> PresetError.
    """
    try:
        func = normalize_function(register_type)
        fixed = codec.normalize_byte_order(byte_order) if byte_order else None
    except ValueError as e:
        raise PresetError([str(e)]) from None
    items = []
    for row in rows or ():
        if not isinstance(row, dict) or not isinstance(row.get("hint"), dict):
            continue
        try:
            addr = int(row.get("address"))
            h = {**row["hint"], "type": codec.normalize_data_type(row["hint"].get("type")),
                 "byte_order": codec.normalize_byte_order(row["hint"].get("byte_order"))}
        except (TypeError, ValueError):
            continue
        if h.get("guess") not in KINDS or not 0 <= addr <= 0xFFFF - (_width(h) - 1):
            continue
        raw = row.get("raw") if isinstance(row.get("raw"), list) else []
        raw = [_reg(v) for v in raw[:2]] + [None] * (2 - len(raw[:2]))
        items.append([addr, h, [c for c in _candidates(*raw) if _width(c) > 1]])

    if fixed:
        order = fixed
    else:
        # Każdy wiersz głosuje na wszystkie kolejności, w których daje sensowny
        # float32 (liczby całkowite tylko swoją podpowiedzią). Przy step=1 CDAB
        # pod parzystym adresem wygląda prawie jak ABCD "na zakładkę" pod
        # nieparzystym - przy remisie wygrywa wyrównanie parzyste.
        votes = {"float": Counter(), "int": Counter()}
        for a, h, wide in items:
            orders = {c["byte_order"] for c in wide if _family(c) == "float"}
            if not wide and _width(h) > 1 and _family(h) == "float":
                orders = {h["byte_order"]}           # wiersz bez surowych danych
            for o in orders:
                votes["float"][o] += 1.0 if a % 2 == 0 else 0.85
            if _width(h) > 1 and _family(h) == "int":
                votes["int"][h["byte_order"]] += 1
        cnt = votes["float"] or votes["int"]
        order = max(codec.BYTE_ORDERS, key=lambda o: (cnt[o], -codec.BYTE_ORDERS.index(o))) if cnt else "ABCD"

    # podpowiedzi w innej kolejności: spróbuj zdekodować w dominującej
    for it in items:
        addr, h, wide = it
        if _width(h) > 1 and h["byte_order"] != order:
            alt = next((c for c in wide if c["byte_order"] == order), None)
            if alt:
                it[1] = alt

    par = Counter(a % 2 for a, h, _ in items if _width(h) > 1 and h["byte_order"] == order)
    fam = Counter(_family(h) for _, h, _ in items)
    chosen = _select([(a, _width(h), h) for a, h, _ in items], order,
                     1 if par[1] > par[0] else 0, "int" if fam["int"] > fam["float"] else "float")
    names = _name(chosen)

    types = Counter(h.get("type") for _, _, h in chosen)
    def_type = max(types, key=lambda t: (types[t], t == "float32")) if types else "float32"

    registers = {}
    for i, (addr, count, h) in enumerate(chosen):
        kind = h["guess"]
        key = names.get(i)
        if key:
            q = QUANTITIES[key]
            label, unit, group, decimals = q["label"], q["unit"], q["group"], q["decimals"]
        else:
            label, unit, group, decimals = KINDS[kind]
            key = f"{kind}_0x{addr:04X}"
            label = f"{label} (0x{addr:04X})"
        spec = {"address": addr, "label": label, "unit": unit, "group": group}
        if h["type"] != def_type:
            spec["type"] = h["type"]
        if not _same_order(count, h["byte_order"], order):
            spec["byte_order"] = h["byte_order"]
        scale = h.get("scale")
        if isinstance(scale, (int, float)) and not isinstance(scale, bool) and scale not in (0, 1):
            spec["scale"] = scale
            decimals = max(decimals, min(10, round(-math.log10(abs(scale)))))
        spec["decimals"] = decimals
        registers[key] = spec

    keys = set(registers)
    if keys & {"voltage_l2", "voltage_l3", "current_l2", "current_l3", "voltage_l12", "power_l2"}:
        phases = 3
    elif "voltage_l1" in keys:
        phases = 1
    else:
        phases = 3
    probe = next((k for k in ("voltage_l1", "frequency", "voltage_l12") if k in keys), None)
    if probe is None and registers:
        probe = min(registers, key=lambda k: registers[k]["address"])

    preset = {
        "name": str(name).strip() if name else f"Skan {time.strftime('%Y-%m-%d %H-%M')}",
        "manufacturer": "",
        "model": "",
        "description": "Wygenerowany automatycznie ze skanu rejestrów - sprawdź adresy, typy i jednostki.",
        "source": "Modbus Dash - skaner rejestrów",
        "phases": phases,
        "register_type": func,
        "byte_order": order,
        "data_type": def_type,
        "registers": registers,
    }
    if probe:
        preset["probe"] = probe
    return preset


# ── ocena wiarygodności odczytu presetu ───────────────────────────────

_UNIT_CATEGORY = {
    "A": "current", "W": "power", "kW": "power", "VA": "power", "kVA": "power",
    "var": "power", "kvar": "power", "Hz": "frequency", "kWh": "energy", "Wh": "energy",
    "MWh": "energy", "kvarh": "energy", "varh": "energy", "kVAh": "energy", "VAh": "energy",
    "%": "thd", "°C": "temperature", "°": "angle",
}
_GROUP_CATEGORY = {"voltage": "voltage", "line_volt": "line_volt", "current": "current",
                   "pf": "pf", "energy": "energy", "thd": "thd", "power": "power", "total": "power"}
_WEIGHTS = {"voltage": 2, "line_volt": 2, "voltage_any": 2, "frequency": 2, "pf": 1.5,
            "current": 1, "power": 1, "energy": 1, "energy_net": 0.5, "thd": 0.5,
            "angle": 0.5, "temperature": 0.5}
# wynik dla dokładnego zera: napięcie/częstotliwość 0 są podejrzane
_ZERO = {"voltage": 0.1, "line_volt": 0.1, "voltage_any": 0.1, "frequency": 0.05, "temperature": 0.3}


def _category(key, spec):
    q = QUANTITIES.get(key)
    if q:
        if key.startswith("voltage_"):
            return "line_volt" if q["group"] == "line_volt" else "voltage"
        if key.startswith("current_"):
            return "current"
        if key.startswith(("power_", "apparent_", "reactive_")):
            return "power"
        if key.startswith("pf_"):
            return "pf"
        if key.startswith("energy_"):
            return "energy_net" if key == "energy_net" else "energy"
        if key.startswith("thd_"):
            return "thd"
        if key.startswith("phase_angle"):
            return "angle"
        return {"frequency": "frequency", "temperature": "temperature"}.get(key)
    unit = normalize_unit(spec.get("unit") or "")
    group = spec.get("group") or ""
    if unit == "V":
        return group if group in ("voltage", "line_volt") else "voltage_any"
    return _UNIT_CATEGORY.get(unit) or _GROUP_CATEGORY.get(group)


def _value_score(cat, v):
    """(wynik 0..1, czy to wiarygodna wartość niezerowa)."""
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        return 0.0, False
    state = classify(v)
    if state == "zero":
        return _ZERO.get(cat, 0.5), False
    if state != "ok":
        return 0.0, False
    av = abs(v)
    if cat == "voltage":
        s = 1.0 if 80 <= v <= 280 else 0.0
    elif cat == "line_volt":
        s = 1.0 if 340 <= v <= 480 else 0.7 if 150 <= v <= 280 else 0.0    # 208/240 V (USA)
    elif cat == "voltage_any":
        s = 1.0 if 80 <= v <= 280 or 340 <= v <= 480 else 0.0
    elif cat == "frequency":
        s = 1.0 if 45 <= v <= 65 else 0.0
    elif cat == "pf":
        s = 1.0 if av <= 1.001 else 0.0
    elif cat == "current":
        s = 1.0 if 0 < v <= 1000 else 0.5 if -1000 <= v < 0 else 0.0     # prąd ze znakiem kierunku
    elif cat == "power":
        s = 1.0 if 1e-3 <= av <= 1e7 else 0.3 if av < 1e-3 else 0.0
    elif cat == "energy":
        # liczniki domowe i przemysłowe rzadko przekraczają 1 GWh; większe wartości to zwykle zły odczyt
        s = 1.0 if 0 < v <= 1e6 else 0.4 if 0 < v <= 1e8 else 0.0
    elif cat == "energy_net":
        s = 1.0
    elif cat == "thd":
        s = 1.0 if 0 < v <= 100 else 0.0
    elif cat == "angle":
        s = 1.0 if av <= 360 else 0.0
    elif cat == "temperature":
        s = 1.0 if -40 <= v <= 125 else 0.0
    else:
        s = 0.5
    return s, s >= 1.0


def plausibility(values, preset):
    """Jak bardzo odczytane wartości pasują do presetu (0..1).

    values: {klucz: wartość|None} (np. PresetReader.read()["values"]); klucze,
    których nie ma w values, są pomijane. preset: znormalizowany (rejestry z unit/group).
    Napięcia 80-280 V / 340-480 V, częstotliwość 45-65 Hz, |cos φ| <= 1, prądy
    0-1000 A, energia >= 0. Same zera (albo brak wiarygodnych wartości) = niski wynik.
    """
    registers = (preset or {}).get("registers") or {}
    probe = (preset or {}).get("probe")
    acc = wsum = 0.0
    nonzero = False
    finite = seen = 0
    for key, spec in registers.items():
        if key not in values:
            continue
        v = values[key]
        seen += 1
        if classify(v) == "ok":
            finite += 1
        cat = _category(key, spec if isinstance(spec, dict) else {})
        if cat is None:
            continue
        w = _WEIGHTS.get(cat, 1.0) * (2 if key == probe else 1)
        s, ok = _value_score(cat, v)
        acc += s * w
        wsum += w
        nonzero = nonzero or ok
    if not seen:
        return 0.0
    if not wsum:
        # nieznane wielkości: tylko czy wartości są "normalnymi" liczbami
        return round(0.5 * finite / seen, 3)
    score = acc / wsum
    if not nonzero:
        score *= 0.2
    return round(max(0.0, min(1.0, score)), 3)
