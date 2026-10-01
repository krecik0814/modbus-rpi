"""Aplikacja Flask: interfejs webowy i REST API."""

import csv
import hmac
import io
import ipaddress
import logging
import platform
import socket
import time
from datetime import datetime
from urllib.parse import urlsplit

from flask import Flask, Response, jsonify, request, send_from_directory

from . import __version__, metrics, mqtt, scanner
from .config import ConfigError, validate_mqtt
from .planner import PresetReader
from .presets import PresetError, PresetFileError, valid_id
from .transport import ModbusError, TransportConfig, list_serial_ports, pymodbus_version

log = logging.getLogger("modbus-dash.web")

SCAN_FUNCTIONS = ("input", "holding", "coil", "discrete")
MAX_SCAN_SPAN = 2000


class ApiError(Exception):
    def __init__(self, message, status=400, **extra):
        super().__init__(message)
        self.status = status
        self.extra = extra


def _body():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ApiError("oczekiwano obiektu JSON w treści zapytania")
    return data


def _opt_body():
    """Treść opcjonalna: brak albo pusta = {}, ale JSON innego typu niż obiekt to błąd 400."""
    if not request.get_data(cache=True):
        return {}
    return _body()


def _host_name(host):
    """Nagłówek Host -> sama nazwa (bez portu, małe litery; [IPv6] bez nawiasów)."""
    host = (host or "").strip().lower()
    if host.startswith("["):
        return host[1:].partition("]")[0]
    if host.count(":") == 1:
        host = host.partition(":")[0]
    return host.rstrip(".")


def host_allowed(host, extra=()):
    """Czy dashboard może odpowiadać pod tą nazwą hosta (ochrona przed DNS rebinding).

    Zawsze: adresy IP, localhost (*.localhost), nazwa tego komputera (także .local).
    extra: nazwy z --allowed-host; "*.dom.lan" = dowolna poddomena, "*" = każda nazwa.
    """
    name = _host_name(host)
    if not name:
        return False
    try:
        ipaddress.ip_address(name.split("%")[0])
        return True
    except ValueError:
        pass
    if name == "localhost" or name.endswith(".localhost"):
        return True
    local = socket.gethostname().lower().rstrip(".")
    short = local.split(".")[0]
    if name in (local, short, f"{short}.local", f"{local}.local"):
        return True
    for pattern in extra:
        pattern = str(pattern).strip().lower().rstrip(".")
        if pattern == "*" or name == pattern or (pattern.startswith("*.") and name.endswith(pattern[1:])):
            return True
    return False


def _csv_safe(text):
    """Komórka CSV, której arkusz nie uzna za formułę (=, +, -, @ na początku)."""
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def _int(data, name, default, lo, hi):
    v = data.get(name, default)
    if isinstance(v, str) and v.strip():
        try:
            v = int(v.strip(), 16) if v.strip().lower().startswith("0x") else int(v.strip())
        except ValueError:
            raise ApiError(f"'{name}' musi być liczbą całkowitą") from None
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    if isinstance(v, bool) or not isinstance(v, int):
        raise ApiError(f"'{name}' musi być liczbą całkowitą")
    if not lo <= v <= hi:
        raise ApiError(f"'{name}' musi być w zakresie {lo}-{hi}")
    return v


def _float(data, name, default, lo, hi):
    v = data.get(name, default)
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ApiError(f"'{name}' musi być liczbą")
    if not lo <= v <= hi:
        raise ApiError(f"'{name}' musi być w zakresie {lo}-{hi}")
    return float(v)


def create_app(ctx):
    """ctx: AppContext (patrz app.py) z polami config, presets, buses, poller, history,
    mqtt, jobs, simulator_info, options, base_dir."""
    app = Flask(__name__, static_folder=None)
    app.config["JSON_SORT_KEYS"] = False  # Flask < 2.3
    if hasattr(app, "json") and hasattr(app.json, "sort_keys"):
        app.json.sort_keys = False      # Flask >= 2.3 - kolejność pól presetu jak w pliku
    app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024
    templates_dir = str(ctx.base_dir / "templates")
    static_dir = str(ctx.base_dir / "static")
    opts = ctx.options

    # ── bezpieczeństwo ─────────────────────────────────────────
    @app.before_request
    def _guard():
        auth = opts.get("auth")
        host = request.headers.get("Host")  # bez nagłówka (HTTP/1.0, healthcheck) - nie przeglądarka
        if not auth and host is not None and not host_allowed(host, opts.get("allowed_hosts") or ()):
            # bez hasła strona z obcej domeny przemapowanej na ten adres (DNS rebinding)
            # mogłaby czytać i zmieniać konfigurację - odpowiadamy tylko znanym nazwom
            msg = (f"nieznana nazwa hosta '{_host_name(host)}' - dodaj ją opcją --allowed-host "
                   "albo włącz logowanie (--auth)")
            if request.path.startswith("/api/"):
                return jsonify({"error": msg}), 403
            return Response(msg + "\n", 403, mimetype="text/plain; charset=utf-8")
        if auth:
            a = request.authorization
            ok = (a is not None and a.type == "basic"
                  and hmac.compare_digest((a.username or "").encode(), auth[0].encode())
                  and hmac.compare_digest((a.password or "").encode(), auth[1].encode()))
            if not ok:
                return Response("Wymagane logowanie\n", 401,
                                {"WWW-Authenticate": 'Basic realm="Modbus Dash", charset="UTF-8"'})
        if request.method in ("POST", "PUT", "DELETE", "PATCH"):
            # ochrona przed CSRF: obce strony nie mogą wysyłać zapytań do API
            if request.headers.get("Sec-Fetch-Site") == "cross-site":
                return jsonify({"error": "zapytanie z obcej strony odrzucone"}), 403
            origin = request.headers.get("Origin")
            if origin and origin != "null" and urlsplit(origin).netloc != request.host:
                return jsonify({"error": "zapytanie z obcej strony odrzucone"}), 403
            if request.method in ("POST", "PUT", "PATCH") and request.content_length and not request.is_json:
                return jsonify({"error": "wymagany Content-Type: application/json"}), 415
        return None

    @app.after_request
    def _headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        if request.path.startswith("/api/"):
            resp.headers.setdefault("Cache-Control", "no-store")
        return resp

    @app.errorhandler(ApiError)
    def _api_error(e):
        return jsonify({"error": str(e), **e.extra}), e.status

    @app.errorhandler(PresetError)
    def _preset_error(e):
        return jsonify({"error": "preset niepoprawny", "errors": e.errors}), 400

    @app.errorhandler(PresetFileError)
    def _preset_file_error(e):
        return jsonify({"error": str(e), "id": e.preset_id, "text": e.text}), 422

    @app.errorhandler(ConfigError)
    def _config_error(e):
        return jsonify({"error": str(e)}), 400

    @app.errorhandler(ModbusError)
    def _modbus_error(e):
        kind = getattr(e, "kind", "io")
        status = {"invalid": 400, "timeout": 504}.get(kind, 502)
        return jsonify({"error": str(e), "kind": kind, "code": getattr(e, "code", None)}), status

    @app.errorhandler(404)
    def _not_found(e):
        if request.path.startswith("/api/"):
            return jsonify({"error": "nie znaleziono"}), 404
        return e

    @app.errorhandler(405)
    def _bad_method(e):
        return jsonify({"error": "niedozwolona metoda"}), 405

    @app.errorhandler(413)
    def _too_large(e):
        return jsonify({"error": "zbyt duże zapytanie"}), 413

    @app.errorhandler(Exception)
    def _unexpected(e):
        if hasattr(e, "code") and hasattr(e, "get_response"):  # wyjątki HTTP werkzeug
            return e
        log.exception("Nieobsłużony wyjątek")
        return jsonify({"error": f"błąd serwera: {e.__class__.__name__}: {e}"}), 500

    # ── pomocnicze ─────────────────────────────────────────────
    def bus_by_id(bus_id):
        buses = ctx.config.get()["buses"]
        if bus_id not in buses:
            raise ApiError(f"nieznana magistrala '{bus_id}'", 404)
        cfg = {k: v for k, v in buses[bus_id].items() if k not in ("name", "locked")}
        return ctx.buses.get(TransportConfig.from_dict(cfg))

    def bus_from_request(data):
        """Magistrala z pola 'bus' albo (zgodność wstecz) z host/port dla trybu TCP."""
        if data.get("bus"):
            return bus_by_id(str(data["bus"]))
        cfg = ctx.config.get()["buses"]["default"]
        if cfg["kind"] in ("rtu", "ascii") or "host" not in data:
            return bus_by_id("default")
        if not opts.get("allow_any_host", True):
            raise ApiError("łączenie z dowolnym hostem jest wyłączone - użyj skonfigurowanej magistrali", 403)
        host = str(data.get("host") or "127.0.0.1").strip()
        port = _int(data, "port", 502, 1, 65535)
        base = {k: v for k, v in cfg.items() if k in ("timeout", "retries", "delay_ms")}
        kind = cfg["kind"] if cfg["kind"] in ("tcp", "rtu_over_tcp", "udp") else "tcp"
        return ctx.buses.get(TransportConfig.from_dict({**base, "kind": kind, "host": host, "port": port}))

    def unit_of(data, default=1):
        return _int(data, "unit", default, 0, 255)

    # ── strony ─────────────────────────────────────────────────
    @app.route("/")
    def index():
        return send_from_directory(templates_dir, "index.html")

    @app.route("/static/<path:filename>")
    def static_files(filename):
        return send_from_directory(static_dir, filename)

    # ── informacje ─────────────────────────────────────────────
    @app.get("/api/info")
    def api_info():
        cfg = ctx.config.get()
        default = cfg["buses"]["default"]
        return jsonify({
            "version": __version__,
            "pymodbus": pymodbus_version(),
            "python": platform.python_version(),
            "simulator": ctx.simulator_info,
            "default_bus": {**default, "describe": TransportConfig.from_dict(
                {k: v for k, v in default.items() if k not in ("name", "locked")}).describe()},
            "features": {
                "mqtt": mqtt.available(),
                "history": ctx.history is not None,
                "write": bool(opts.get("allow_write")),
                "auth": bool(opts.get("auth")),
            },
        })

    @app.get("/api/config")
    def api_config():
        """Zgodność wstecz: tryb pracy (serial / tcp)."""
        default = ctx.config.get()["buses"]["default"]
        if default["kind"] in ("rtu", "ascii"):
            return jsonify({"mode": "serial", "serial": {
                "port": default["serial_port"], "baudrate": default["baudrate"],
                "parity": default["parity"], "stopbits": default["stopbits"],
                "bytesize": default["bytesize"]}})
        return jsonify({"mode": "tcp", "host": default.get("host"), "port": default.get("port")})

    @app.get("/api/health")
    def api_health():
        devices = {}
        for dev_id in ctx.poller.device_ids():
            data = ctx.poller.values(dev_id)
            if data:
                devices[dev_id] = data["status"]["state"]
        ok = all(s in ("ok", "disabled", "no_preset") for s in devices.values())
        return jsonify({"ok": ok, "devices": devices}), (200 if ok else 503)

    @app.get("/metrics")
    def api_metrics():
        return Response(metrics.render(ctx.poller), mimetype="text/plain; version=0.0.4; charset=utf-8")

    # ── presety ────────────────────────────────────────────────
    @app.get("/api/presets")
    def api_list_presets():
        return jsonify(ctx.presets.list())

    @app.get("/api/presets/<preset_id>")
    def api_get_preset(preset_id):
        if request.args.get("raw") in ("1", "true"):
            # dokładna treść pliku (przeglądarka gubi precyzję liczb 64-bit w JSON.parse)
            text = ctx.presets.get_text(preset_id) if valid_id(preset_id) else None
            if text is None:
                raise ApiError("preset nie znaleziony", 404)
            return Response(text, mimetype="application/json; charset=utf-8")
        p = ctx.presets.get_raw(preset_id) if valid_id(preset_id) else None
        if p is None:
            raise ApiError("preset nie znaleziony", 404)
        return jsonify(p)

    @app.post("/api/presets")
    def api_create_preset():
        data = _body()
        source = data.get("copy_from")
        if source is not None:
            # kopia po stronie serwera - liczby (np. uint64) bez utraty precyzji
            raw = ctx.presets.get_raw(source) if isinstance(source, str) and valid_id(source) else None
            if raw is None:
                raise ApiError("preset źródłowy nie znaleziony", 404)
            name = data.get("_save_as") or f"{raw.get('name') or source} (kopia)"
            data = {k: v for k, v in raw.items() if not str(k).startswith("_")}
            data["name"] = name
        name = data.get("_save_as") or data.get("name") or "preset"
        preset_id = ctx.presets.unique_id(name)
        ctx.presets.save(preset_id, data)
        return jsonify({"ok": True, "id": preset_id, "filename": preset_id}), 201

    @app.put("/api/presets/<preset_id>")
    def api_update_preset(preset_id):
        if not valid_id(preset_id):
            raise ApiError("nieprawidłowy identyfikator presetu")
        if ctx.presets.is_builtin(preset_id):
            raise ApiError("presetu wbudowanego nie można zmienić - zapisz kopię", 403)
        ctx.presets.save(preset_id, _body())
        ctx.poller.reload()
        return jsonify({"ok": True, "id": preset_id})

    @app.delete("/api/presets/<preset_id>")
    def api_delete_preset(preset_id):
        if not valid_id(preset_id):
            raise ApiError("nieprawidłowy identyfikator presetu")
        if ctx.presets.is_builtin(preset_id):
            raise ApiError("presetu wbudowanego nie można usunąć", 403)
        if not ctx.presets.delete(preset_id):
            raise ApiError("preset nie znaleziony", 404)
        ctx.poller.reload()
        return jsonify({"ok": True})

    @app.post("/api/presets/validate")
    def api_validate_preset():
        from .presets import normalize_preset
        try:
            norm = normalize_preset({k: v for k, v in _body().items() if not str(k).startswith("_")})
        except PresetError as e:
            return jsonify({"ok": False, "errors": e.errors})
        return jsonify({"ok": True, "errors": [], "register_count": len(norm["registers"])})

    # ── magistrale ─────────────────────────────────────────────
    def bus_entry(bus_id, b):
        cfg = {k: v for k, v in b.items() if k not in ("name", "locked")}
        tc = TransportConfig.from_dict(cfg)
        stats = None
        for snap in ctx.buses.snapshot():
            if snap["key"] == tc.key():
                stats = snap["stats"]
        return {"id": bus_id, **b, "locked": bool(b.get("locked")), "describe": tc.describe(),
                "stats": stats}

    @app.get("/api/buses")
    def api_buses():
        return jsonify([bus_entry(k, v) for k, v in ctx.config.get()["buses"].items()])

    def _create_guard(kind, exists):
        """?create=1 - tworzenie bez nadpisywania istniejącego elementu."""
        if request.args.get("create") in ("1", "true") and exists:
            raise ApiError(f"{kind} o takim identyfikatorze już istnieje", 409)

    @app.put("/api/buses/<bus_id>")
    def api_put_bus(bus_id):
        _create_guard("magistrala", bus_id in ctx.config.get()["buses"])
        ctx.config.put_bus(bus_id, _body())
        return jsonify(bus_entry(bus_id, ctx.config.get()["buses"][bus_id]))

    @app.delete("/api/buses/<bus_id>")
    def api_delete_bus(bus_id):
        if not ctx.config.delete_bus(bus_id):
            raise ApiError("magistrala nie znaleziona", 404)
        return jsonify({"ok": True})

    @app.post("/api/buses/<bus_id>/ping")
    def api_bus_ping(bus_id):
        data = _opt_body()
        res = bus_by_id(bus_id).ping(data.get("unit"))
        return jsonify(res), (200 if res.get("ok") else 502)

    @app.post("/api/buses/test")
    def api_bus_test():
        """Test parametrów magistrali przed zapisaniem."""
        data = _body()
        try:
            tc = TransportConfig.from_dict({k: v for k, v in data.items() if k not in ("name", "unit", "locked", "id")})
        except (TypeError, ValueError) as e:
            raise ApiError(str(e)) from None
        res = ctx.buses.get(tc).ping(data.get("unit"))
        return jsonify(res), (200 if res.get("ok") else 502)

    @app.get("/api/serial-ports")
    def api_serial_ports():
        return jsonify(list_serial_ports())

    @app.post("/api/ping")
    def api_ping():
        """Zgodność wstecz: test połączenia (host/port albo domyślna magistrala)."""
        data = _opt_body()
        res = bus_from_request(data).ping(data.get("unit"))
        return jsonify(res), (200 if res.get("ok") else 502)

    # ── urządzenia ─────────────────────────────────────────────
    def device_entry(dev_id, d):
        data = ctx.poller.values(dev_id)
        preset = (data or {}).get("preset")
        return {"id": dev_id, **d, "status": data["status"] if data else None,
                "preset_name": preset["name"] if preset else None}

    @app.get("/api/devices")
    def api_devices():
        return jsonify([device_entry(k, v) for k, v in ctx.config.get()["devices"].items()])

    @app.put("/api/devices/<dev_id>")
    def api_put_device(dev_id):
        _create_guard("urządzenie", dev_id in ctx.config.get()["devices"])
        dev = ctx.config.put_device(dev_id, _body())
        return jsonify(device_entry(dev_id, dev))

    @app.delete("/api/devices/<dev_id>")
    def api_delete_device(dev_id):
        if not ctx.config.delete_device(dev_id):
            raise ApiError("urządzenie nie znalezione", 404)
        if ctx.history:
            ctx.history.delete_device(dev_id)
        return jsonify({"ok": True})

    def runtime_or_404(dev_id):
        rt = ctx.poller.runtime(dev_id)
        if rt is None:
            raise ApiError("urządzenie nie znalezione", 404)
        return rt

    @app.get("/api/devices/<dev_id>/values")
    def api_device_values(dev_id):
        runtime_or_404(dev_id)
        return jsonify(ctx.poller.values(dev_id))

    @app.post("/api/devices/<dev_id>/read")
    def api_device_read(dev_id):
        runtime_or_404(dev_id)
        ctx.poller.read_now(dev_id)
        return jsonify(ctx.poller.values(dev_id))

    def history_data(dev_id):
        runtime_or_404(dev_id)
        seconds = _int(request.args, "seconds", 600, 10, 3650 * 86400)
        keys = [k for k in request.args.get("keys", "").split(",") if k] or None
        source = request.args.get("source", "auto")
        max_points = _int(request.args, "max_points", 1000, 10, 20000)
        mem = ctx.poller.history(dev_id, seconds, keys)
        mem["source"] = "memory"
        if ctx.history is None or source == "memory":
            return mem
        now = time.time()
        since = now - seconds
        oldest = mem["points"][0][0] if mem["points"] else None
        if source == "auto" and oldest is not None and oldest <= since + 5:
            return mem  # bufor w pamięci obejmuje całe okno - pełna rozdzielczość
        want = keys or mem["keys"]
        if source == "auto" and oldest is not None and seconds <= 6 * 3600:
            # starsza część okna z SQLite (agregaty), świeża z pamięci (pełna rozdzielczość)
            older = ctx.history.query(dev_id, want, since, oldest - 1, max_points=max_points)
            index = {k: i for i, k in enumerate(older["keys"])}
            pts = [[p[0]] + [p[1 + index[k]] if k in index else None for k in mem["keys"]]
                   for p in older["points"] if p[0] < oldest]
            mem["points"] = pts + mem["points"]
            mem["source"] = "mixed" if pts else "memory"
            return mem
        res = ctx.history.query(dev_id, want, since, max_points=max_points)
        if not res["points"] and mem["points"]:
            return mem
        res["source"] = "db"
        return res

    @app.get("/api/devices/<dev_id>/history")
    def api_device_history(dev_id):
        return jsonify(history_data(dev_id))

    @app.get("/api/devices/<dev_id>/history.csv")
    def api_device_history_csv(dev_id):
        data = history_data(dev_id)
        meta = (ctx.poller.values(dev_id) or {}).get("meta", {})
        buf = io.StringIO()
        w = csv.writer(buf, delimiter=";")
        w.writerow(["czas"] + [_csv_safe(f"{meta.get(k, {}).get('label', k)} [{meta.get(k, {}).get('unit', '')}]")
                               for k in data["keys"]])
        for row in data["points"]:
            w.writerow([datetime.fromtimestamp(row[0]).isoformat(sep=" ", timespec="seconds")] +
                       ["" if v is None else str(v).replace(".", ",") for v in row[1:]])
        name = f"{dev_id}_{datetime.now().strftime('%Y-%m-%d_%H%M%S')}.csv"
        return Response("﻿" + buf.getvalue(), mimetype="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    # ── odczyt wg presetu (zgodność wstecz) ────────────────────
    reader_cache = {}

    @app.post("/api/live")
    def api_live():
        data = _body()
        preset_id = data.get("preset")
        if not isinstance(preset_id, str) or not valid_id(preset_id):
            raise ApiError("preset nie znaleziony", 404)
        preset = ctx.presets.get(preset_id)
        if not preset:
            raise ApiError("preset nie znaleziony", 404)
        if not preset["registers"]:
            raise ApiError("preset pusty")
        bus = bus_from_request(data)
        unit = unit_of(data)
        key = (bus.cfg.key(), unit, preset_id)
        cached = reader_cache.get(key)
        if cached is None or cached.preset != preset:
            cached = reader_cache[key] = PresetReader(preset)
        res = cached.read(bus, unit)
        if not res["ok"]:
            first = next(iter(res["errors"].values()), "brak danych")
            raise ApiError(f"błąd odczytu: {first}", 502, errors=res["errors"])
        values = {}
        for k, spec in preset["registers"].items():
            values[k] = {"value": res["values"][k], "unit": spec["unit"], "label": spec["label"],
                         "group": spec["group"], "decimals": spec["decimals"]}
            if k in res["errors"]:
                values[k]["error"] = res["errors"][k]
        return jsonify({"values": values, "timestamp": time.time(), "duration_ms": res["duration_ms"]})

    # ── skaner ─────────────────────────────────────────────────
    @app.post("/api/scan")
    def api_scan():
        data = _body()
        function = str(data.get("register_type", "input"))
        if function not in SCAN_FUNCTIONS:
            raise ApiError(f"'register_type' musi być jednym z: {', '.join(SCAN_FUNCTIONS)}")
        start = _int(data, "start", 0, 0, 65535)
        end = _int(data, "end", start + 100, 1, 65536)
        if end <= start:
            raise ApiError("'end' musi być większy niż 'start'")
        if end - start > MAX_SCAN_SPAN:
            raise ApiError(f"maksymalny zakres skanu to {MAX_SCAN_SPAN} adresów")
        step = _int(data, "step", 2, 1, 2)
        bus = bus_from_request(data)
        res = scanner.scan(bus, unit_of(data), function, start, end, step)
        if res["readable"] == 0:
            raise ApiError(res["error"] or "urządzenie nie zwróciło żadnych danych z tego zakresu", 502,
                           unreadable=res["unreadable"], exception_code=res["exception_code"])
        return jsonify(res)

    @app.post("/api/scan/preset")
    def api_scan_preset():
        """Propozycja presetu z wierszy skanu (do edycji i zapisania przez użytkownika)."""
        from . import heuristics
        data = _body()
        rows = data.get("registers")
        if not isinstance(rows, list):
            raise ApiError("'registers' musi być listą wierszy skanu")
        for field in ("byte_order", "register_type", "name", "alignment"):
            if data.get(field) is not None and not isinstance(data[field], str):
                raise ApiError(f"'{field}' musi być tekstem")
        preset = heuristics.suggest_preset(rows, byte_order=data.get("byte_order"),
                                           register_type=data.get("register_type") or "input",
                                           name=data.get("name"), alignment=data.get("alignment"))
        return jsonify(preset)

    @app.post("/api/scan/units")
    def api_scan_units():
        data = _body()
        bus = bus_from_request(data)
        first = _int(data, "first", 1, 0, 255)
        last = _int(data, "last", 247, first, 255)
        function = str(data.get("register_type", "input"))
        if function not in ("input", "holding"):
            raise ApiError("'register_type' musi być input lub holding")
        address = _int(data, "address", 0, 0, 65535)
        count = _int(data, "count", 1, 1, 125)
        timeout = _float(data, "timeout", 0.3, 0.05, 5)
        try:
            job = ctx.jobs.start("units", scanner.scan_units, bus, first, last, function, address, count, timeout)
        except RuntimeError as e:
            raise ApiError(str(e), 409) from None
        return jsonify(job.to_dict()), 202

    @app.post("/api/detect")
    def api_detect():
        data = _body()
        bus = bus_from_request(data)
        timeout = _float(data, "timeout", 0.5, 0.05, 5)
        try:
            job = ctx.jobs.start("detect", scanner.detect_preset, bus, unit_of(data), ctx.presets, timeout)
        except RuntimeError as e:
            raise ApiError(str(e), 409) from None
        return jsonify(job.to_dict()), 202

    @app.get("/api/jobs/<job_id>")
    def api_job(job_id):
        job = ctx.jobs.get(job_id)
        if job is None:
            raise ApiError("zadanie nie znalezione", 404)
        return jsonify(job.to_dict())

    @app.delete("/api/jobs/<job_id>")
    def api_job_cancel(job_id):
        return jsonify({"ok": ctx.jobs.cancel(job_id)})

    @app.post("/api/write")
    def api_write():
        """Zapis rejestru/cewki - tylko gdy uruchomiono z --allow-write."""
        if not opts.get("allow_write"):
            raise ApiError("zapis wyłączony - uruchom z flagą --allow-write", 403)
        data = _body()
        bus = bus_from_request(data)
        unit = unit_of(data)
        address = _int(data, "address", None, 0, 65535)
        function = data.get("function", "holding")
        if function == "coil":
            bus.write_coil(unit, address, bool(data.get("value")))
        elif function == "holding":
            values = data.get("values")
            if values is None:
                values = [_int(data, "value", None, 0, 65535)]
            if (not isinstance(values, list) or not 1 <= len(values) <= 123 or
                    any(isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 65535 for v in values)):
                raise ApiError("'values' musi być listą 1-123 liczb 0-65535")
            if len(values) == 1:
                bus.write_register(unit, address, values[0])
            else:
                bus.write_registers(unit, address, values)
        else:
            raise ApiError("'function' musi być holding lub coil")
        log.warning("Zapis Modbus: unit=%s %s @%s = %s", unit, function, address,
                    data.get("values", data.get("value")))
        return jsonify({"ok": True})

    # ── integracje ─────────────────────────────────────────────
    @app.get("/api/settings/mqtt")
    def api_get_mqtt():
        m = dict(ctx.config.get()["mqtt"])
        m["password"] = "********" if m.get("password") else ""
        return jsonify({"settings": m, "status": ctx.mqtt.status()})

    @app.put("/api/settings/mqtt")
    def api_put_mqtt():
        data = _body()
        with ctx.config.write_lock:  # sprawdzenie i zapis razem - równoległa zmiana hosta nie przejdzie
            return _put_mqtt(data)

    def _put_mqtt(data):
        stored = ctx.config.get()["mqtt"]
        if data.get("password", "********") == "********":
            # zapisane hasło (zamaskowane w GET) zostaje tylko dla tego samego brokera i konta -
            # inaczej każdy z dostępem do API mógłby je przechwycić, wskazując własny serwer
            try:
                new = validate_mqtt({**stored, **{k: v for k, v in data.items() if k != "password"}}, strict=True)
            except ConfigError as e:
                raise ApiError(str(e)) from None
            moved = any(new[k] != stored[k] for k in ("host", "port", "username", "tls"))
            if moved and stored["password"]:
                raise ApiError("zmieniono brokera, konto albo TLS - wpisz hasło MQTT ponownie", 400,
                               field="password")
            data["password"] = stored["password"]
        ctx.config.put_section("mqtt", data)
        return api_get_mqtt()

    @app.get("/api/settings/history")
    def api_get_history():
        return jsonify({"settings": ctx.config.get()["history"],
                        "available": not ctx.options.get("no_history"),
                        "active": ctx.history is not None})

    @app.put("/api/settings/history")
    def api_put_history():
        ctx.config.put_section("history", _body())
        return api_get_history()

    return app
