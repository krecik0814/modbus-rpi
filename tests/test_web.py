"""Testy integracyjne: aplikacja Flask + wbudowany symulator + prawdziwy transport pymodbus."""

import json
import socket
import time

import pytest

import app as app_module
from modbus_dash.web import create_app


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait(fn, timeout=8.0, step=0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        v = fn()
        if v:
            return v
        time.sleep(step)
    return fn()


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    data = tmp_path_factory.mktemp("data")
    port = _free_port()
    args = app_module.parse_args(["--data-dir", str(data), "--modbus-port", str(port),
                                  "--sim-preset", "simulator_3f:2", "--log-level", "WARNING"])
    ctx = app_module.AppContext(args)
    flask_app = create_app(ctx)
    flask_app.testing = True
    ctx.start()
    client = flask_app.test_client()
    yield ctx, client, port
    ctx.shutdown()


def post(client, url, body=None, **kw):
    return client.post(url, data=json.dumps(body if body is not None else {}),
                       content_type="application/json", **kw)


def put(client, url, body):
    return client.put(url, data=json.dumps(body), content_type="application/json")


def test_index_and_info(env):
    ctx, c, port = env
    r = c.get("/")
    assert r.status_code == 200 and b"<html" in r.data.lower()
    info = c.get("/api/info").get_json()
    assert info["version"] and info["simulator"]["port"] == port
    assert {d["unit"] for d in info["simulator"]["devices"]} == {1, 2}
    assert c.get("/api/config").get_json()["mode"] == "tcp"


def test_simulator_device_is_polled(env):
    ctx, c, _ = env
    data = _wait(lambda: (lambda d: d if d["status"]["state"] == "ok" else None)(
        c.get("/api/devices/symulator/values").get_json()))
    assert data, c.get("/api/devices/symulator/values").get_json()
    assert 200 < data["values"]["voltage_l1"] < 260
    assert 49 < data["values"]["frequency"] < 51
    assert data["meta"]["voltage_l1"]["unit"] == "V"
    devs = {d["id"]: d for d in c.get("/api/devices").get_json()}
    assert devs["symulator"]["locked"] and devs["sym-2"]["unit"] == 2
    hist = _wait(lambda: (lambda h: h if len(h["points"]) >= 2 else None)(
        c.get("/api/devices/symulator/history?seconds=60").get_json()))
    assert hist and hist["source"] == "memory" and "voltage_l1" in hist["keys"]
    csv = c.get("/api/devices/symulator/history.csv?seconds=60")
    assert csv.status_code == 200 and "Napięcie L1 [V]" in csv.get_data(as_text=True)


def test_legacy_live_scan_ping(env):
    ctx, c, port = env
    r = post(c, "/api/live", {"host": "127.0.0.1", "port": port, "unit": 1, "preset": "simulator_3f"})
    assert r.status_code == 200, r.get_json()
    vals = r.get_json()["values"]
    assert 49 < vals["frequency"]["value"] < 51 and vals["voltage_l1"]["unit"] == "V"
    r = post(c, "/api/scan", {"host": "127.0.0.1", "port": port, "unit": 1, "start": 0, "end": 70})
    assert r.status_code == 200, r.get_json()
    rows = {row["address"]: row for row in r.get_json()["registers"]}
    assert rows[36]["hint"]["guess"] == "frequency"
    assert rows[0]["hint"]["guess"] == "voltage"
    r = post(c, "/api/ping", {"host": "127.0.0.1", "port": port})
    assert r.status_code == 200 and r.get_json()["ok"]
    r = post(c, "/api/ping", {"host": "127.0.0.1", "port": _free_port()})
    assert r.status_code == 502 and not r.get_json()["ok"]


@pytest.mark.parametrize("body,frag", [
    ({"bus": "sim", "start": 10, "end": 5}, "end"),
    ({"bus": "sim", "start": "abc"}, "start"),
    ({"bus": "sim", "start": 0, "end": 5000}, "zakres"),
    ({"bus": "sim", "register_type": "foo"}, "register_type"),
    ({"bus": "nieznana"}, "magistrala"),
])
def test_scan_validation(env, body, frag):
    ctx, c, _ = env
    r = post(c, "/api/scan", body)
    assert 400 <= r.status_code < 500
    assert frag in r.get_json()["error"]


def test_scan_rejects_non_object_body(env):
    ctx, c, _ = env
    r = c.post("/api/scan", data="null", content_type="application/json")
    assert r.status_code == 400 and "JSON" in r.get_json()["error"]


def test_scan_holding_and_coils(env):
    ctx, c, _ = env
    r = post(c, "/api/scan", {"bus": "sim", "unit": 1, "start": 0, "end": 16, "register_type": "coil"})
    assert r.status_code in (200, 502)
    r = post(c, "/api/scan", {"bus": "sim", "unit": 1, "start": 0, "end": 10, "step": 1})
    assert r.status_code == 200 and r.get_json()["count"] == 10


def test_preset_crud_and_protection(env):
    ctx, c, _ = env
    body = {"name": "Test <b>xss</b>", "registers": {"voltage_l1": {"address": 0, "unit": "V"}}}
    r = post(c, "/api/presets", body)
    assert r.status_code == 201
    pid = r.get_json()["id"]
    r2 = post(c, "/api/presets", body)
    assert r2.get_json()["id"] != pid  # bez nadpisywania
    assert c.get(f"/api/presets/{pid}").get_json()["name"] == "Test <b>xss</b>"
    r = put(c, f"/api/presets/{pid}", {"name": "x", "registers": {"v": {"address": "zz"}}})
    assert r.status_code == 400 and r.get_json()["errors"]
    assert put(c, "/api/presets/simulator_3f", body).status_code == 403
    assert c.delete("/api/presets/simulator_3f").status_code == 403
    assert c.delete(f"/api/presets/{pid}").status_code == 200
    assert c.delete(f"/api/presets/{pid}").status_code == 404
    c.delete(f"/api/presets/{r2.get_json()['id']}")
    assert post(c, "/api/live", {"preset": "../../app", "bus": "sim"}).status_code == 404
    assert post(c, "/api/presets/validate", {"registers": {"a": {"address": -5}}}).get_json()["ok"] is False


def test_csrf_and_content_type(env):
    ctx, c, _ = env
    r = c.post("/api/presets", data=json.dumps({"name": "a"}), content_type="application/json",
               headers={"Origin": "http://evil.example"})
    assert r.status_code == 403
    r = c.delete("/api/presets/x", headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403
    r = c.post("/api/presets", data="name=a", content_type="application/x-www-form-urlencoded")
    assert r.status_code == 415


def test_buses_and_devices(env):
    ctx, c, port = env
    r = put(c, "/api/buses/lab", {"name": "Lab", "kind": "tcp", "host": "127.0.0.1", "port": port, "timeout": 0.5})
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["describe"]
    r = post(c, "/api/buses/lab/ping", {"unit": 1})
    assert r.status_code == 200
    assert put(c, "/api/buses/sim", {"kind": "tcp"}).status_code == 400  # zablokowana (CLI)
    assert put(c, "/api/buses/zla", {"kind": "rtu", "serial_port": ""}).status_code == 400
    r = put(c, "/api/devices/lab1", {"name": "Lab 1", "bus": "lab", "unit": 2, "preset": "simulator_3f",
                                     "interval": 0.5})
    assert r.status_code == 200, r.get_json()
    data = _wait(lambda: (lambda d: d if d["status"]["state"] == "ok" else None)(
        c.get("/api/devices/lab1/values").get_json()))
    assert data and data["values"]["voltage_l1"]
    assert c.delete("/api/buses/lab").status_code == 400  # używana przez urządzenie
    assert put(c, "/api/devices/lab1", {"bus": "brak"}).status_code == 400
    assert put(c, "/api/devices/BAD ID", {"bus": "lab"}).status_code in (400, 404)
    assert c.delete("/api/devices/lab1").status_code == 200
    assert c.get("/api/devices/lab1/values").status_code == 404
    assert c.delete("/api/buses/lab").status_code == 200


def _job(c, r):
    assert r.status_code == 202, r.get_json()
    job_id = r.get_json()["id"]
    return _wait(lambda: (lambda j: j if j["state"] != "running" else None)(
        c.get(f"/api/jobs/{job_id}").get_json()), timeout=30)


def test_unit_scan_and_detect(env):
    ctx, c, _ = env
    job = _job(c, post(c, "/api/scan/units", {"bus": "sim", "first": 1, "last": 4, "timeout": 0.2}))
    assert job["state"] == "done", job
    assert [f["unit"] for f in job["result"]["found"]] == [1, 2]
    job = _job(c, post(c, "/api/detect", {"bus": "sim", "unit": 1}))
    assert job["state"] == "done", job
    assert job["result"]["candidates"][0]["id"] == "simulator_3f"


def test_write_disabled_by_default(env):
    ctx, c, _ = env
    assert post(c, "/api/write", {"bus": "sim", "unit": 1, "address": 0, "value": 1}).status_code == 403


def test_metrics_and_health(env):
    ctx, c, _ = env
    _wait(lambda: c.get("/api/health").status_code == 200)
    text = c.get("/metrics").get_data(as_text=True)
    assert 'modbus_dash_value{device="symulator"' in text
    assert c.get("/api/health").get_json()["devices"]["symulator"] == "ok"


def test_settings_roundtrip(env):
    ctx, c, _ = env
    r = put(c, "/api/settings/mqtt", {"enabled": False, "host": "broker", "port": 1884, "password": "tajne"})
    assert r.status_code == 200 and r.get_json()["settings"]["password"] == "********"
    r = put(c, "/api/settings/mqtt", {"enabled": False, "host": "broker", "port": 1884, "password": "********"})
    assert ctx.config.get()["mqtt"]["password"] == "tajne"
    assert put(c, "/api/settings/mqtt", {"port": 0}).status_code == 400
    r = put(c, "/api/settings/history", {"bucket_seconds": 30})
    assert r.status_code == 200 and r.get_json()["settings"]["bucket_seconds"] == 30
    assert put(c, "/api/settings/history", {"bucket_seconds": 7}).status_code == 400


def test_auth(tmp_path):
    args = app_module.parse_args(["--data-dir", str(tmp_path), "--no-sim", "--no-history", "--auth", "admin:tajne"])
    ctx = app_module.AppContext(args)
    c = create_app(ctx).test_client()
    try:
        assert c.get("/api/info").status_code == 401
        import base64
        tok = base64.b64encode(b"admin:tajne").decode()
        assert c.get("/api/info", headers={"Authorization": f"Basic {tok}"}).status_code == 200
        bad = base64.b64encode(b"admin:zle").decode()
        assert c.get("/api/info", headers={"Authorization": f"Basic {bad}"}).status_code == 401
    finally:
        ctx.shutdown()


def test_unknown_api_route_is_json(env):
    ctx, c, _ = env
    r = c.get("/api/nie-ma")
    assert r.status_code == 404 and r.get_json()["error"]
