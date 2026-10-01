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
