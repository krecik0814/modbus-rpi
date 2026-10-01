"""Planowanie odczytów: grupowanie rejestrów presetu w bloki.

Modbus pozwala odczytać maksymalnie 125 rejestrów jednym zapytaniem, a wiele
liczników odrzuca zapytania obejmujące nieużywane adresy (wyjątek 02 Illegal
Data Address). Planer łączy rejestry w bloki nie dłuższe niż max_block, z
przerwami nie większymi niż max_gap. Gdy licznik odrzuci blok, PresetReader
dzieli go na mniejsze i zapamiętuje podział, o ile pomógł (część odczytała się).
Wyjątki "zajęte"/"operacja w toku" (05/06) nie dzielą bloków, a co REPLAN_SECONDS
plan jest układany od nowa - jeden zły odczyt nie psuje planu na zawsze.
"""

import time
from dataclasses import dataclass, field

from . import codec
from .presets import MODBUS_MAX_REGS

REPLAN_SECONDS = 3600.0
_TRANSIENT_CODES = (0x05, 0x06)  # potwierdzenie / urządzenie zajęte - spróbuj później


@dataclass
class ReadBlock:
    function: str          # "input" | "holding"
    start: int
    count: int
    keys: list = field(default_factory=list)
    rejected: bool = False  # podział nic nie dał - czytamy w całości (do ponownego planowania)

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


def decode_value(spec, regs, factor=1.0, rounded=True):
    """Surowe rejestry -> wartość fizyczna (zaokrąglona do "decimals") albo None.

    factor: dodatkowy mnożnik ze "scale_from" (np. 10^wykładnik z innego rejestru).
    rounded=False: wartość dokładna (np. mnożnik dla innych rejestrów).
    """
    raw = codec.decode(regs, spec["type"], spec["order"])
    if spec.get("invalid") and float(raw) in spec["invalid"]:
        return None
    v = codec.scaled(raw, spec["scale"] * factor, spec["offset"])
    if v is None:
        return None
    return round(float(v), spec["decimals"]) if rounded else float(v)


def scale_factor(spec, source_value):
    """Mnożnik dla rejestru ze "scale_from" albo None, gdy rejestr skali nie ma wartości."""
    if source_value is None:
        return None
    if spec.get("scale_mode") == "multiply":
        return float(source_value)
    try:
        return 10.0 ** source_value
    except OverflowError:
        return None


class PresetReader:
    """Czyta wartości presetu z magistrali, adaptując podział bloków.

    bus musi mieć metodę read_registers(unit, function, address, count) -> list[int],
    rzucającą ModbusError (atrybut .kind == "exception" dla odpowiedzi wyjątku).
    """

    def __init__(self, preset):
        self.preset = preset
        self.registers = preset["registers"]
        self._sources = {s["scale_from"] for s in self.registers.values() if s.get("scale_from")}
        self._replan()

    def _replan(self):
        self.blocks = plan_reads(self.registers, self.preset.get("max_block", 64),
                                 self.preset.get("max_gap", 10))
        self._planned = time.monotonic()

    def read(self, bus, unit):
        """Zwraca dict: values {key: float|None}, errors {key: str}, duration_ms, requests."""
        t0 = time.monotonic()
        if t0 - self._planned > REPLAN_SECONDS:
            self._replan()
        st = _ReadState({k: None for k in self.registers})
        new_blocks = []
        for block in self.blocks:
            new_blocks += self._read_block(bus, unit, block, st)[0]
        for k, chunk in st.raw_regs.items():
            spec = self.registers[k]
            # mnożnik z wartości dokładnej, nie zaokrąglonej do wyświetlania
            factor = scale_factor(spec, st.exact.get(spec["scale_from"]))
            if factor is None:
                st.errors[k] = f"brak wartości rejestru skali '{spec['scale_from']}'"
                continue
            try:
                st.values[k] = decode_value(spec, chunk, factor)
            except (ValueError, OverflowError) as e:
                st.errors[k] = str(e)
        self.blocks = sorted(new_blocks, key=lambda b: (b.function, b.start))
        return {
            "values": st.values,
            "errors": st.errors,
            "duration_ms": round((time.monotonic() - t0) * 1000, 1),
            "requests": st.requests,
            "ok": st.fatal is None and len(st.errors) < len(self.registers),
        }

    def _read_block(self, bus, unit, block, st):
        """Czyta blok, dzieląc go po odrzuceniu. Zwraca (bloki do planu, czy coś się odczytało)."""
        if st.fatal is not None:
            for k in block.keys:
                st.errors[k] = st.fatal
            return [block], False
        st.requests += 1
        try:
            regs = bus.read_registers(unit, block.function, block.start, block.count)
        except Exception as e:  # noqa: BLE001 - ModbusError i błędy transportu
            kind = getattr(e, "kind", "io")
            code = getattr(e, "code", None)
            if kind == "exception" and code not in _TRANSIENT_CODES and len(block.keys) > 1 \
                    and not block.rejected:
                # licznik odrzucił zakres - podziel; podział zostaje w planie tylko, gdy pomógł
                parts, ok = [], False
                for part in split_block(block, self.registers):
                    got, part_ok = self._read_block(bus, unit, part, st)
                    parts += got
                    ok = ok or part_ok
                if ok:
                    return parts, True
                block.rejected = True
                return [block], False
            msg = str(e) or kind
            for k in block.keys:
                st.errors[k] = msg
            if kind in ("connection", "timeout"):
                st.fatal = msg  # bez sensu odpytywać dalej niedostępne urządzenie
            return [block], False
        block.rejected = False
        for k in block.keys:
            spec = self.registers[k]
            i = spec["address"] - block.start
            chunk = regs[i:i + spec["count"]]
            if len(chunk) < spec["count"]:
                st.errors[k] = "za krótka odpowiedź"
                continue
            if spec.get("scale_from"):
                st.raw_regs[k] = chunk
                continue
            try:
                st.values[k] = decode_value(spec, chunk)
                if k in self._sources:
                    st.exact[k] = decode_value(spec, chunk, rounded=False)
            except (ValueError, OverflowError) as e:
                st.errors[k] = str(e)
        return [block], True


class _ReadState:
    """Stan jednego odczytu presetu."""

    def __init__(self, values):
        self.values = values
        self.errors = {}
        self.raw_regs = {}      # rejestry ze "scale_from" - dekodowane po rejestrach skali
        self.exact = {}         # niezaokrąglone wartości rejestrów skali
        self.requests = 0
        self.fatal = None
