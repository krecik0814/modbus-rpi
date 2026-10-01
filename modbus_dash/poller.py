"""Odpytywanie urządzeń w tle.

Jeden wątek na magistralę: urządzenia na tej samej magistrali RS-485 są
odpytywane po kolei (magistrala jest half-duplex), a przeglądarki czytają
ostatnie wartości z pamięci zamiast generować ruch na magistrali.
"""

import logging
import threading
import time
from collections import deque

from .presets import PresetError

log = logging.getLogger("modbus-dash.poller")

MAX_BACKOFF = 30.0


class DeviceRuntime:
    def __init__(self, dev_id, cfg, history_points):
        self.id = dev_id
        self.cfg = cfg
        self.preset = None
        self.preset_error = None
        self.reader = None
        self.latest = None
        self.keys = []
        self.history = deque(maxlen=history_points)
        self.next_due = 0.0
        self.fail_count = 0
        self.polls = 0
        self.failures = 0
        self.last_ok_ts = None
        self.lock = threading.Lock()

    def set_preset(self, preset, reader_factory):
        """Ustawia (nowy) preset; czyści historię gdy zmienił się zestaw rejestrów."""
        if preset == self.preset:
            return
        keys = list(preset["registers"]) if preset else []
        with self.lock:
            self.preset = preset
            self.reader = reader_factory(preset) if preset else None
            if keys != self.keys:
                self.keys = keys
                self.history.clear()
                self.latest = None

    def record(self, sample):
        with self.lock:
            self.polls += 1
            self.latest = sample
            if sample["ok"]:
                self.fail_count = 0
                self.last_ok_ts = sample["ts"]
                self.history.append((sample["ts"], tuple(sample["values"].get(k) for k in self.keys)))
            else:
                self.fail_count += 1
                self.failures += 1


class Poller:
    def __init__(self, config_store, preset_store, bus_manager, reader_factory, transport_config_cls):
        self.config = config_store
        self.presets = preset_store
        self.buses = bus_manager
        self.reader_factory = reader_factory
        self.TransportConfig = transport_config_cls
        self.listeners = []
        self._lock = threading.RLock()
        self._devices = {}          # id -> DeviceRuntime
        self._workers = {}          # bus id -> (thread, stop_event, wake_event)
        self._running = False

    # ── cykl życia ─────────────────────────────────────────────
    def start(self):
        self._running = True
        self.reload()

    def stop(self):
        self._running = False
        with self._lock:
            workers = list(self._workers.values())
            self._workers.clear()
        for _, stop, wake in workers:
            stop.set()
            wake.set()
        for th, _, _ in workers:
            th.join(timeout=5)

    def add_listener(self, fn):
        """fn(device_id, runtime, sample) - wywoływane po każdym odczycie."""
        self.listeners.append(fn)

    def reload(self, *_):
        """Synchronizuje urządzenia i wątki z bieżącą konfiguracją."""
        cfg = self.config.get()
        points = cfg["history"]["memory_points"]
        with self._lock:
            for dev_id in list(self._devices):
                if dev_id not in cfg["devices"]:
                    del self._devices[dev_id]
            for dev_id, dcfg in cfg["devices"].items():
                rt = self._devices.get(dev_id)
                if rt is None or rt.history.maxlen != points:
                    rt = DeviceRuntime(dev_id, dcfg, points)
                    self._devices[dev_id] = rt
                else:
                    rt.cfg = dcfg
                    rt.next_due = 0.0
                self._refresh_preset(rt)
            if not self._running:
                return
            wanted = {d["bus"] for d in cfg["devices"].values() if d["enabled"]}
            for bus_id in list(self._workers):
                if bus_id not in wanted:
                    _, stop, wake = self._workers.pop(bus_id)
                    stop.set()
                    wake.set()
            for bus_id in wanted:
                if bus_id in self._workers:
                    self._workers[bus_id][2].set()
                    continue
                stop, wake = threading.Event(), threading.Event()
                th = threading.Thread(target=self._worker, args=(bus_id, stop, wake),
                                      name=f"poller-{bus_id}", daemon=True)
                self._workers[bus_id] = (th, stop, wake)
                th.start()

    def _refresh_preset(self, rt):
        pid = rt.cfg.get("preset")
        if not pid:
            rt.preset_error = None
            rt.set_preset(None, self.reader_factory)
            return
        try:
            preset = self.presets.get(pid)
            rt.preset_error = None if preset else f"preset '{pid}' nie istnieje"
        except PresetError as e:
            preset, rt.preset_error = None, f"preset '{pid}' jest niepoprawny: {e}"
        except (OSError, ValueError) as e:
            preset, rt.preset_error = None, f"nie można wczytać presetu '{pid}': {e}"
        rt.set_preset(preset, self.reader_factory)

    # ── pętla odpytywania ──────────────────────────────────────
    def _bus_for(self, bus_id):
        bus_cfg = self.config.get()["buses"].get(bus_id)
        if bus_cfg is None:
            raise RuntimeError(f"brak magistrali '{bus_id}'")
        cfg = {k: v for k, v in bus_cfg.items() if k not in ("name", "locked")}
        return self.buses.get(self.TransportConfig.from_dict(cfg))

    def _worker(self, bus_id, stop, wake):
        last_preset_check = 0.0
        while not stop.is_set():
            now = time.monotonic()
            with self._lock:
                devs = [rt for rt in self._devices.values()
                        if rt.cfg["bus"] == bus_id and rt.cfg["enabled"]]
            if now - last_preset_check > 2.0:
                # wykrywa edycję pliku presetu (cache w PresetStore po mtime)
                for rt in devs:
                    self._refresh_preset(rt)
                last_preset_check = now
            due = [rt for rt in devs if rt.reader and rt.next_due <= now]
            for rt in sorted(due, key=lambda r: r.next_due):
                if stop.is_set():
                    return
                self.poll(rt, bus_id)
            with self._lock:
                pending = [rt.next_due for rt in self._devices.values()
                           if rt.cfg["bus"] == bus_id and rt.cfg["enabled"] and rt.reader]
            delay = min(pending) - time.monotonic() if pending else 1.0
            wake.wait(timeout=max(0.02, min(delay, 1.0)))
            wake.clear()

    def poll(self, rt, bus_id=None):
        """Jeden odczyt urządzenia (wywoływany z wątku magistrali lub na żądanie)."""
        bus_id = bus_id or rt.cfg["bus"]
        started = time.monotonic()
        reader = rt.reader
        if reader is None:
            return None
        try:
            bus = self._bus_for(bus_id)
            res = reader.read(bus, rt.cfg["unit"])
            error = None if res["ok"] else _first_error(res["errors"])
        except Exception as e:  # noqa: BLE001 - błąd konfiguracji/transportu nie może zabić wątku
            log.warning("Odczyt %s: %s", rt.id, e)
            res = {"values": {}, "errors": {}, "duration_ms": 0.0, "requests": 0, "ok": False}
            error = str(e)
        sample = {
            "ts": time.time(),
            "values": res["values"],
            "errors": res["errors"],
            "ok": res["ok"],
            "error": error,
            "duration_ms": res["duration_ms"],
            "requests": res["requests"],
        }
        rt.record(sample)
        interval = rt.cfg["interval"]
        if not sample["ok"] and rt.fail_count > 1:
            interval = min(MAX_BACKOFF, interval * 2 ** min(rt.fail_count - 1, 5))
        rt.next_due = max(started + interval, time.monotonic() + 0.05)
        for fn in list(self.listeners):
            try:
                fn(rt.id, rt, sample)
            except Exception:  # noqa: BLE001
                log.exception("Listener odczytu")
        return sample

    # ── dostęp do danych ───────────────────────────────────────
    def runtime(self, dev_id):
        with self._lock:
            return self._devices.get(dev_id)

    def device_ids(self):
        with self._lock:
            return list(self._devices)

    def read_now(self, dev_id):
        rt = self.runtime(dev_id)
        if rt is None:
            return None
        self._refresh_preset(rt)
        return self.poll(rt)

    def status(self, rt):
        now = time.time()
        latest = rt.latest
        if not rt.cfg["enabled"]:
            state = "disabled"
        elif rt.preset_error or not rt.cfg.get("preset"):
            state = "no_preset"
        elif latest is None:
            state = "waiting"
        elif not latest["ok"]:
            state = "error"
        elif now - latest["ts"] > max(10.0, rt.cfg["interval"] * 5):
            state = "stale"
        else:
            state = "ok"
        return {
            "state": state,
            "error": rt.preset_error or (latest or {}).get("error"),
            "ts": latest["ts"] if latest else None,
            "age": round(now - latest["ts"], 1) if latest else None,
            "last_ok_ts": rt.last_ok_ts,
            "polls": rt.polls,
            "failures": rt.failures,
            "duration_ms": (latest or {}).get("duration_ms"),
            "requests": (latest or {}).get("requests"),
        }

    def values(self, dev_id):
        """Ostatnie wartości + metadane rejestrów do dashboardu."""
        rt = self.runtime(dev_id)
        if rt is None:
            return None
        preset = rt.preset
        latest = rt.latest or {}
        meta = {}
        if preset:
            for k, s in preset["registers"].items():
                meta[k] = {"label": s["label"], "unit": s["unit"], "group": s["group"],
                           "decimals": s["decimals"]}
        return {
            "device": {"id": rt.id, **rt.cfg},
            "preset": ({"id": preset.get("id"), "name": preset.get("name"),
                        "manufacturer": preset.get("manufacturer"), "model": preset.get("model"),
                        "phases": preset.get("phases")} if preset else None),
            "meta": meta,
            "values": latest.get("values", {}),
            "errors": latest.get("errors", {}),
            "status": self.status(rt),
        }

    def history(self, dev_id, seconds=600, keys=None):
        rt = self.runtime(dev_id)
        if rt is None:
            return None
        cutoff = time.time() - seconds
        with rt.lock:
            all_keys = list(rt.keys)
            rows = [r for r in rt.history if r[0] >= cutoff]
        idx = [all_keys.index(k) for k in keys if k in all_keys] if keys else list(range(len(all_keys)))
        return {
            "keys": [all_keys[i] for i in idx],
            "points": [[round(ts, 3)] + [vals[i] for i in idx] for ts, vals in rows],
        }


def _first_error(errors):
    for msg in errors.values():
        return msg
    return "brak danych"
