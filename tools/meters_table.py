#!/usr/bin/env python3
"""Generuje tabelę obsługiwanych liczników (Markdown) z biblioteki presetów.

Użycie:  python tools/meters_table.py            # wypisuje tabelę
         python tools/meters_table.py --readme   # podmienia tabelę w README.md
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from modbus_dash.codec import normalize_byte_order, normalize_data_type  # noqa: E402

LIB = ROOT / "presets" / "library"
START, END = "<!-- METERS_TABLE_START -->", "<!-- METERS_TABLE_END -->"
FC = {"input": "Input (FC04)", "holding": "Holding (FC03)"}


def table():
    rows = []
    for f in sorted(LIB.glob("*.json")):
        p = json.loads(f.read_text(encoding="utf-8"))
        if f.stem == "simulator_3f":
            continue
        regs = p.get("registers", {})
        funcs = {r.get("register_type", p.get("register_type", "input")) for r in regs.values()}
        types = sorted({normalize_data_type(r.get("type", p.get("data_type"))) for r in regs.values()})
        orders = sorted({normalize_byte_order(r.get("byte_order", p.get("byte_order"))) for r in regs.values()})
        serial = p.get("serial") or {}
        ser = (f"{serial.get('baudrate', '?')} 8{serial.get('parity', '?')}{serial.get('stopbits', '?')}"
               if serial else "-")
        rows.append((p.get("manufacturer", ""), p.get("model") or p.get("name", f.stem), p.get("phases", ""),
                     " + ".join(FC.get(x, x) for x in sorted(funcs)), ", ".join(types),
                     ", ".join(str(o) for o in orders), ser, len(regs), f.stem))
    out = ["| Producent | Model | Fazy | Rejestry | Typy danych | Kolejność | Port (fabr.) | Wielkości | Preset |",
           "|---|---|---|---|---|---|---|---|---|"]
    for r in sorted(rows, key=lambda r: (r[0].lower(), r[1].lower())):
        out.append("| " + " | ".join(str(x) for x in r[:8]) + f" | `{r[8]}` |")
    return "\n".join(out)


def main():
    t = table()
    if "--readme" in sys.argv:
        readme = ROOT / "README.md"
        text = readme.read_text(encoding="utf-8")
        a, b = text.index(START) + len(START), text.index(END)
        readme.write_text(text[:a] + "\n" + t + "\n" + text[b:], encoding="utf-8")
    else:
        print(t)


if __name__ == "__main__":
    main()
