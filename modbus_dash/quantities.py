"""Słownik kanonicznych wielkości mierzonych przez liczniki.

Presety powinny używać tych kluczy, gdy znaczenie się zgadza - dzięki temu
symulator potrafi wypełnić dowolny preset, a integracje (MQTT / Home Assistant,
Prometheus) znają typ wielkości.
"""

import re

PHASES = ("l1", "l2", "l3")

# klucz -> {label, unit, group, decimals}
QUANTITIES = {}


def _add(key, label, unit, group, decimals):
    QUANTITIES[key] = {"label": label, "unit": unit, "group": group, "decimals": decimals}


for p in PHASES:
    _add(f"voltage_{p}", f"Napięcie {p.upper()}", "V", "voltage", 1)
for a, b in (("l1", "l2"), ("l2", "l3"), ("l3", "l1")):
    _add(f"voltage_{a}{b[-1]}", f"Napięcie {a.upper()}-{b.upper()}", "V", "line_volt", 1)
_add("voltage_ln_avg", "Napięcie fazowe śr.", "V", "voltage", 1)
_add("voltage_ll_avg", "Napięcie międzyfazowe śr.", "V", "line_volt", 1)

for p in PHASES:
    _add(f"current_{p}", f"Prąd {p.upper()}", "A", "current", 2)
_add("current_n", "Prąd N", "A", "current", 2)
_add("current_total", "Prąd Σ", "A", "current", 2)
_add("current_avg", "Prąd śr.", "A", "current", 2)

for name, label, unit, dec in (("power", "Moc czynna", "W", 0),
                               ("apparent", "Moc pozorna", "VA", 0),
                               ("reactive", "Moc bierna", "var", 0)):
    for p in PHASES:
        _add(f"{name}_{p}", f"{label} {p.upper()}", unit, "power", dec)
    _add(f"{name}_total", f"{label} Σ", unit, "total", dec)

for p in PHASES:
    _add(f"pf_{p}", f"cos φ {p.upper()}", "", "pf", 3)
_add("pf_total", "cos φ Σ", "", "pf", 3)
for p in PHASES:
    _add(f"phase_angle_{p}", f"Kąt fazowy {p.upper()}", "°", "system", 1)
_add("phase_angle_total", "Kąt fazowy Σ", "°", "system", 1)
_add("frequency", "Częstotliwość", "Hz", "system", 2)

_add("energy_import", "Energia pobrana", "kWh", "energy", 2)
_add("energy_export", "Energia oddana", "kWh", "energy", 2)
_add("energy_total", "Energia całkowita", "kWh", "energy", 2)
_add("energy_net", "Energia netto", "kWh", "energy", 2)
for p in PHASES:
    _add(f"energy_import_{p}", f"Energia pobrana {p.upper()}", "kWh", "energy", 2)
    _add(f"energy_export_{p}", f"Energia oddana {p.upper()}", "kWh", "energy", 2)
    _add(f"energy_total_{p}", f"Energia całkowita {p.upper()}", "kWh", "energy", 2)
for t in ("t1", "t2"):
    _add(f"energy_import_{t}", f"Energia pobrana {t.upper()}", "kWh", "energy", 2)
    _add(f"energy_export_{t}", f"Energia oddana {t.upper()}", "kWh", "energy", 2)
    _add(f"energy_total_{t}", f"Energia całkowita {t.upper()}", "kWh", "energy", 2)
_add("power_import", "Moc pobierana", "W", "total", 0)
_add("power_export", "Moc oddawana", "W", "total", 0)
_add("energy_reactive_import", "Energia bierna pobrana", "kvarh", "energy", 2)
_add("energy_reactive_export", "Energia bierna oddana", "kvarh", "energy", 2)
_add("energy_reactive_total", "Energia bierna całkowita", "kvarh", "energy", 2)
_add("energy_apparent", "Energia pozorna", "kVAh", "energy", 2)

for p in PHASES:
    _add(f"thd_v_{p}", f"THD U {p.upper()}", "%", "thd", 1)
_add("thd_v_avg", "THD U śr.", "%", "thd", 1)
for p in PHASES:
    _add(f"thd_i_{p}", f"THD I {p.upper()}", "%", "thd", 1)
_add("thd_i_avg", "THD I śr.", "%", "thd", 1)

_add("power_demand", "Moc szczytowa (demand)", "W", "system", 0)
_add("power_demand_max", "Maks. moc szczytowa", "W", "system", 0)
_add("current_demand", "Prąd szczytowy (demand)", "A", "system", 2)
_add("temperature", "Temperatura", "°C", "system", 1)


# Home Assistant: device_class / state_class po jednostce i kluczu
_HA_UNIT = {
    "V": ("voltage", "measurement"),
    "A": ("current", "measurement"),
    "W": ("power", "measurement"),
    "kW": ("power", "measurement"),
    "VA": ("apparent_power", "measurement"),
    "var": ("reactive_power", "measurement"),
    "Hz": ("frequency", "measurement"),
    "kWh": ("energy", "total_increasing"),
    "Wh": ("energy", "total_increasing"),
    "MWh": ("energy", "total_increasing"),
    "°C": ("temperature", "measurement"),
}
_UNIT_NORMALIZE = {"VAr": "var", "VAR": "var", "kVAr": "kvar", "kVArh": "kvarh", "kVARh": "kvarh",
                   "kwh": "kWh", "KWh": "kWh", "wh": "Wh", "hz": "Hz", "deg": "°",
                   "mwh": "MWh", "MWH": "MWh", "VArh": "varh", "VARh": "varh", "vah": "VAh"}


def normalize_unit(unit):
    return _UNIT_NORMALIZE.get(unit, unit)


def ha_classes(key, unit):
    """(device_class, state_class) dla Home Assistant; None gdy nieznane."""
    unit = normalize_unit(unit or "")
    if key.startswith("pf_"):
        return "power_factor", "measurement"
    dc, sc = _HA_UNIT.get(unit, (None, "measurement"))
    net = "net" in key.split("_")  # bilans (import - eksport) może maleć
    if dc == "energy":
        sc = "total" if net else "total_increasing"
    if unit in ("kvarh", "varh", "kVAh", "VAh"):  # liczniki bez klasy urządzenia w HA
        return None, "total" if net else "total_increasing"
    return dc, sc


_KEY_SAFE = re.compile(r"[^A-Za-z0-9_]+")


def safe_key(key):
    """Klucz bezpieczny dla nazw metryk / topiców MQTT."""
    return _KEY_SAFE.sub("_", str(key)).strip("_") or "value"
