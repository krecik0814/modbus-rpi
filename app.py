import json, os, struct, math, time, random, threading, argparse, logging
from pathlib import Path
from flask import Flask, jsonify, request, send_from_directory

# ── pymodbus compat ─────────────────────────────────────────────

from pymodbus.datastore import ModbusSequentialDataBlock, ModbusServerContext
try:
    from pymodbus.datastore import ModbusDeviceContext as SlaveContext
except ImportError:
    from pymodbus.datastore import ModbusSlaveContext as SlaveContext
try:
    from pymodbus.server import StartTcpServer
except ImportError:
    from pymodbus.server.sync import StartTcpServer
from pymodbus.client import ModbusTcpClient
try:
    from pymodbus.client import ModbusSerialClient
    HAS_SERIAL = True
except ImportError:
    HAS_SERIAL = False

import pymodbus
_PV = tuple(int(x) for x in pymodbus.__version__.split(".")[:2])
USE_DEVICE_ID = _PV >= (3, 10)

def _make_ctx(store, single=True):
    for kw in ("devices", "device_ids", "slaves"):
        try: return ModbusServerContext(**{kw: store}, single=single)
        except TypeError: continue
    raise RuntimeError("Nieznana wersja pymodbus")

def _kw_unit(unit):
    return {"device_id": unit} if USE_DEVICE_ID else {"slave": unit}

# ── Float helpers ───────────────────────────────────────────────

def float_to_regs(v):
    p = struct.pack(">f", v)
    return [struct.unpack(">H", p[0:2])[0], struct.unpack(">H", p[2:4])[0]]

def regs_to_float_be(r0, r1):
    return struct.unpack(">f", struct.pack(">HH", r0, r1))[0]
def regs_to_float_ws(r0, r1):
    return struct.unpack(">f", struct.pack(">HH", r1, r0))[0]
def regs_to_float_le(r0, r1):
    return struct.unpack("<f", struct.pack("<HH", r0, r1))[0]

DECODERS = {"big_endian": regs_to_float_be, "word_swap": regs_to_float_ws, "little_endian": regs_to_float_le}

HINTS = [
    (200,260,"voltage","Napięcie","V"), (340,420,"line_volt","Nap. międzyfaz.","V"),
    (0.01,100,"current","Prąd","A"), (49,51.5,"frequency","Częstotliwość","Hz"),
    (0.5,1.01,"pf","cos φ",""), (100,50000,"power","Moc","W"),
    (0.1,30,"thd","THD","%"),
]

def guess_value(v):
    if v is None or math.isnan(v) or math.isinf(v): return None
    av = abs(v)
    for lo,hi,key,label,unit in HINTS:
        if lo <= av <= hi: return {"guess":key,"label":label,"unit":unit}
    if av > 100: return {"guess":"energy","label":"Energia","unit":"kWh"}
    return None

# ── Simulator ──────────────────────────────────────────────────

class Simulator:
    def __init__(self):
        self.t0=time.time(); self.e_imp=12345.678; self.e_exp=234.567; self.e_react=1023.456; self.last=time.time()
    def tick(self):
        now=time.time(); dt=now-self.last; self.last=now
        n=lambda b,p=0.02: b*(1+random.gauss(0,p))
        d=lambda b,a,T: b+a*math.sin(2*math.pi*(now-self.t0)/T)
        v1,v2,v3=n(d(230,3,120),.005),n(d(229.5,2.5,150),.005),n(d(231,2,180),.005)
        i1,i2,i3=max(.01,n(d(2.5,.8,60),.03)),max(.01,n(d(1.8,.5,90),.03)),max(.01,n(d(3.2,1,45),.03))
        pf1,pf2,pf3=min(1,max(.7,n(.95,.01))),min(1,max(.7,n(.92,.01))),min(1,max(.7,n(.88,.02)))
        p1,p2,p3=v1*i1*pf1,v2*i2*pf2,v3*i3*pf3; s1,s2,s3=v1*i1,v2*i2,v3*i3
        q1,q2,q3=[math.sqrt(max(0,s**2-p**2)) for s,p in [(s1,p1),(s2,p2),(s3,p3)]]
        self.e_imp+=(p1+p2+p3)/1e6*dt; self.e_react+=(q1+q2+q3)/1e6*dt
        vals=[v1,v2,v3,i1,i2,i3,p1,p2,p3,s1,s2,s3,q1,q2,q3,pf1,pf2,pf3,n(50,.001),
              p1+p2+p3,s1+s2+s3,q1+q2+q3,self.e_imp,self.e_exp,self.e_react,
              n(v1*1.732,.005),n(v2*1.732,.005),n(v3*1.732,.005),abs(i1-i2)*.3+abs(i2-i3)*.3,
              n(2.5,.1),n(2.8,.1),n(2.3,.1),n(8.5,.1),n(12,.1),n(6.5,.1)]
        regs=[]
        for v in vals: regs.extend(float_to_regs(v))
        return regs

# ── Globals ────────────────────────────────────────────────────

BASE_DIR = Path(__file__).parent.resolve()
PRESETS_DIR = BASE_DIR / "presets"
PRESETS_DIR.mkdir(exist_ok=True)
TEMPLATES_DIR = BASE_DIR / "templates"

sim = Simulator()
sim_context = None
sim_thread_started = False

# Serial config (set via CLI for RPi)
SERIAL_CFG = None  # dict: {port, baudrate, parity, stopbits, bytesize}

log = logging.getLogger("modbus-dash")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")

app = Flask(__name__, static_folder=None)

# ── Simulator server ───────────────────────────────────────────

def start_simulator(port=5020):
    global sim_context, sim_thread_started
    store = SlaveContext(
        di=ModbusSequentialDataBlock(0,[0]*10), co=ModbusSequentialDataBlock(0,[0]*10),
        hr=ModbusSequentialDataBlock(0,[0]*10), ir=ModbusSequentialDataBlock(0,[0]*140))
    sim_context = _make_ctx(store, single=True)

    # Identyfikacja urządzenia (Modbus Device Identification, FC=43)
    try:
        from pymodbus.device import ModbusDeviceIdentification
        identity = ModbusDeviceIdentification()
        identity.VendorName = "SimMeter"
        identity.ProductCode = "SM-630"
        identity.ProductName = "Simulated 3-Phase Energy Meter"
        identity.ModelName = "SM-630-SIM"
        identity.MajorMinorRevision = "2.0.0"
    except Exception:
        identity = None

    def updater():
        while True:
            try: sim_context[0x00].setValues(4, 0, sim.tick())
            except Exception as e: log.error(f"Sim: {e}")
            time.sleep(1)
    def server():
        log.info(f"Symulator Modbus TCP @ 0.0.0.0:{port}")
        kw = {"context": sim_context, "address": ("0.0.0.0", port)}
        if identity: kw["identity"] = identity
        StartTcpServer(**kw)
    if not sim_thread_started:
        threading.Thread(target=updater, daemon=True).start()
        threading.Thread(target=server, daemon=True).start()
        sim_thread_started = True

# ── Preset helpers ─────────────────────────────────────────────

def list_presets():
    result = []
    for f in sorted(PRESETS_DIR.glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            data["_filename"] = f.stem
            result.append(data)
        except: pass
    return result

def get_preset(name):
    p = PRESETS_DIR / f"{name}.json"
    if p.exists(): return json.loads(p.read_text(encoding="utf-8"))
    return None

def save_preset(name, data):
    safe = "".join(c for c in name if c.isalnum() or c in "-_ ").strip() or "preset"
    p = PRESETS_DIR / f"{safe}.json"
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return safe

def delete_preset(name):
    p = PRESETS_DIR / f"{name}.json"
    if p.exists(): p.unlink(); return True
    return False

# ── Modbus client factory ──────────────────────────────────────

def get_client(host=None, port=None):
    """Zwraca klienta TCP lub Serial zależnie od konfiguracji."""
    if SERIAL_CFG:
        if not HAS_SERIAL:
            return None
        c = ModbusSerialClient(
            port=SERIAL_CFG["port"],
            baudrate=SERIAL_CFG["baudrate"],
            parity=SERIAL_CFG.get("parity", "N"),
            stopbits=SERIAL_CFG.get("stopbits", 1),
            bytesize=SERIAL_CFG.get("bytesize", 8),
            timeout=3
        )
        return c if c.connect() else None
    else:
        host = host or "127.0.0.1"
        port = int(port or 5020)
        c = ModbusTcpClient(host, port=port, timeout=3)
        return c if c.connect() else None

def read_registers(client, start, count, unit, reg_type="input"):
    kw = _kw_unit(unit)
    if reg_type == "holding":
        r = client.read_holding_registers(address=start, count=count, **kw)
    else:
        r = client.read_input_registers(address=start, count=count, **kw)
    if r.isError(): return None
    return r.registers

# ── Routes: Static files ───────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory(str(TEMPLATES_DIR), "index.html")

@app.route("/static/<path:filename>")
def static_files(filename):
    return send_from_directory(str(TEMPLATES_DIR), filename)

# ── Routes: API ────────────────────────────────────────────────

@app.route("/api/config", methods=["GET"])
def api_config():
    """Zwraca bieżącą konfigurację (serial vs TCP)."""
    if SERIAL_CFG:
        return jsonify({"mode": "serial", "serial": SERIAL_CFG})
    return jsonify({"mode": "tcp"})

@app.route("/api/presets", methods=["GET"])
def api_list_presets():
    return jsonify(list_presets())

@app.route("/api/presets/<name>", methods=["GET"])
def api_get_preset(name):
    p = get_preset(name)
    return jsonify(p) if p else (jsonify({"error":"not found"}), 404)

@app.route("/api/presets", methods=["POST"])
def api_create_preset():
    data = request.json
    name = data.get("_save_as") or data.get("name", "preset")
    # Wyczyść tymczasowe klucze
    data.pop("_save_as", None)
    data.pop("_filename", None)
    fname = save_preset(name, data)
    return jsonify({"ok": True, "filename": fname})

@app.route("/api/presets/<name>", methods=["PUT"])
def api_update_preset(name):
    data = request.json
    data.pop("_filename", None)
    save_preset(name, data)
    return jsonify({"ok": True})

@app.route("/api/presets/<name>", methods=["DELETE"])
def api_delete_preset(name):
    return jsonify({"ok": True}) if delete_preset(name) else (jsonify({"error":"not found"}), 404)

@app.route("/api/scan", methods=["POST"])
def api_scan():
    params = request.json
    host = params.get("host", "127.0.0.1")
    port = params.get("port", 5020)
    unit = params.get("unit", 1)
    start = params.get("start", 0)
    end = params.get("end", 100)
    reg_type = params.get("register_type", "input")

    client = get_client(host, port)
    if not client:
        return jsonify({"error": f"Brak połączenia"}), 502

    try:
        count = min(end - start, 125)  # Modbus max 125 rejestrów na raz
        all_regs = []

        # Czytaj w blokach po 125 rejestrów
        addr = start
        while addr < end:
            chunk = min(125, end - addr)
            regs = read_registers(client, addr, chunk, unit, reg_type)
            if regs is None:
                break
            all_regs.extend(regs)
            addr += chunk

        if not all_regs:
            return jsonify({"error": "Brak danych z urządzenia"}), 502

        results = []
        for i in range(0, len(all_regs) - 1, 2):
            a = start + i
            r0, r1 = all_regs[i], all_regs[i+1]
            decoded = {}
            best = None
            for order, fn in DECODERS.items():
                try:
                    v = fn(r0, r1)
                    decoded[order] = round(v, 4) if not (math.isnan(v) or math.isinf(v)) else None
                    if decoded[order] is not None and not best:
                        g = guess_value(v)
                        if g: best = {**g, "byte_order": order, "value": round(v, 4)}
                except: decoded[order] = None

            results.append({
                "address": a, "raw": [r0, r1],
                "raw_hex": f"0x{r0:04X} 0x{r1:04X}",
                "decoded": decoded, "hint": best,
            })

        return jsonify({"registers": results, "count": len(results)})
    finally:
        client.close()

@app.route("/api/live", methods=["POST"])
def api_live():
    params = request.json
    host = params.get("host", "127.0.0.1")
    port = params.get("port", 5020)
    unit = params.get("unit", 1)
    preset_name = params.get("preset")

    preset = get_preset(preset_name) if preset_name else None
    if not preset:
        return jsonify({"error": "Preset nie znaleziony"}), 404

    byte_order = preset.get("byte_order", "big_endian")
    reg_type = preset.get("register_type", "input")
    decode_fn = DECODERS.get(byte_order, regs_to_float_be)
    registers = preset.get("registers", {})
    if not registers:
        return jsonify({"error": "Preset pusty"}), 400

    addrs = [r["address"] for r in registers.values()]
    min_addr = min(addrs)
    max_addr = max(addrs) + 2
    count = max_addr - min_addr

    client = get_client(host, port)
    if not client:
        return jsonify({"error": "Brak połączenia"}), 502

    try:
        regs = read_registers(client, min_addr, count, unit, reg_type)
        if regs is None:
            return jsonify({"error": "Błąd odczytu"}), 502

        values = {}
        for key, spec in registers.items():
            idx = spec["address"] - min_addr
            v = None
            if 0 <= idx and idx + 1 < len(regs):
                try:
                    v = decode_fn(regs[idx], regs[idx+1])
                    if math.isnan(v) or math.isinf(v): v = None
                    else: v = round(v, spec.get("decimals", 2))
                except: v = None
            values[key] = {
                "value": v, "unit": spec.get("unit",""),
                "label": spec.get("label", key),
                "group": spec.get("group","other"),
                "decimals": spec.get("decimals", 2),
            }
        return jsonify({"values": values, "timestamp": time.time()})
    finally:
        client.close()

@app.route("/api/ping", methods=["POST"])
def api_ping():
    params = request.json
    c = get_client(params.get("host"), params.get("port"))
    if c: c.close(); return jsonify({"ok": True})
    return jsonify({"ok": False}), 502

# ── Main ───────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Modbus Meter Dashboard")
    parser.add_argument("--port", type=int, default=5000, help="Port HTTP (def: 5000)")
    parser.add_argument("--modbus-port", type=int, default=5020, help="Port symulatora TCP (def: 5020)")
    parser.add_argument("--no-sim", action="store_true", help="Nie uruchamiaj symulatora")
    # RPi RS-485 options
    parser.add_argument("--serial", type=str, default=None, help="Port szeregowy RS-485 (np. /dev/ttyS0)")
    parser.add_argument("--baudrate", type=int, default=9600, help="Baudrate (def: 9600)")
    parser.add_argument("--parity", type=str, default="N", choices=["N","E","O"], help="Parity (def: N)")
    parser.add_argument("--stopbits", type=int, default=1, choices=[1,2], help="Stop bits (def: 1)")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    if args.serial:
        SERIAL_CFG = {
            "port": args.serial,
            "baudrate": args.baudrate,
            "parity": args.parity,
            "stopbits": args.stopbits,
            "bytesize": 8,
        }
        log.info(f"Tryb RS-485: {args.serial} @ {args.baudrate} baud")
        args.no_sim = True  # nie uruchamiaj symulatora w trybie serial

    if not args.no_sim:
        start_simulator(args.modbus_port)
        log.info(f"Symulator TCP na porcie {args.modbus_port}")

    log.info(f"Dashboard: http://localhost:{args.port}")
    app.run(host="0.0.0.0", port=args.port, debug=args.debug, use_reloader=False)