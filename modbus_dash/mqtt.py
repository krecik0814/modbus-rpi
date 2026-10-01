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
        self._client = None
        self._cfg = None
        self._connected = False
        self._last_error = None
        self._last_pub = {}          # device -> monotonic
        self._discovered = {}        # device -> set(topic)
        self._dev_online = {}        # device -> bool
        self._published = 0

    # ── konfiguracja ───────────────────────────────────────────
    def apply(self, *_):
        cfg = self.config.get()["mqtt"]
        old = None
        with self._lock:
            if cfg == self._cfg and (self._client or not cfg["enabled"]):
                if self._connected:
                    self._publish_discovery_all()
                return
            old = self._detach_client()
            self._cfg = cfg
            if cfg["enabled"] and _paho is None:
                self._last_error = "brak biblioteki paho-mqtt (pip install paho-mqtt)"
                log.warning("MQTT: %s", self._last_error)
            elif cfg["enabled"]:
                self._start_client(cfg)
        # zatrzymanie poza blokadą - wątek paho może czekać na nią w callbacku
        self._shutdown(old)

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

    def _shutdown(self, old):
        if not old:
            return
        client, connected = old
        try:
            if connected:
                info = client.publish(self._t("status"), "offline", qos=1, retain=True)
                try:
                    info.wait_for_publish(2)
                except TypeError:  # paho < 1.6 bez parametru timeout
                    pass
            client.disconnect()
            client.loop_stop()
        except Exception:  # noqa: BLE001
            pass

    def stop(self):
        with self._lock:
            old = self._detach_client()
        self._shutdown(old)

    # ── callbacki paho (v1 i v2) ───────────────────────────────
    def _on_connect(self, client, userdata, flags, rc, *props):
        code = getattr(rc, "value", rc)
        if code != 0:
            self._last_error = f"broker odrzucił połączenie (kod {rc})"
            log.warning("MQTT: %s", self._last_error)
            return
        with self._lock:
            self._connected = True
            self._last_error = None
            self._discovered.clear()
            self._dev_online.clear()
            client.publish(self._t("status"), "online", qos=1, retain=True)
            if self._cfg and self._cfg["ha_discovery"]:
                client.subscribe(f"{self._cfg['ha_prefix']}/status")
                self._publish_discovery_all()
        log.info("MQTT: połączono z %s:%s", self._cfg["host"], self._cfg["port"])

    def _on_connect_fail(self, client, userdata, *args):
        cfg = self._cfg or {}
        self._connected = False
        self._last_error = f"nie można połączyć z brokerem {cfg.get('host')}:{cfg.get('port')}"

    def _on_disconnect(self, client, userdata, *args):
        self._connected = False
        rc = args[-2] if len(args) >= 2 else (args[0] if args else 0)
        code = getattr(rc, "value", rc)
        if code:
            self._last_error = f"rozłączono (kod {rc})"

    def _on_message(self, client, userdata, msg):
        # Home Assistant po restarcie wysyła "online" - ponownie publikujemy discovery
        if msg.payload == b"online":
            with self._lock:
                self._discovered.clear()
                self._publish_discovery_all()

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
                for topic in self._discovered.pop(dev_id):
                    self._pub(topic, "", retain=True)
        for dev_id in current:
            self._publish_discovery(dev_id)

    def _publish_discovery(self, device_id):
        data = self.poller.values(device_id)
        if not data or not data["meta"]:
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
