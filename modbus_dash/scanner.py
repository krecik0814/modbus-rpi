"""Skaner: odczyt zakresów rejestrów, wyszukiwanie urządzeń i rozpoznawanie liczników.

Długie operacje (skan Unit ID, rozpoznawanie modelu) działają jako zadania w
tle (JobManager), a przeglądarka odpytuje ich postęp.
"""

import contextlib
import itertools
import threading
import time

from . import heuristics
from .planner import PresetReader
from .presets import PresetError

MAX_REGS = 125
MAX_BITS = 2000
# Eastron i część liczników przyjmuje maks. 80 rejestrów i tylko parzyste długości
SCAN_CHUNK = 80

# klucze używane do szybkiego rozpoznawania modelu (w kolejności ważności)
DETECT_KEYS = ("voltage_l1", "voltage_l2", "voltage_l3", "frequency", "current_l1",
               "pf_l1", "pf_total", "power_total", "energy_import", "voltage_l12",
               "voltage_dc", "current_dc", "power_dc")


def _fatal(e):
    return getattr(e, "kind", "io") in ("connection", "timeout")


def read_range(bus, unit, function, start, end, chunk=SCAN_CHUNK, max_requests=None):
    """Czyta rejestry [start, end).

    Zwraca (wartości z None dla nieczytelnych, błąd|None, liczba zapytań, ostatni wyjątek
    Modbus|None, adres końca sprawdzonego zakresu). Gdy urządzenie odrzuci blok wyjątkiem
    Modbus, blok jest dzielony na pół aż do pojedynczych rejestrów - dzięki temu skan omija
    dziury w mapie rejestrów. Wyjątek 01 (niedozwolona funkcja) dotyczy całej funkcji, więc
    kończy skan od razu. Limit zapytań domyślnie rośnie z zakresem (dzielenie nieczytelnych
    obszarów kosztuje ok. 2 zapytania na rejestr).
    """
    if max_requests is None:
        max_requests = max(400, 2 * (end - start))
    bits = function in ("coil", "discrete")
    limit = MAX_BITS if bits else MAX_REGS
    chunk = max(1, min(chunk, limit))
    out = [None] * (end - start)
    pending = [(a, min(chunk, end - a)) for a in range(start, end, chunk)]
    requests = 0
    error = None
    last_exc = None
    covered = start
    while pending:
        addr, count = pending.pop(0)
        if requests >= max_requests:
            error = "przekroczono limit zapytań - zawęź zakres"
            break
        requests += 1
        try:
            if bits:
                vals = bus.read_bits(unit, function, addr, count)
            else:
                vals = bus.read_registers(unit, function, addr, count)
        except Exception as e:  # noqa: BLE001
            if _fatal(e):
                error = str(e)
                break
            last_exc = e
            if getattr(e, "code", None) == 1:
                error = f"{e} - urządzenie nie obsługuje tej funkcji, spróbuj innego typu rejestrów"
                covered = end
                break
            if count > 1:
                # parzyste podziały - pary rejestrów (float32) zostają razem; 3 -> 2 + 1
                half = count // 2
                if half % 2:
                    half = half - 1 if half > 1 else (2 if count > 2 else 1)
                pending[0:0] = [(addr, half), (addr + half, count - half)]
            else:
                covered = max(covered, addr + 1)
            continue
        covered = max(covered, addr + count)
        for i, v in enumerate(vals[:count]):
            out[addr - start + i] = int(v)
    return out, error, requests, last_exc, covered


def scan(bus, unit, function, start, end, step=2):
    """Pełny skan zakresu z analizą heurystyczną (rejestry) albo lista bitów."""
    t0 = time.monotonic()
    values, error, requests, last_exc, covered = read_range(bus, unit, function, start, end)
    readable = sum(v is not None for v in values)
    if not readable and not error and last_exc is not None:
        error = f"każde zapytanie odrzucone: {last_exc}"
    result = {
        "function": function,
        "start": start,
        "end": end,
        "requests": requests,
        "error": error,
        "readable": readable,
        "exception_code": getattr(last_exc, "code", None),
        "unchecked_from": covered if covered < end else None,
    }
    if function in ("coil", "discrete"):
        result["bits"] = [{"address": start + i, "value": v} for i, v in enumerate(values)]
        result["count"] = len(values)
    else:
        rows = heuristics.analyze_registers(start, values, step=step)
        result["registers"] = rows
        result["count"] = len(rows)
    # nieczytelne = sprawdzone i odrzucone; zakres za limitem zapytań nie był sprawdzany
    result["unreadable"] = _ranges(start, values[:max(0, covered - start)])
    result["duration_ms"] = round((time.monotonic() - t0) * 1000, 1)
    return result


def _ranges(start, values):
    """Zakresy adresów, których nie udało się odczytać: [[od, do], ...]."""
    out = []
    for is_none, grp in itertools.groupby(enumerate(values), key=lambda iv: iv[1] is None):
        if is_none:
            grp = list(grp)
            out.append([start + grp[0][0], start + grp[-1][0]])
    return out


@contextlib.contextmanager
def quick(bus, timeout, settle=None):
    """Tymczasowo krótszy timeout (o ile transport to wspiera). settle: patrz Bus.override."""
    override = getattr(bus, "override", None)
    if override is None:
        yield
        return
    kw = {} if settle is None else {"settle": settle}
    with override(timeout=timeout, retries=0, **kw):
        yield


def scan_units(job, bus, first, last, function, address, count, timeout):
    """Szuka urządzeń odpowiadających na Unit ID z zakresu [first, last]."""
    units = list(range(first, last + 1))
    job.progress(0, len(units), "Szukanie urządzeń...")
    found = []
    # brak odpowiedzi to tu norma - bez długiego czekania na spóźnione odpowiedzi
    # (odpowiedź innego urządzenia odrzuca kontrola Unit ID w transporcie)
    with quick(bus, timeout, settle=0.05):
        for i, unit in enumerate(units):
            if job.cancelled:
                break
            entry = {"unit": unit}
            try:
                regs = bus.read_registers(unit, function, address, count)
                entry.update(ok=True, registers=regs)
            except Exception as e:  # noqa: BLE001
                kind = getattr(e, "kind", "io")
                if kind == "exception":
                    # odpowiedź wyjątkiem też oznacza, że urządzenie istnieje
                    entry.update(ok=True, exception=str(e))
                elif kind == "connection" and getattr(e, "code", None) == 0x0A:
                    entry = None  # bramka: brak ścieżki do tego Unit ID - szukamy dalej
                elif kind == "connection":
                    job.fail(str(e))
                    return
                else:
                    entry = None
            if entry:
                found.append(entry)
                job.results.append(entry)
            job.progress(i + 1, len(units), f"Unit ID {unit}: znaleziono {len(found)}")
    job.finish({"found": found})


def _alive(bus, unit):
    """Czy urządzenie w ogóle odpowiada (także wyjątkiem)? Zwraca (bool, błąd_połączenia)."""
    for func in ("input", "holding"):
        try:
            bus.read_registers(unit, func, 0, 1)
            return True, None
        except Exception as e:  # noqa: BLE001
            kind = getattr(e, "kind", "io")
            if kind == "exception":
                return True, None
            if kind == "connection":
                return False, str(e)
    return False, None


def detect_preset(job, bus, unit, store, timeout=None):
    """Próbuje wszystkie poprawne presety i ocenia wiarygodność odczytanych wartości."""
    summaries = [p for p in store.list() if p["valid"]]
    job.progress(0, len(summaries), "Sprawdzanie urządzenia...")
    alive, conn_error = _alive(bus, unit)
    if not alive:
        job.fail(conn_error or "Brak odpowiedzi od urządzenia - sprawdź Unit ID i parametry magistrali")
        return
    ranked = []
    for i, summary in enumerate(summaries):
        if job.cancelled:
            break
        job.progress(i, len(summaries), summary["name"])
        try:
            preset = store.get(summary["id"])
        except (PresetError, OSError, ValueError):
            preset = None
        if not preset or not preset["registers"]:
            continue
        keys = [k for k in DETECT_KEYS if k in preset["registers"]]
        if preset.get("probe") and preset["probe"] not in keys:
            keys.insert(0, preset["probe"])
        if not keys:
            keys = list(preset["registers"])[:4]
        keys = keys[:6]
        keys += [preset["registers"][k]["scale_from"] for k in keys
                 if preset["registers"][k].get("scale_from") and preset["registers"][k]["scale_from"] not in keys]
        sub = {**preset, "registers": {k: preset["registers"][k] for k in keys}}
        try:
            with quick(bus, timeout) if timeout else contextlib.nullcontext():
                res = PresetReader(sub).read(bus, unit)
        except Exception as e:  # noqa: BLE001
            res = {"values": {}, "errors": {"*": str(e)}, "ok": False}
        score = heuristics.plausibility(res["values"], sub) if res["ok"] else 0.0
        ranked.append({
            "id": summary["id"], "name": summary["name"],
            "manufacturer": summary.get("manufacturer"), "model": summary.get("model"),
            "builtin": summary["builtin"], "score": round(score, 3),
            "matched": sum(k in DETECT_KEYS for k in sub["registers"]),
            "meta": {k: {"label": r["label"], "unit": r["unit"], "decimals": r["decimals"]}
                     for k, r in sub["registers"].items()},
            "values": {k: v for k, v in res["values"].items() if v is not None},
        })
    job.progress(len(summaries), len(summaries), "Gotowe")
    # przy remisie wygrywa preset z większą liczbą kluczy kanonicznych, potem wbudowany
    ranked.sort(key=lambda e: (-e["score"], -e["matched"], not e["builtin"]))
    job.finish({"candidates": ranked[:15], "checked": len(ranked)})


class Job:
    def __init__(self, job_id, kind):
        self.id = job_id
        self.kind = kind
        self.state = "running"
        self.done = 0
        self.total = 0
        self.message = ""
        self.results = []
        self.result = None
        self.error = None
        self.cancelled = False
        self.started = time.time()
        self.finished = None

    def progress(self, done, total, message=""):
        self.done, self.total, self.message = done, total, message

    def finish(self, result):
        self.result = result
        self.state = "cancelled" if self.cancelled else "done"
        self.finished = time.time()

    def fail(self, error):
        self.error = error
        self.state = "error"
        self.finished = time.time()

    def to_dict(self):
        return {
            "id": self.id, "kind": self.kind, "state": self.state,
            "progress": {"done": self.done, "total": self.total, "message": self.message},
            "results": list(self.results), "result": self.result, "error": self.error,
            "started": self.started, "finished": self.finished,
        }


class JobManager:
    def __init__(self, keep=20):
        self._jobs = {}
        self._lock = threading.Lock()
        self._ids = itertools.count(1)
        self._keep = keep

    def start(self, kind, fn, *args):
        job = Job(f"{kind}-{next(self._ids)}", kind)
        with self._lock:
            running = [j for j in self._jobs.values()
                       if j.kind == kind and j.state == "running" and not j.cancelled]
            if running:
                raise RuntimeError("takie zadanie już trwa - poczekaj lub je anuluj")
            self._jobs[job.id] = job
            for old in sorted(self._jobs.values(), key=lambda j: j.started)[:-self._keep]:
                if old.state != "running":
                    del self._jobs[old.id]

        def run():
            try:
                fn(job, *args)
                if job.state == "running":
                    job.finish(job.result)
            except Exception as e:  # noqa: BLE001
                job.fail(str(e) or e.__class__.__name__)

        threading.Thread(target=run, name=f"job-{job.id}", daemon=True).start()
        return job

    def get(self, job_id):
        with self._lock:
            return self._jobs.get(job_id)

    def cancel(self, job_id):
        job = self.get(job_id)
        if job and job.state == "running":
            job.cancelled = True
            return True
        return False
