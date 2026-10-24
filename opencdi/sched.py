"""Cycle-based event scheduler.  Time is counted in CPU cycles."""

import heapq


class Event:
    __slots__ = ('time', 'seq', 'func', 'cancelled')

    def __init__(self, time, seq, func):
        self.time, self.seq, self.func, self.cancelled = time, seq, func, False

    def __lt__(self, other):
        return (self.time, self.seq) < (other.time, other.seq)

    def cancel(self):
        self.cancelled = True


class Scheduler:
    def __init__(self, clock_hz):
        self.clock = clock_hz
        self.now = 0
        self.q = []
        self.seq = 0

    def at(self, time, func):
        self.seq += 1
        ev = Event(int(time), self.seq, func)
        heapq.heappush(self.q, ev)
        return ev

    def after(self, cycles, func):
        return self.at(self.now + max(1, int(cycles)), func)

    def after_seconds(self, secs, func):
        return self.after(secs * self.clock, func)

    def next_time(self):
        q = self.q
        while q and q[0].cancelled:
            heapq.heappop(q)
        return q[0].time if q else None

    def run_due(self):
        q = self.q
        while q and q[0].time <= self.now:
            ev = heapq.heappop(q)
            if not ev.cancelled:
                ev.func()

    def clear(self):
        self.q.clear()
