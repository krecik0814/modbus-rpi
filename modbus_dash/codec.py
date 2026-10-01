"""Kodowanie i dekodowanie wartości zapisanych w rejestrach Modbus.

Rejestr Modbus ma 16 bitów. Wartości 32- i 64-bitowe zajmują 2 lub 4 kolejne
rejestry, a producenci różnie układają w nich bajty. Kolejność opisujemy
literami bajtów wartości zapisanej big-endian (A = najstarszy bajt):

    ABCD  big-endian (najczęstsze: Eastron, Orno, Finder, Schneider, Janitza)
    CDAB  word swap - młodsze słowo pierwsze (Carlo Gavazzi, część Schneider)
    BADC  byte swap - zamienione bajty w każdym słowie
    DCBA  little-endian - całkowicie odwrócone

Dla typów 64-bitowych ta sama zasada: ABCD = słowa od najstarszego,
CDAB = słowa od najmłodszego, BADC/DCBA = dodatkowo zamienione bajty w słowie.
Dla typów 16-bitowych liczy się tylko zamiana bajtów (BADC/DCBA).
"""

import math
import struct

# typ -> (liczba rejestrów, format struct big-endian)
DATA_TYPES = {
    "int16": (1, ">h"),
    "uint16": (1, ">H"),
    "int32": (2, ">i"),
    "uint32": (2, ">I"),
    "float32": (2, ">f"),
    "int64": (4, ">q"),
    "uint64": (4, ">Q"),
    "float64": (4, ">d"),
}

BYTE_ORDERS = ("ABCD", "CDAB", "BADC", "DCBA")

_ORDER_ALIASES = {
    "abcd": "ABCD", "big_endian": "ABCD", "big": "ABCD", "be": "ABCD",
    "cdab": "CDAB", "word_swap": "CDAB", "wordswap": "CDAB", "ws": "CDAB",
    "lsw_first": "CDAB", "swapped": "CDAB",
    "badc": "BADC", "byte_swap": "BADC", "byteswap": "BADC", "bs": "BADC",
    "dcba": "DCBA", "little_endian": "DCBA", "little": "DCBA", "le": "DCBA",
}

_TYPE_ALIASES = {
    "float": "float32", "real": "float32", "f32": "float32", "ieee754": "float32",
    "double": "float64", "f64": "float64",
    "int": "int16", "short": "int16", "i16": "int16", "s16": "int16",
    "uint": "uint16", "word": "uint16", "u16": "uint16",
    "dint": "int32", "long": "int32", "i32": "int32", "s32": "int32",
    "udint": "uint32", "dword": "uint32", "ulong": "uint32", "u32": "uint32",
    "lint": "int64", "i64": "int64", "s64": "int64",
    "ulint": "uint64", "u64": "uint64",
}

# Etykiety do UI (kolumny skanera, CSV)
ORDER_LABELS = {
    "ABCD": "Big-endian (AB CD)",
    "CDAB": "Word swap (CD AB)",
    "BADC": "Byte swap (BA DC)",
    "DCBA": "Little-endian (DC BA)",
}


def normalize_byte_order(order):
    """Zwraca kanoniczną nazwę kolejności (ABCD/CDAB/BADC/DCBA) albo rzuca ValueError."""
    if order is None:
        return "ABCD"
    key = str(order).strip()
    if key.upper() in BYTE_ORDERS:
        return key.upper()
    try:
        return _ORDER_ALIASES[key.lower()]
    except KeyError:
        raise ValueError(f"nieznana kolejność bajtów: {order!r}") from None


def normalize_data_type(dtype):
    """Zwraca kanoniczną nazwę typu (np. 'float32') albo rzuca ValueError."""
    if dtype is None:
        return "float32"
    key = str(dtype).strip().lower()
    if key in DATA_TYPES:
        return key
    try:
        return _TYPE_ALIASES[key]
    except KeyError:
        raise ValueError(f"nieznany typ danych: {dtype!r}") from None


def register_count(dtype):
    return DATA_TYPES[normalize_data_type(dtype)][0]


def _flags(order):
    order = normalize_byte_order(order)
    return order in ("CDAB", "DCBA"), order in ("BADC", "DCBA")


def _swap16(v):
    return ((v & 0xFF) << 8) | ((v >> 8) & 0xFF)


def regs_to_bytes(regs, order="ABCD"):
    """Układa rejestry w bajty big-endian (ABCD) zgodnie z podaną kolejnością."""
    word_swap, byte_swap = _flags(order)
    words = [int(r) & 0xFFFF for r in regs]
    if byte_swap:
        words = [_swap16(w) for w in words]
    if word_swap:
        words.reverse()
    return struct.pack(f">{len(words)}H", *words)


def bytes_to_regs(data, order="ABCD"):
    """Odwrotność regs_to_bytes: bajty big-endian -> rejestry w kolejności urządzenia."""
    word_swap, byte_swap = _flags(order)
    words = list(struct.unpack(f">{len(data) // 2}H", data))
    if word_swap:
        words.reverse()
    if byte_swap:
        words = [_swap16(w) for w in words]
    return words


def decode(regs, dtype="float32", order="ABCD"):
    """Dekoduje surowe rejestry do liczby (int albo float).

    Dla 16-bitowych typów word swap nie ma znaczenia.
    """
    dtype = normalize_data_type(dtype)
    count, fmt = DATA_TYPES[dtype]
    if len(regs) < count:
        raise ValueError(f"{dtype} wymaga {count} rejestrów, podano {len(regs)}")
    return struct.unpack(fmt, regs_to_bytes(regs[:count], order))[0]


def encode(value, dtype="float32", order="ABCD"):
    """Koduje liczbę do listy rejestrów (używane przez symulator i testy).

    Liczby całkowite są zaokrąglane i przycinane do zakresu typu.
    """
    dtype = normalize_data_type(dtype)
    count, fmt = DATA_TYPES[dtype]
    if dtype.startswith("float"):
        value = float(value)
    else:
        bits = count * 16
        if math.isnan(value) or math.isinf(value):
            value = 0
        value = int(round(value))
        if dtype.startswith("u"):
            lo, hi = 0, (1 << bits) - 1
        else:
            lo, hi = -(1 << (bits - 1)), (1 << (bits - 1)) - 1
        value = max(lo, min(hi, value))
    return bytes_to_regs(struct.pack(fmt, value), order)


def is_finite(v):
    return v is not None and not (isinstance(v, float) and (math.isnan(v) or math.isinf(v)))


def scaled(raw, scale=1.0, offset=0.0):
    """Wartość fizyczna = surowa * scale + offset. Zwraca None dla NaN/inf."""
    if not is_finite(raw):
        return None
    v = raw * scale + offset if (scale != 1 or offset) else raw
    return v if is_finite(v) else None


def unscaled(value, scale=1.0, offset=0.0):
    """Odwrotność scaled() - używana przez symulator do zapisu wartości fizycznej."""
    if not scale:
        return 0
    return (value - offset) / scale
