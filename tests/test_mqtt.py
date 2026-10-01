"""Test MQTT / Home Assistant discovery z prawdziwym brokerem (amqtt), o ile jest zainstalowany."""

import json
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

paho = pytest.importorskip("paho.mqtt.client")
AMQTT = shutil.which("amqtt") or str(Path(sys.executable).parent / "amqtt")
if not Path(AMQTT).exists():
    pytest.skip("brak brokera amqtt", allow_module_level=True)

from modbus_dash.mqtt import MqttPublisher  # noqa: E402


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def broker(tmp_path_factory):
    port = _free_port()
    cfg = tmp_path_factory.mktemp("mqtt") / "broker.yaml"
    cfg.write_text(f"listeners:\n  default:\n    type: tcp\n    bind: 127.0.0.1:{port}\n"
                   "plugins:\n  amqtt.plugins.authentication.AnonymousAuthPlugin:\n    allow_anonymous: true\n")
    proc = subprocess.Popen([AMQTT, "-c", str(cfg)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    else:
        proc.kill()
        pytest.skip("broker amqtt nie wystartował")
    yield port
    proc.terminate()
    proc.wait(timeout=5)


class FakeConfig:
    def __init__(self, port):
        self.data = {"mqtt": {"enabled": True, "host": "127.0.0.1", "port": port, "username": "", "password": "",
                              "tls": False, "topic_prefix": "md-test", "ha_discovery": True,
                              "ha_prefix": "homeassistant", "interval": 1, "retain": False}}

    def get(self):
        return self.data


class FakePoller:
    def device_ids(self):
        return ["licznik"]

    def values(self, dev_id):
        return {
            "device": {"id": dev_id, "name": "Licznik główny"},
            "preset": {"id": "p", "name": "Test", "manufacturer": "Eastron", "model": "SDM630", "phases": 3},
            "meta": {
                "voltage_l1": {"label": "Napięcie L1", "unit": "V", "group": "voltage", "decimals": 1},
                "energy_import": {"label": "Energia pobrana", "unit": "kWh", "group": "energy", "decimals": 2},
                "reactive_total": {"label": "Moc bierna", "unit": "VAr", "group": "total", "decimals": 0},
            },
            "values": {},
            "status": {"state": "ok"},
        }


def test_publish_state_and_discovery(broker):
    got = {}
    lock = threading.Lock()

    def on_message(c, u, msg):
        with lock:
            got[msg.topic] = msg.payload.decode()

    if hasattr(paho, "CallbackAPIVersion"):
        sub = paho.Client(paho.CallbackAPIVersion.VERSION2, client_id="test-sub")
    else:
        sub = paho.Client(client_id="test-sub")
    sub.on_message = on_message
    sub.connect("127.0.0.1", broker)
    sub.subscribe("md-test/#")
    sub.subscribe("homeassistant/#")
    sub.loop_start()

    pub = MqttPublisher(FakeConfig(broker), FakePoller())
    pub.apply()
    try:
        deadline = time.time() + 10
        while not pub.status()["connected"] and time.time() < deadline:
            time.sleep(0.1)
        assert pub.status()["connected"], pub.status()
        sample = {"ok": True, "ts": time.time(), "values": {"voltage_l1": 230.4, "energy_import": 12.5,
                                                             "reactive_total": None}}
        deadline = time.time() + 10
        while "md-test/licznik/state" not in got and time.time() < deadline:
            pub.on_sample("licznik", None, sample)
            time.sleep(0.3)
        with lock:
            state = json.loads(got["md-test/licznik/state"])
            assert state["voltage_l1"] == 230.4 and "reactive_total" not in state
            assert got.get("md-test/status") == "online"
            assert got.get("md-test/licznik/availability") == "online"
            cfg = json.loads(got["homeassistant/sensor/md_test/licznik_energy_import/config"])
        assert cfg["device_class"] == "energy" and cfg["state_class"] == "total_increasing"
        assert cfg["unit_of_measurement"] == "kWh"
        assert cfg["state_topic"] == "md-test/licznik/state"
        assert cfg["value_template"] == '{{ value_json["energy_import"] }}'
        assert cfg["device"]["manufacturer"] == "Eastron"
        with lock:
            react = json.loads(got["homeassistant/sensor/md_test/licznik_reactive_total/config"])
        assert react["unit_of_measurement"] == "var" and react["device_class"] == "reactive_power"
        # urządzenie przestaje odpowiadać -> availability offline
        pub.on_sample("licznik", None, {"ok": False, "ts": time.time(), "values": {}})
        deadline = time.time() + 5
        while got.get("md-test/licznik/availability") != "offline" and time.time() < deadline:
            time.sleep(0.1)
        assert got.get("md-test/licznik/availability") == "offline"
    finally:
        pub.stop()
        sub.loop_stop()
        sub.disconnect()
    assert not pub.status()["connected"]


_COL_IDS = iter(range(1, 10**6))


class Collector:
    """Klient-podglądacz: zbiera ostatnie (retained) wiadomości z brokera."""

    def __init__(self, port, *filters):
        self.got, self.lock = {}, threading.Lock()
        cid = f"col-{next(_COL_IDS)}"
        if hasattr(paho, "CallbackAPIVersion"):
            self.c = paho.Client(paho.CallbackAPIVersion.VERSION2, client_id=cid)
        else:
            self.c = paho.Client(client_id=cid)
        self.c.on_message = self._on
        self.c.connect("127.0.0.1", port)
        for f in filters:
            self.c.subscribe(f)
        self.c.loop_start()

    def _on(self, c, u, msg):
        with self.lock:
            self.got[msg.topic] = msg.payload.decode()

    def wait(self, pred, timeout=8):
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self.lock:
                if pred(dict(self.got)):
                    return True
            time.sleep(0.1)
        return False

    def stop(self):
        self.c.loop_stop()
        self.c.disconnect()


class Poller2(FakePoller):
    def __init__(self, ids, states=None):
        self.ids, self.states = list(ids), dict(states or {})

    def device_ids(self):
        return list(self.ids)

    def values(self, dev_id):
        v = super().values(dev_id)
        v["status"] = {"state": self.states.get(dev_id, "ok")}
        return v


def _connected(pub):
    deadline = time.time() + 10
    while not pub.status()["connected"] and time.time() < deadline:
        time.sleep(0.1)
    return pub.status()["connected"]


def test_prefix_change_cleans_old_entities(broker):
    cfg = FakeConfig(broker)
    cfg.data["mqtt"]["topic_prefix"] = "pfx-a"
    col = Collector(broker, "pfx-a/#", "pfx-b/#", "homeassistant/sensor/pfx_a/#", "homeassistant/sensor/pfx_b/#")
    pub = MqttPublisher(cfg, Poller2(["m1"]))
    try:
        pub.apply()
        assert _connected(pub)
        pub.on_sample("m1", None, {"ok": True, "ts": time.time(), "values": {"voltage_l1": 230.0}})
        old_cfg = "homeassistant/sensor/pfx_a/m1_voltage_l1/config"
        assert col.wait(lambda g: g.get(old_cfg) and g.get("pfx-a/m1/availability") == "online")
        t0 = time.monotonic()
        cfg.data["mqtt"] = {**cfg.data["mqtt"], "topic_prefix": "pfx-b"}
        pub.apply()
        assert time.monotonic() - t0 < 0.5  # przełączenie w tle - zapis ustawień nie czeka
        assert col.wait(lambda g: g.get(old_cfg) == "" and g.get("pfx-a/status") == "offline"
                        and g.get("pfx-a/m1/availability") == ""), col.got
        assert _connected(pub)
        assert col.wait(lambda g: g.get("pfx-b/status") == "online"
                        and g.get("homeassistant/sensor/pfx_b/m1_voltage_l1/config"))
    finally:
        pub.stop()
        col.stop()


def test_stale_discovery_is_swept_and_disabled_device_is_offline(broker):
    cfg = FakeConfig(broker)
    cfg.data["mqtt"]["topic_prefix"] = "sweep"
    col = Collector(broker, "sweep/#", "homeassistant/sensor/sweep/#")
    pub = MqttPublisher(cfg, Poller2(["keep", "gone"]))
    try:
        pub.apply()
        assert _connected(pub)
        for dev in ("keep", "gone"):
            pub.on_sample(dev, None, {"ok": True, "ts": time.time(), "values": {"voltage_l1": 230.0}})
        gone = "homeassistant/sensor/sweep/gone_voltage_l1/config"
        assert col.wait(lambda g: g.get(gone) and g.get("sweep/gone/availability") == "online")
    finally:
        pub.stop()
    # urządzenie usunięte, gdy aplikacja nie działała; drugie wyłączone
    pub = MqttPublisher(cfg, Poller2(["keep"], {"keep": "disabled"}))
    try:
        pub.apply()
        assert _connected(pub)
        assert col.wait(lambda g: g.get(gone) == "" and g.get("sweep/gone/availability") == ""
                        and g.get("sweep/keep/availability") == "offline"
                        and g.get("homeassistant/sensor/sweep/keep_voltage_l1/config")), col.got
    finally:
        pub.stop()
        col.stop()
