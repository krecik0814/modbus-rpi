"""Planowanie odczytów: grupowanie rejestrów presetu w bloki.

Modbus pozwala odczytać maksymalnie 125 rejestrów jednym zapytaniem, a wiele
liczników odrzuca zapytania obejmujące nieużywane adresy (wyjątek 02 Illegal
Data Address). Planer łączy rejestry w bloki nie dłuższe niż max_block, z
przerwami nie większymi niż max_gap. Gdy licznik odrzuci blok, PresetReader
dzieli go na mniejsze i zapamiętuje podział.
"""

import time
from dataclasses import dataclass, field

from . import codec
from .presets import MODBUS_MAX_REGS


@dataclass
class ReadBlock:
    function: str          # "input" | "holding"
    start: int
    count: int
    keys: list = field(default_factory=list)

    @property
    def end(self):
        return self.start + self.count


def plan_reads(registers, max_block=64, max_gap=10):
    """registers: {key: spec znormalizowany} -> lista ReadBlock."""
    max_block = max(1, min(int(max_block), MODBUS_MAX_REGS))
    blocks = []
    by_func = {}
    for spec in registers.values():
        by_func.setdefault(spec["function"], []).append(spec)
    for func in sorted(by_func):
        specs = sorted(by_func[func], key=lambda s: (s["address"], -s["count"]))
        cur = None
        for s in specs:
            s_end = s["address"] + s["count"]
            if cur is not None:
                new_end = max(cur.end, s_end)
                gap = s["address"] - cur.end
                if gap <= max_gap and new_end - cur.start <= max_block:
                    cur.count = new_end - cur.start
                    cur.keys.append(s["key"])
                    continue
            cur = ReadBlock(func, s["address"], s["count"], [s["key"]])
            blocks.append(cur)
    return blocks


def split_block(block, registers):
    """Dzieli blok na dwa (po granicy rejestrów). Zwraca listę 1 lub 2 bloków."""
    if len(block.keys) <= 1:
        return [block]
    keys = sorted(block.keys, key=lambda k: registers[k]["address"])
    mid = len(keys) // 2
    out = []
    for part in (keys[:mid], keys[mid:]):
        start = min(registers[k]["address"] for k in part)
        end = max(registers[k]["address"] + registers[k]["count"] for k in part)
        out.append(ReadBlock(block.function, start, end - start, list(part)))
    return out


def decode_value(spec, regs):
    """Surowe rejestry -> wartość fizyczna (zaokrąglona) albo None."""
    raw = codec.decode(regs, spec["type"], spec["order"])
    if spec.get("invalid") and raw in spec["invalid"]:
        return None
    v = codec.scaled(raw, spec["scale"], spec["offset"])
    if v is None:
        return None
    return round(float(v), spec["decimals"])


class PresetReader:
    """Czyta wartości presetu z magistrali, adaptując podział bloków.

    bus musi mieć metodę read_registers(unit, function, address, count) -> list[int],
    rzucającą ModbusError (atrybut .kind == "exception" dla odpowiedzi wyjątku).
    """

    def __init__(self, preset):
        self.preset = preset
        self.registers = preset["registers"]
        self.blocks = plan_reads(self.registers, preset.get("max_block", 64),
                                 preset.get("max_gap", 10))

    def read(self, bus, unit):
        """Zwraca dict: values {key: float|None}, errors {key: str}, duration_ms, requests."""
        t0 = time.monotonic()
        values = {k: None for k in self.registers}
        errors = {}
        requests = 0
        new_blocks = []
        fatal = None
        queue = list(self.blocks)
        while queue:
            block = queue.pop(0)
            if fatal is not None:
                for k in block.keys:
                    errors[k] = fatal
                new_blocks.append(block)
                continue
            requests += 1
            try:
                regs = bus.read_registers(unit, block.function, block.start, block.count)
            except Exception as e:  # noqa: BLE001 - ModbusError i błędy transportu
                kind = getattr(e, "kind", "io")
                if kind == "exception" and len(block.keys) > 1:
                    # licznik odrzucił zakres - podziel i spróbuj ponownie
                    queue[0:0] = split_block(block, self.registers)
                    continue
                msg = str(e) or kind
                for k in block.keys:
                    errors[k] = msg
                new_blocks.append(block)
                if kind in ("connection", "timeout"):
                    # bez sensu odpytywać dalej niedostępne urządzenie
                    fatal = msg
                continue
            new_blocks.append(block)
            for k in block.keys:
                spec = self.registers[k]
                i = spec["address"] - block.start
                chunk = regs[i:i + spec["count"]]
                if len(chunk) < spec["count"]:
                    errors[k] = "za krótka odpowiedź"
                    continue
                try:
                    values[k] = decode_value(spec, chunk)
                except (ValueError, OverflowError) as e:
                    errors[k] = str(e)
        self.blocks = sorted(new_blocks, key=lambda b: (b.function, b.start))
        return {
            "values": values,
            "errors": errors,
            "duration_ms": round((time.monotonic() - t0) * 1000, 1),
            "requests": requests,
            "ok": fatal is None and len(errors) < len(self.registers),
        }
