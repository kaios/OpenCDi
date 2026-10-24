"""MK48T08 timekeeper: 8 KiB battery backed RAM with a clock in the top bytes.

Mapped at 0x320000-0x323fff on the upper data byte only (even addresses).
The clock follows host time; values written by software are kept as an offset.
"""

import datetime
from pathlib import Path

from .bus import Device

SIZE = 0x2000
CTRL, SECS, MINS, HOURS, DAY, DATE, MONTH, YEAR = range(0x1ff8, 0x2000)
CONTROL_W, CONTROL_R = 0x80, 0x40


def bcd(v):
    return (v // 10) << 4 | v % 10


def unbcd(v):
    return (v >> 4) * 10 + (v & 0xf)


class MK48T08(Device):
    def __init__(self, path=None):
        self.path = Path(path) if path else None
        self.data = bytearray(b'\xff' * SIZE)
        if self.path and self.path.exists():
            raw = self.path.read_bytes()
            self.data[:len(raw[:SIZE])] = raw[:SIZE]
        self.offset = datetime.timedelta(0)
        self.data[CTRL] = 0
        self.data[0x1ff0] = 0  # flags: battery fine
        self.counters_to_ram()

    def now(self):
        return datetime.datetime.now() + self.offset

    def counters_to_ram(self):
        t = self.now()
        d = self.data
        d[SECS] = bcd(t.second)
        d[MINS] = bcd(t.minute)
        d[HOURS] = bcd(t.hour)
        d[DAY] = t.isoweekday() % 7 + 1
        d[DATE] = bcd(t.day)
        d[MONTH] = bcd(t.month)
        d[YEAR] = bcd(t.year % 100)

    def counters_from_ram(self):
        d = self.data
        try:
            year = 1900 + unbcd(d[YEAR])
            if year < 1970:
                year += 100
            t = datetime.datetime(year, unbcd(d[MONTH] & 0x1f), unbcd(d[DATE] & 0x3f),
                                  unbcd(d[HOURS] & 0x3f), unbcd(d[MINS] & 0x7f), unbcd(d[SECS] & 0x7f))
            self.offset = t - datetime.datetime.now()
        except ValueError:
            pass

    def _index(self, a):
        return ((a - 0x320000) >> 1) & (SIZE - 1)

    def read8(self, a):
        if a & 1:
            return 0
        i = self._index(a)
        if SECS <= i <= YEAR and not self.data[CTRL] & (CONTROL_R | CONTROL_W):
            self.counters_to_ram()
        return self.data[i]

    def read16(self, a):
        return self.read8(a) << 8

    def write8(self, a, v):
        if a & 1:
            return
        i = self._index(a)
        if i == CTRL:
            old = self.data[CTRL]
            if old & CONTROL_W and not v & CONTROL_W:
                self.counters_from_ram()
            self.data[CTRL] = v
            return
        self.data[i] = v

    def write16(self, a, v):
        self.write8(a, v >> 8)

    def save(self):
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_bytes(bytes(self.data))
