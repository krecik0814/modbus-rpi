#!/usr/bin/env python3
"""Modbus Dash - webowy monitoring liczników energii (Modbus RTU / TCP).

Uruchomienie:
    python app.py                                   # symulator + dashboard na :5000
    python app.py --serial /dev/serial0 --baudrate 9600 --parity E   # RPi 3/4: wymaga dtoverlay=disable-bt
    python app.py --tcp 192.168.1.50:502            # licznik / bramka Modbus TCP
    python app.py --help                            # wszystkie opcje
"""

import argparse
import contextlib
import logging
import os
import signal
import sys
from pathlib import Path

BASE_DIR = Path(__file__).parent.resolve()

log = logging.getLogger("modbus-dash")


class _PolishDefaults(argparse.HelpFormatter):
    """Dopisuje "(domyślnie: ...)" po polsku; pomija puste wartości i --auth (hasło z env)."""

    def _get_help_string(self, action):
        text = action.help or ""
        default = action.default
        if (action.dest == "auth" or default in (None, False, [], argparse.SUPPRESS)
                or "%(default)" in text or not action.option_strings):
            return text
        return f"{text} (domyślnie: %(default)s)"


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Modbus Dash - monitoring liczników energii (Modbus RTU/TCP)",
        formatter_class=_PolishDefaults)
    g = p.add_argument_group("serwer WWW")
    g.add_argument("--host", default="0.0.0.0", help="adres nasłuchu HTTP (127.0.0.1 = tylko lokalnie)")
    g.add_argument("--port", type=int, default=5000, help="port HTTP dashboardu")
    g.add_argument("--auth", default=os.environ.get("MODBUS_DASH_AUTH"), metavar="USER:HASŁO",
                   help="włącz logowanie HTTP Basic (lub zmienna MODBUS_DASH_AUTH)")
    g.add_argument("--allow-write", action="store_true", help="zezwól na zapis rejestrów/cewek z interfejsu")
    g.add_argument("--allowed-host", action="append", metavar="NAZWA",
                   default=[h.strip() for h in os.environ.get("MODBUS_DASH_ALLOWED_HOSTS", "").split(",") if h.strip()],
                   help="dodatkowa nazwa hosta, pod którą otwierasz dashboard bez --auth (np. energia.lan, "
                        "*.home.lan; można powtarzać; zmienna MODBUS_DASH_ALLOWED_HOSTS) - adresy IP, localhost "
                        "i nazwa tego komputera są zawsze dozwolone")
    g.add_argument("--data-dir", default=str(BASE_DIR / "data"), help="katalog na konfigurację i historię")
    g.add_argument("--presets-dir", default=str(BASE_DIR / "presets"), help="katalog presetów użytkownika")
    g.add_argument("--no-history", action="store_true", help="nie zapisuj historii w SQLite")
    g.add_argument("--debug", action="store_true", help="tryb debug Flask (tylko lokalnie!)")
    g.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                   help="poziom komunikatów w logu")

    g = p.add_argument_group("połączenie Modbus ('default')")
    g.add_argument("--serial", metavar="PORT", help="port RS-485, np. /dev/serial0, /dev/ttyUSB0, COM3")
    g.add_argument("--baudrate", type=int, default=9600, help="prędkość portu szeregowego [bit/s]")
    g.add_argument("--parity", default="N", choices=["N", "E", "O"], help="parzystość: N (brak), E (parzysta), O")
    g.add_argument("--stopbits", type=int, default=1, choices=[1, 2], help="bity stopu")
    g.add_argument("--bytesize", type=int, default=8, choices=[7, 8], help="bity danych")
    g.add_argument("--framer", default="rtu", choices=["rtu", "ascii"], help="ramkowanie na porcie szeregowym")
    g.add_argument("--local-echo", action="store_true",
                   help="adapter RS-485 odsyła własną transmisję (lokalne echo) - odrzucaj ją")
    g.add_argument("--tcp", metavar="HOST[:PORT]", help="urządzenie / bramka Modbus TCP")
    g.add_argument("--rtu-over-tcp", metavar="HOST[:PORT]",
                   help="bramka w trybie transparentnym (ramki RTU po TCP, np. USR-TCP232, Elfin EW11)")
    g.add_argument("--timeout", type=float, default=1.0, help="timeout odpowiedzi [s]")
    g.add_argument("--retries", type=int, default=1, help="liczba ponowień przy braku odpowiedzi")
    g.add_argument("--delay-ms", type=int, default=0, help="dodatkowa przerwa między ramkami [ms]")
    g.add_argument("--preset", help="od razu odczytuj urządzenie wg presetu (id), np. eastron_sdm630")
    g.add_argument("--unit", type=int, default=1, help="Unit ID urządzenia dla --preset")
    g.add_argument("--interval", type=float, default=1.0, help="interwał odczytu dla --preset [s]")

    g = p.add_argument_group("symulator")
    g.add_argument("--no-sim", action="store_true", help="nie uruchamiaj symulatora")
    g.add_argument("--sim", action="store_true",
                   help="uruchom symulator także przy --serial/--tcp/--rtu-over-tcp (domyślnie wtedy wyłączony)")
    g.add_argument("--modbus-port", type=int, default=5020, help="port TCP symulatora")
    g.add_argument("--sim-preset", action="append", default=[], metavar="PRESET[:UNIT]",
                   help="dodatkowe urządzenie w symulatorze (można powtarzać), np. eastron_sdm120:2")
    g.add_argument("--sim-framing", default="tcp", choices=["tcp", "rtu"],
                   help="ramkowanie symulatora: Modbus TCP albo RTU-over-TCP")
    g.add_argument("--sim-strict", action="store_true",
                   help="symulator zwraca wyjątek 02 dla niezmapowanych adresów (jak wiele liczników)")
    args = p.parse_args(argv)
    if args.auth and ":" not in args.auth:
        p.error("--auth wymaga formatu USER:HASŁO")
    if sum(bool(x) for x in (args.serial, args.tcp, args.rtu_over_tcp)) > 1:
        p.error("podaj tylko jedno z: --serial, --tcp, --rtu-over-tcp")
    if (args.serial or args.tcp or args.rtu_over_tcp) and not args.sim:
        args.no_sim = True  # prawdziwe urządzenie = bez symulatora (chyba że --sim)
    for opt in ("tcp", "rtu_over_tcp"):
        if getattr(args, opt):
            try:
                _host_port(getattr(args, opt))
            except ValueError as e:
                p.error(f"--{opt.replace('_', '-')}: {e}")
    for spec in args.sim_preset:
        try:
            _sim_spec(spec)
        except ValueError as e:
            p.error(f"--sim-preset: {e}")
    return args


def _host_port(value, default_port=502):
    """"host", "host:port", "[IPv6]" albo "[IPv6]:port" -> (host, port). Rzuca ValueError."""
    value = value.strip()
    if value.startswith("[") and "]" in value:
        host, _, rest = value[1:].partition("]")
        if rest and not rest.startswith(":"):
            raise ValueError(f"niepoprawny adres '{value}' (użyj [IPv6]:PORT)")
        port = rest[1:]
    elif value.count(":") == 1:
        host, _, port = value.partition(":")
    else:
        host, port = value, ""
    if not host:
        raise ValueError(f"brak hosta w '{value}'")
    if not port:
        return host, default_port
    if not port.isdigit() or not 1 <= int(port) <= 65535:
        raise ValueError(f"niepoprawny port w '{value}' (1-65535)")
    return host, int(port)


def _sim_spec(spec):
    """"preset[:unit]" -> (preset, unit). Rzuca ValueError."""
    pid, _, unit = spec.partition(":")
    if not pid:
        raise ValueError(f"brak id presetu w '{spec}'")
    if not unit:
        return pid, 2
    if not unit.isdigit() or not 1 <= int(unit) <= 247:
        raise ValueError(f"niepoprawny Unit ID w '{spec}' (1-247)")
    return pid, int(unit)


def build_overrides(args):
    """Magistrale/urządzenia z flag CLI (tylko w pamięci, nie zapisywane do config.json)."""
    common = {"timeout": args.timeout, "retries": args.retries, "delay_ms": args.delay_ms}
    buses, devices = {}, {}
    if args.serial:
        buses["default"] = {"name": "RS-485 (CLI)", "kind": args.framer, "serial_port": args.serial,
                            "baudrate": args.baudrate, "parity": args.parity, "stopbits": args.stopbits,
                            "bytesize": args.bytesize, "local_echo": args.local_echo, **common}
    elif args.tcp or args.rtu_over_tcp:
        host, port = _host_port(args.tcp or args.rtu_over_tcp)
        buses["default"] = {"name": "TCP (CLI)", "kind": "tcp" if args.tcp else "rtu_over_tcp",
                            "host": host, "port": port, **common}
    if not args.no_sim:
        buses["sim"] = {"name": "Symulator", "kind": "tcp" if args.sim_framing == "tcp" else "rtu_over_tcp",
                        "host": "127.0.0.1", "port": args.modbus_port, "timeout": 1.0, "retries": 0}
        devices["symulator"] = {"name": "Symulator 3F", "bus": "sim", "unit": 1,
                                "preset": "simulator_3f", "interval": 1.0}
        for spec in args.sim_preset:
            pid, unit = _sim_spec(spec)
            devices[f"sym-{unit}"] = {"name": f"Symulator {pid} #{unit}", "bus": "sim", "unit": unit,
                                      "preset": pid, "interval": 1.0}
    if args.preset:
        devices["cli"] = {"name": f"Licznik ({args.preset})", "bus": "default", "unit": args.unit,
                          "preset": args.preset, "interval": args.interval}
    return {"buses": buses, "devices": devices}


class AppContext:
    """Wszystkie usługi aplikacji w jednym miejscu (przekazywane do web.create_app)."""

    def __init__(self, args):
        from modbus_dash.config import ConfigStore
        from modbus_dash.mqtt import MqttPublisher
        from modbus_dash.planner import PresetReader
        from modbus_dash.poller import Poller
        from modbus_dash.presets import PresetStore
        from modbus_dash.scanner import JobManager
        from modbus_dash.transport import BusManager, TransportConfig

        self.base_dir = BASE_DIR
        self.data_dir = Path(args.data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.options = {
            "auth": tuple(args.auth.split(":", 1)) if args.auth else None,
            "allow_write": args.allow_write,
            "allowed_hosts": list(args.allowed_host),
            "no_history": args.no_history,
        }
        self.presets = PresetStore(Path(args.presets_dir), BASE_DIR / "presets" / "library")
        defaults = {"buses": {"default": {"name": "Domyślna", "kind": "tcp", "host": "127.0.0.1", "port": 502}}}
        self.config = ConfigStore(self.data_dir / "config.json", defaults, build_overrides(args))
        self.buses = BusManager()
        self._bus_keys = self._bus_keys_now()   # łącza z konfiguracji (zwalniane po usunięciu/zmianie)
        self.poller = Poller(self.config, self.presets, self.buses, PresetReader, TransportConfig)
        self.jobs = JobManager()
        self.history = None
        self._apply_history()
        self.mqtt = MqttPublisher(self.config, self.poller)
        self.poller.add_listener(self.mqtt.on_sample)
        self.poller.add_preset_listener(self.mqtt.on_preset_change)  # discovery HA po edycji presetu
        self.config.on_change(self._on_config_change)
        self.simulator = None
        self.simulator_info = None
        if not args.no_sim:
            self._start_simulator(args)

    def _apply_history(self):
        """Włącza/wyłącza zapis historii w SQLite zgodnie z konfiguracją (bez restartu)."""
        from modbus_dash.history import HistoryDB
        h = self.config.get()["history"]
        want = h["enabled"] and not self.options["no_history"]
        if want and self.history is None:
            self.history = HistoryDB(self.data_dir / "history.sqlite", h["bucket_seconds"], h["retention_days"])
            self.poller.add_listener(self.history.on_sample)
        elif not want and self.history is not None:
            db, self.history = self.history, None
            self.poller.remove_listener(db.on_sample)
            db.close()
        elif self.history is not None:
            self.history.configure(h["bucket_seconds"], h["retention_days"])

    def _on_config_change(self, section):
        if section == "history":
            self._apply_history()
        if section in ("buses", "devices", "history"):
            self.poller.reload()
        if section == "buses":
            self._retain_buses()
        if section in ("mqtt", "devices"):
            self.mqtt.apply()

    def _bus_keys_now(self):
        from modbus_dash.transport import TransportConfig
        keys = set()
        for cfg in self.config.get()["buses"].values():
            try:
                keys.add(TransportConfig.from_dict({k: v for k, v in cfg.items() if k not in ("name", "locked")}).key())
            except ValueError:
                continue
        return keys

    def _retain_buses(self):
        """Usunięta albo zmieniona magistrala od razu zwalnia port (nie po 5 min bezczynności)."""
        keys = self._bus_keys_now()
        removed, self._bus_keys = self._bus_keys - keys, keys
        self.buses.retain(keys, removed=removed)

    def _start_simulator(self, args):
        from modbus_dash.presets import PresetError
        from modbus_dash.simulator import Simulator
        sim = Simulator(host="0.0.0.0", port=args.modbus_port, framing=args.sim_framing)
        units = [("simulator_3f", 1)] + [_sim_spec(spec) for spec in args.sim_preset]
        loaded = []
        for pid, unit in units:
            try:
                preset = self.presets.get(pid)
            except PresetError as e:
                log.error("Symulator: preset '%s' niepoprawny: %s", pid, e)
                continue
            if not preset:
                log.error("Symulator: brak presetu '%s'", pid)
                continue
            sim.add_preset(unit, preset, strict=args.sim_strict)
            loaded.append({"unit": unit, "preset": pid})
        try:
            sim.start()
        except OSError as e:
            log.error("Symulator: nie można nasłuchiwać na porcie %s: %s (użyj --modbus-port albo --no-sim)",
                      args.modbus_port, e)
            sim.stop()
            return
        self.simulator = sim
        self.simulator_info = {"port": args.modbus_port, "framing": args.sim_framing, "devices": loaded}
        log.info("Symulator Modbus %s na porcie %s (urządzenia: %s)", args.sim_framing.upper(), args.modbus_port,
                 ", ".join(f"{d['preset']}@{d['unit']}" for d in loaded))

    def start(self):
        self.poller.start()
        self.mqtt.apply()

    def shutdown(self):
        log.info("Zatrzymywanie...")
        for step in (self.poller.stop, self.mqtt.stop,
                     getattr(self.history, "close", None), self.buses.close_all,
                     getattr(self.simulator, "stop", None)):
            if step is None:
                continue
            try:
                step()
            except Exception:  # noqa: BLE001
                log.exception("Błąd przy zamykaniu")


def main(argv=None):
    args = parse_args(argv)
    logging.basicConfig(level=args.log_level, format="%(asctime)s [%(name)s] %(levelname)s %(message)s")
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    if args.debug and args.host not in ("127.0.0.1", "localhost", "::1"):
        log.warning("--debug włącza debugger Werkzeug (wykonanie kodu!) - nasłuch ograniczony do 127.0.0.1")
        args.host = "127.0.0.1"

    from modbus_dash.config import ConfigError
    from modbus_dash.web import create_app
    try:
        ctx = AppContext(args)
    except (ConfigError, ValueError) as e:
        log.error("Błąd konfiguracji: %s", e)
        return 2
    app = create_app(ctx)
    ctx.start()

    def _terminate(signum, frame):
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, _terminate)

    shown = "localhost" if args.host in ("0.0.0.0", "::") else args.host
    log.info("Dashboard: http://%s:%s", shown, args.port)
    if not args.auth and args.host not in ("127.0.0.1", "localhost", "::1"):
        log.info("Wskazówka: dashboard jest dostępny w sieci bez hasła - użyj --auth USER:HASŁO lub --host 127.0.0.1")
    try:
        serve = None
        if not args.debug:  # debugger Werkzeug działa tylko z serwerem Flask
            with contextlib.suppress(ImportError):
                from waitress import serve
        if serve is None:
            app.run(host=args.host, port=args.port, debug=args.debug, use_reloader=False, threaded=True)
        else:
            # limit rozmiaru zapytania już w waitress: bez niego buforuje do 1 GB na dysku,
            # zanim Flask sprawdzi logowanie i MAX_CONTENT_LENGTH; treść do limitu trzymamy w RAM
            limit = app.config["MAX_CONTENT_LENGTH"]
            serve(app, host=args.host, port=args.port, threads=8, ident="modbus-dash",
                  max_request_body_size=limit, inbuf_overflow=limit + 65536, connection_limit=20)
    except KeyboardInterrupt:
        pass
    finally:
        ctx.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
