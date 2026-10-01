"""Historia długoterminowa w SQLite (biblioteka standardowa).

Próbki z pollera są agregowane w pamięci do przedziałów (domyślnie 60 s) i
zapisywane jako średnia/min/maks - jeden zapis na przedział oszczędza kartę SD.
"""

import logging
import sqlite3
import threading
import time

log = logging.getLogger("modbus-dash.history")

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    device TEXT NOT NULL,
    key    TEXT NOT NULL,
    ts     INTEGER NOT NULL,
    avg    REAL, min REAL, max REAL, last REAL,
    n      INTEGER NOT NULL,
    PRIMARY KEY (device, key, ts)
) WITHOUT ROWID;
"""


class HistoryDB:
    def __init__(self, path, bucket_seconds=60, retention_days=30):
        self.path = str(path)
        self.bucket = int(bucket_seconds)
        self.retention_days = int(retention_days)
        self._lock = threading.Lock()
        self._acc = {}          # (device, key) -> [bucket_ts, sum, min, max, last, n]
        self._last_cleanup = 0.0
        self._db = sqlite3.connect(self.path, check_same_thread=False, timeout=10)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(SCHEMA)

    def configure(self, bucket_seconds=None, retention_days=None):
        with self._lock:
            if bucket_seconds and int(bucket_seconds) != self.bucket:
                self._flush_all()
                self.bucket = int(bucket_seconds)
            if retention_days:
                self.retention_days = int(retention_days)

    # ── zapis ──────────────────────────────────────────────────
    def on_sample(self, device_id, runtime, sample):
        """Listener pollera."""
        if not sample.get("ok"):
            return
        ts = sample["ts"]
        rows = []
        with self._lock:
            bucket_ts = int(ts // self.bucket * self.bucket)
            for key, v in sample["values"].items():
                if v is None:
                    continue
                acc = self._acc.get((device_id, key))
                if acc and acc[0] != bucket_ts:
                    rows.append(self._row(device_id, key, acc))
                    acc = None
                if acc is None:
                    self._acc[(device_id, key)] = [bucket_ts, v, v, v, v, 1]
                else:
                    acc[1] += v
                    acc[2] = min(acc[2], v)
                    acc[3] = max(acc[3], v)
                    acc[4] = v
                    acc[5] += 1
            if rows:
                self._write(rows)
            if time.time() - self._last_cleanup > 3600:
                self._cleanup()

    @staticmethod
    def _row(device_id, key, acc):
        b, s, mn, mx, last, n = acc
        return (device_id, key, b, s / n, mn, mx, last, n)

    def _write(self, rows):
        try:
            with self._db:
                self._db.executemany(
                    "INSERT OR REPLACE INTO samples(device,key,ts,avg,min,max,last,n) VALUES (?,?,?,?,?,?,?,?)",
                    rows)
        except sqlite3.Error as e:
            log.warning("Zapis historii: %s", e)

    def _flush_all(self):
        rows = [self._row(d, k, acc) for (d, k), acc in self._acc.items()]
        self._acc.clear()
        if rows:
            self._write(rows)

    def _cleanup(self):
        self._last_cleanup = time.time()
        cutoff = int(time.time() - self.retention_days * 86400)
        try:
            with self._db:
                self._db.execute("DELETE FROM samples WHERE ts < ?", (cutoff,))
        except sqlite3.Error as e:
            log.warning("Czyszczenie historii: %s", e)

    def flush(self):
        with self._lock:
            self._flush_all()

    def close(self):
        with self._lock:
            self._flush_all()
            self._db.close()

    def delete_device(self, device_id):
        with self._lock:
            for k in [k for k in self._acc if k[0] == device_id]:
                del self._acc[k]
            with self._db:
                self._db.execute("DELETE FROM samples WHERE device = ?", (device_id,))

    # ── odczyt ─────────────────────────────────────────────────
    def query(self, device_id, keys, since, until=None, max_points=1000):
        """Zwraca {"keys", "points": [[ts, v1, ...]], "bucket"}; przy dużej liczbie punktów
        łączy przedziały (średnia ważona liczbą próbek)."""
        until = until or time.time()
        span = max(1.0, until - since)
        step = self.bucket
        while span / step > max_points:
            step *= 2
        with self._lock:
            self._flush_partial(device_id)
            cur = self._db.execute(
                f"SELECT key, (ts / {step}) * {step} AS b, SUM(avg * n) / SUM(n) "
                f"FROM samples WHERE device = ? AND ts >= ? AND ts <= ? "
                f"GROUP BY key, b ORDER BY b",
                (device_id, int(since), int(until)))
            data = cur.fetchall()
        want = list(keys) if keys else sorted({r[0] for r in data})
        index = {k: i for i, k in enumerate(want)}
        points = {}
        for key, b, avg in data:
            if key not in index:
                continue
            row = points.setdefault(b, [b] + [None] * len(want))
            row[1 + index[key]] = avg
        return {"keys": want, "points": [points[b] for b in sorted(points)], "bucket": step}

    def _flush_partial(self, device_id):
        """Zapisuje bieżący (niepełny) przedział, aby zapytanie widziało świeże dane."""
        rows = [self._row(d, k, acc) for (d, k), acc in self._acc.items() if d == device_id]
        if rows:
            self._write(rows)
