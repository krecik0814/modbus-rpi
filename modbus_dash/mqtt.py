"""Publikacja odczytów do MQTT + automatyczne wykrywanie w Home Assistant.

Wymaga opcjonalnej biblioteki paho-mqtt (v1 lub v2): pip install paho-mqtt

Topiki (prefiks domyślnie "modbus-dash"):
    <prefiks>/status                 online/offline (retained, LWT)
    <prefiks>/<urządzenie>/state     JSON {klucz: wartość, ..., "ts": unix}  (<urządzenie> = id urządzenia)
    <prefiks>/<urządzenie>/availability  online/offline
    homeassistant/sensor/<prefiks>/<urządzenie>_<klucz>/config   discovery
"""

import json
import logging
import ssl
import threading
import time

from . import __version__
from .quantities import ha_classes, normalize_unit, safe_key

log = logging.getLogger("modbus-dash.mqtt")

try:
    import paho.mqtt.client as _paho
except ImportError:  # pragma: no cover - zależność opcjonalna
    _paho = None


def available():
    return _paho is not None


class MqttPublisher:
    def __init__(self, config_store, poller):
        self.config = config_store
        self.poller = poller
        self._lock = threading.RLock()
        self._switch_lock = threading.Lock()  # kolejne przełączenia klienta po kolei
        self._client = None
        self._cfg = None
        self._gen = 0                # numer konfiguracji (przełączenie w tle sprawdza aktualność)
        self._pending = False        # przełączenie klienta w toku
        self._connected = False
        self._last_error = None
        self._last_pub = {}          # device -> monotonic
        self._discovered = {}        # device -> set(topic)
        self._dev_online = {}        # device -> bool
        self._published = 0

    # ── konfiguracja ───────────────────────────────────────────
    def apply(self, *_):
        """Stosuje konfigurację MQTT. Zmiana ustawień przełącza klienta w tle (stary klient
        może czekać na niedostępnego brokera - nie blokujemy zapisu ustawień ani odpytywania)."""
        cfg = self.config.get()["mqtt"]
        with self._lock:
            if cfg == self._cfg and (self._client or self._pending or not cfg["enabled"]):
                if self._connected:
                    self._sync_availability()
                    self._publish_discovery_all()
                return
            old_cfg, old_devices = self._cfg, set(self._discovered) | set(self._dev_online)
            orphans = set()
            if old_cfg and self._orphaned(old_cfg, cfg):
                orphans = set().union(*self._discovered.values()) if self._discovered else set()
            old = self._detach_client()
            self._cfg = cfg
            self._gen += 1
            gen = self._gen
            self._last_error = None
            if cfg["enabled"] and _paho is None:
                self._last_error = "brak biblioteki paho-mqtt (pip install paho-mqtt)"
                log.warning("MQTT: %s", self._last_error)
            start = cfg["enabled"] and _paho is not None
            if not (old or start):
                return
            self._pending = start
        prefix_changed = bool(old_cfg) and old_cfg["topic_prefix"] != cfg["topic_prefix"]
        cleanup = (orphans, old_devices if prefix_changed else ())
        threading.Thread(target=self._switch, args=(old, old_cfg, cleanup, gen), daemon=True,
                         name="mqtt-switch").start()

    @staticmethod
    def _orphaned(old_cfg, cfg):
        """Czy encje discovery starej konfiguracji przestają być aktualne (trzeba je usunąć)."""
        return old_cfg["ha_discovery"] and (
            not cfg["ha_discovery"] or old_cfg["topic_prefix"] != cfg["topic_prefix"]
            or old_cfg["ha_prefix"] != cfg["ha_prefix"])

    def _switch(self, old, old_cfg, cleanup, gen):
        with self._switch_lock:
            self._shutdown(old, old_cfg, cleanup)
            with self._lock:
                if gen != self._gen:
                    return  # w międzyczasie przyszła nowsza konfiguracja
                self._pending = False
                if self._client is None and self._cfg["enabled"] and _paho is not None:
                    self._start_client(self._cfg)

    def _start_client(self, cfg):
        client_id = f"modbus-dash-{safe_key(cfg['topic_prefix'])}"
        if hasattr(_paho, "CallbackAPIVersion"):
            client = _paho.Client(_paho.CallbackAPIVersion.VERSION2, client_id=client_id)
        else:
            client = _paho.Client(client_id=client_id)
        if cfg["username"]:
            client.username_pw_set(cfg["username"], cfg["password"] or None)
        if cfg["tls"]:
            client.tls_set(cert_reqs=ssl.CERT_REQUIRED)
        client.will_set(self._t("status"), "offline", qos=1, retain=True)
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        client.on_connect_fail = self._on_connect_fail
        client.reconnect_delay_set(min_delay=1, max_delay=60)
        self._client = client
        self._last_error = None
        try:
            client.connect_async(cfg["host"], int(cfg["port"]), keepalive=60)
            client.loop_start()
        except (OSError, ValueError) as e:
            self._last_error = f"połączenie: {e}"
            log.warning("MQTT: %s", self._last_error)

    def _detach_client(self):
        """Odłącza bieżącego klienta (pod blokadą); zwraca (klient, czy_połączony)."""
        client, connected = self._client, self._connected
        self._client = None
        self._connected = False
        self._discovered.clear()
        self._dev_online.clear()
        return (client, connected) if client else None

    @staticmethod
    def _shutdown(old, old_cfg, cleanup=((), ())):
        """Zamyka starego klienta. Tematy budujemy ze STAREJ konfiguracji; cleanup = (tematy
        discovery do usunięcia, urządzenia, których retained availability/state usuwamy)."""
        if not old:
            return
        client, connected = old
        prefix = old_cfg["topic_prefix"]
        try:
            if connected:
                topics, devices = cleanup
                for topic in topics:
                    client.publish(topic, "", qos=1, retain=True)
                for dev_id in devices:
                    client.publish(f"{prefix}/{dev_id}/availability", "", qos=1, retain=True)
                    client.publish(f"{prefix}/{dev_id}/state", "", qos=1, retain=True)
                info = client.publish(f"{prefix}/status", "offline", qos=1, retain=True)
                try:
                    info.wait_for_publish(2)
                except TypeError:  # paho < 1.6 bez parametru timeout
                    pass
            client.disconnect()
            client.loop_stop()
        except Exception:  # noqa: BLE001
            pass

    def stop(self):
        """Zatrzymanie przy wyjściu z aplikacji (synchronicznie: retained "offline" musi wyjść)."""
        with self._lock:
            self._gen += 1
            self._pending = False
            old_cfg = self._cfg or self.config.get()["mqtt"]
            old = self._detach_client()
        self._shutdown(old, old_cfg)

    def on_preset_change(self, device_id):
        """Listener pollera: preset urządzenia się zmienił albo przestał się wczytywać."""
        with self._lock:
            if not (self._client and self._connected and self._cfg):
                return
            if self._cfg["ha_discovery"]:
                self._publish_discovery(device_id)
            self._sync_availability()

    # ── callbacki paho (v1 i v2) ───────────────────────────────
    def _on_connect(self, client, userdata, flags, rc, *props):
        code = getattr(rc, "value", rc)
        with self._lock:
            if client is not self._client:
                return  # stary klient (przełączanie konfiguracji)
            if code != 0:
                self._last_error = f"broker odrzucił połączenie (kod {rc})"
                log.warning("MQTT: %s", self._last_error)
                return
            self._connected = True
            self._last_error = None
            self._dev_online.clear()
            client.publish(self._t("status"), "online", qos=1, retain=True)
            if self._cfg["ha_discovery"]:
                client.subscribe(f"{self._cfg['ha_prefix']}/status")
                # przegląd encji zapamiętanych w brokerze: usuwamy te, których już nie ma
                # (urządzenie usunięte przy wyłączonym MQTT albo zatrzymanej aplikacji)
                client.subscribe(self._discovery_filter())
                self._publish_discovery_all()
            self._sync_availability()
            host, port = self._cfg["host"], self._cfg["port"]
        log.info("MQTT: połączono z %s:%s", host, port)

    def _on_connect_fail(self, client, userdata, *args):
        with self._lock:
            if client is not self._client:
                return
            cfg = self._cfg or {}
            self._connected = False
            self._last_error = f"nie można połączyć z brokerem {cfg.get('host')}:{cfg.get('port')}"

    def _on_disconnect(self, client, userdata, *args):
        with self._lock:
            if client is not self._client:
                return
            self._connected = False
            rc = args[-2] if len(args) >= 2 else (args[0] if args else 0)
            code = getattr(rc, "value", rc)
            if code:
                self._last_error = f"rozłączono (kod {rc})"

    def _on_message(self, client, userdata, msg):
        # wyjątek z callbacku zatrzymałby wątek sieciowy paho na dobre - nigdy go nie przepuszczamy
        with self._lock:
            try:
                if client is not self._client or not (self._cfg and self._cfg["ha_discovery"]):
                    return
                if msg.topic == f"{self._cfg['ha_prefix']}/status":
                    # Home Assistant po restarcie wysyła "online" - ponownie publikujemy discovery
                    if msg.payload == b"online":
                        self._publish_discovery_all()
                elif msg.retain and msg.payload and self._is_discovery_topic(msg.topic):
                    self._sweep(msg.topic, msg.payload)
            except Exception:  # noqa: BLE001
                log.exception("MQTT: wiadomość %s", getattr(msg, "topic", "?"))

    def _discovery_filter(self):
        return f"{self._cfg['ha_prefix']}/sensor/{safe_key(self._cfg['topic_prefix'])}/+/config"

    def _is_discovery_topic(self, topic):
        """Temat pasuje do filtra discovery naszego węzła (nie ufamy dopasowaniu po stronie brokera)."""
        head = f"{self._cfg['ha_prefix']}/sensor/{safe_key(self._cfg['topic_prefix'])}/"
        rest = topic[len(head):] if topic.startswith(head) else ""
        return rest.endswith("/config") and rest.count("/") == 1 and len(rest) > len("/config")

    def _sweep(self, topic, payload):
        """Zapamiętana w brokerze encja discovery naszego węzła, której już nie publikujemy -> usuwamy
        ją (razem z retained availability/state urządzenia, jeśli to urządzenie już nie istnieje)."""
        current = set().union(*self._discovered.values()) if self._discovered else set()
        if topic in current:
            return
        devices = set(self.poller.device_ids())
        mine = {f"{safe_key(self._cfg['topic_prefix'])}_{safe_key(d)}" for d in devices}
        try:
            data = json.loads(payload)
            ident = (data.get("device") or {}).get("identifiers") or [None]
            ident = ident[0] if isinstance(ident, list) else ident
        except (ValueError, AttributeError, TypeError, RecursionError):
            data, ident = {}, None
        if not isinstance(ident, str):
            ident = None
        if ident in mine and not self._dev_ready(ident, devices):
            return  # urządzenie istnieje, ale jego discovery jeszcze nie wyszło (np. brak presetu)
        self._pub(topic, "", retain=True)
        if ident not in mine and isinstance(data, dict):
            live = {self._t(d, "availability") for d in devices} | {self._t(d, "state") for d in devices}
            avail = data.get("availability")
            stale = [a.get("topic") for a in avail if isinstance(a, dict)] if isinstance(avail, list) else []
            stale.append(data.get("state_topic"))
            for t in stale:
                if isinstance(t, str) and t.startswith(self._t("")) and t != self._t("status") and t not in live:
                    self._pub(t, "", retain=True)

    def _dev_ready(self, ident, devices):
        """Czy discovery urządzenia o tym identyfikatorze zostało już opublikowane w tej sesji."""
        node = safe_key(self._cfg["topic_prefix"])
        return any(f"{node}_{safe_key(d)}" == ident and d in self._discovered for d in devices)

    # ── publikacja ─────────────────────────────────────────────
    def _t(self, *parts):
        prefix = (self._cfg or self.config.get()["mqtt"])["topic_prefix"]
        return "/".join([prefix, *parts])

    def _pub(self, topic, payload, retain=False):
        if not (self._client and self._connected):
            return
        if not isinstance(payload, (str, bytes)):
            payload = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        self._client.publish(topic, payload, qos=0, retain=retain)
        self._published += 1

    def on_sample(self, device_id, runtime, sample):
        """Listener pollera."""
        with self._lock:
            if not (self._client and self._connected and self._cfg):
                return
            if runtime is not None and (not runtime.cfg.get("enabled", True) or runtime.reader is None
                                        or getattr(runtime, "preset_error", None)):
                # ręczny odczyt wyłączonego urządzenia albo odczyt w toku przy wyłączaniu:
                # w HA urządzenie zostaje "offline", tak jak na dashboardzie
                if self._dev_online.get(device_id) is not False:
                    self._dev_online[device_id] = False
                    self._pub(self._t(device_id, "availability"), "offline", retain=True)
                return
            online = bool(sample["ok"])
            if self._dev_online.get(device_id) != online:
                self._dev_online[device_id] = online
                self._pub(self._t(device_id, "availability"),
                          "online" if online else "offline", retain=True)
            if not online:
                return
            now = time.monotonic()
            if now - self._last_pub.get(device_id, 0) < self._cfg["interval"]:
                return
            self._last_pub[device_id] = now
            if self._cfg["ha_discovery"] and device_id not in self._discovered:
                self._publish_discovery(device_id)
            state = {k: v for k, v in sample["values"].items() if v is not None}
            state["ts"] = round(sample["ts"], 3)
            self._pub(self._t(device_id, "state"), state, retain=self._cfg["retain"])

    def _publish_discovery_all(self):
        if not (self._cfg and self._cfg["ha_discovery"] and self._connected):
            return
        current = set(self.poller.device_ids())
        for dev_id in list(self._discovered):
            if dev_id not in current:
                self._forget(dev_id)
        for dev_id in current:
            self._publish_discovery(dev_id)

    def _forget(self, device_id, availability=True):
        """Usuwa encje discovery urządzenia (i retained availability/state usuniętego urządzenia)."""
        for topic in self._discovered.pop(device_id, ()):
            self._pub(topic, "", retain=True)
        if availability:
            self._dev_online.pop(device_id, None)
            self._pub(self._t(device_id, "availability"), "", retain=True)
            if self._cfg and self._cfg["retain"]:
                self._pub(self._t(device_id, "state"), "", retain=True)

    def _sync_availability(self):
        """Urządzenia, których nie odpytujemy (wyłączone, bez presetu), są "offline" także w HA -
        availability publikuje się przy odczycie, a tych urządzeń nikt nie odczytuje."""
        if not (self._client and self._connected):
            return
        for dev_id in self.poller.device_ids():
            data = self.poller.values(dev_id)
            state = ((data or {}).get("status") or {}).get("state")
            if state in ("disabled", "no_preset") and self._dev_online.get(dev_id) is not False:
                self._dev_online[dev_id] = False
                self._pub(self._t(dev_id, "availability"), "offline", retain=True)

    def _publish_discovery(self, device_id):
        data = self.poller.values(device_id)
        if not data or not data["meta"]:
            self._forget(device_id, availability=False)  # preset usunięty - encje znikają
            return
        cfg = self._cfg
        node = safe_key(cfg["topic_prefix"])
        dev_key = safe_key(device_id)
        preset = data["preset"] or {}
        device_info = {
            "identifiers": [f"{node}_{dev_key}"],
            "name": data["device"].get("name") or device_id,
            "manufacturer": preset.get("manufacturer") or "Modbus",
            "model": preset.get("model") or preset.get("name") or "",
            "sw_version": f"modbus-dash {__version__}",
        }
        topics = set()
        old = self._discovered.get(device_id, set())
        for key, m in data["meta"].items():
            obj = f"{dev_key}_{safe_key(key)}"
            topic = f"{cfg['ha_prefix']}/sensor/{node}/{obj}/config"
            unit = normalize_unit(m.get("unit") or "")
            dc, sc = ha_classes(key, unit)
            payload = {
                "name": m.get("label") or key,
                "unique_id": f"{node}_{obj}",
                "object_id": f"{node}_{obj}",
                "state_topic": self._t(device_id, "state"),
                "value_template": "{{ value_json[" + json.dumps(key) + "] }}",
                "availability": [{"topic": self._t("status")},
                                 {"topic": self._t(device_id, "availability")}],
                "availability_mode": "all",
                "device": device_info,
                "suggested_display_precision": m.get("decimals", 2),
            }
            if unit:
                payload["unit_of_measurement"] = unit
            if dc:
                payload["device_class"] = dc
            if sc:
                payload["state_class"] = sc
            self._pub(topic, payload, retain=True)
            topics.add(topic)
        for topic in old - topics:
            self._pub(topic, "", retain=True)
        self._discovered[device_id] = topics

    def status(self):
        with self._lock:
            cfg = self._cfg or self.config.get()["mqtt"]
            return {
                "available": available(),
                "enabled": bool(cfg["enabled"]),
                "connected": self._connected,
                "last_error": self._last_error,
                "published": self._published,
                "discovered_devices": sorted(self._discovered),
            }
