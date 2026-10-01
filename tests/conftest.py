import contextlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class FakeModbusError(Exception):
    def __init__(self, kind, code=None, msg=None):
        super().__init__(msg or kind)
        self.kind = kind
        self.code = code


class FakeBus:
    """Magistrala w pamięci: image[(function, address)] = uint16.

    strict=True: odczyt obejmujący niezmapowany adres -> wyjątek 02 (jak wiele liczników).
    """

    def __init__(self, image=None, strict=True, max_count=125, silent_units=()):
        self.image = dict(image or {})
        self.strict = strict
        self.max_count = max_count
        self.silent_units = set(silent_units)
        self.calls = []

    def read_registers(self, unit, function, address, count):
        self.calls.append((unit, function, address, count))
        if unit in self.silent_units:
            raise FakeModbusError("timeout", msg="Brak odpowiedzi (timeout)")
        if count > self.max_count:
            raise FakeModbusError("exception", 3, "Wyjątek Modbus 03")
        out = []
        for a in range(address, address + count):
            if (function, a) not in self.image:
                if self.strict:
                    raise FakeModbusError("exception", 2, "Wyjątek Modbus 02")
                out.append(0)
            else:
                out.append(self.image[(function, a)])
        return out

    def read_bits(self, unit, function, address, count):
        self.calls.append((unit, function, address, count))
        return [bool(self.image.get((function, a), 0)) for a in range(address, address + count)]

    def put(self, function, address, regs):
        for i, r in enumerate(regs):
            self.image[(function, address + i)] = r

    @contextlib.contextmanager
    def override(self, timeout=None, retries=None):
        yield self


@pytest.fixture
def fake_bus():
    return FakeBus()
