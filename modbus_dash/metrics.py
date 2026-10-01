"""Eksport metryk w formacie tekstowym Prometheusa (GET /metrics)."""

import math

from . import __version__
from .quantities import normalize_unit


def _esc(v):
    return str(v).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _labels(**kw):
    return "{" + ",".join(f'{k}="{_esc(v)}"' for k, v in kw.items()) + "}"


def _num(v):
    if v is None or (isinstance(v, float) and not math.isfinite(v)):
        return None
    return repr(float(v))


# metryki stanu odpytywania: nazwa -> (typ, opis)
_STATUS_METRICS = {
    "modbus_dash_up": ("gauge", "1 gdy ostatni odczyt się powiódł"),
    "modbus_dash_polls_total": ("counter", "Liczba odczytów"),
    "modbus_dash_poll_failures_total": ("counter", "Liczba nieudanych odczytów"),
    "modbus_dash_last_poll_timestamp_seconds": ("gauge", "Czas ostatniego odczytu"),
    "modbus_dash_last_success_timestamp_seconds": ("gauge", "Czas ostatniego udanego odczytu"),
    "modbus_dash_poll_duration_seconds": ("gauge", "Czas trwania ostatniego odczytu"),
}


def render(poller):
    out = [
        "# HELP modbus_dash_info Informacje o wersji",
        "# TYPE modbus_dash_info gauge",
        f"modbus_dash_info{_labels(version=__version__)} 1",
        "# HELP modbus_dash_value Wartość rejestru urządzenia (jednostka w etykiecie unit)",
        "# TYPE modbus_dash_value gauge",
    ]
    status = {metric: [] for metric in _STATUS_METRICS}  # metryka -> [(etykiety, wartość)]
    for dev_id in sorted(poller.device_ids()):
        data = poller.values(dev_id)
        if not data:
            continue
        st = data["status"]
        name = data["device"].get("name", dev_id)
        if st["state"] == "ok":
            for key, v in sorted(data["values"].items()):
                num = _num(v)
                if num is None:
                    continue
                m = data["meta"].get(key, {})
                out.append("modbus_dash_value" + _labels(
                    device=dev_id, device_name=name, key=key, label=m.get("label", key),
                    unit=normalize_unit(m.get("unit", "")), group=m.get("group", "other")) + f" {num}")
        lab = _labels(device=dev_id, device_name=name)
        status["modbus_dash_up"].append((lab, 1 if st["state"] == "ok" else 0))
        status["modbus_dash_polls_total"].append((lab, st["polls"]))
        status["modbus_dash_poll_failures_total"].append((lab, st["failures"]))
        if st["ts"]:
            status["modbus_dash_last_poll_timestamp_seconds"].append((lab, round(st["ts"], 3)))
        if st["last_ok_ts"]:
            status["modbus_dash_last_success_timestamp_seconds"].append((lab, round(st["last_ok_ts"], 3)))
        if st["duration_ms"] is not None:
            status["modbus_dash_poll_duration_seconds"].append((lab, st["duration_ms"] / 1000.0))
    for metric, (typ, help_) in _STATUS_METRICS.items():
        if status[metric]:
            out += [f"# HELP {metric} {help_}", f"# TYPE {metric} {typ}"]
            out += [f"{metric}{lab} {v}" for lab, v in status[metric]]
    return "\n".join(out) + "\n"
