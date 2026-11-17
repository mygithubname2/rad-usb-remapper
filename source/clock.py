# clock.py - now_ms(), the firmware's millisecond clock, in a module of its own so that every
# part (the display first of all, before the bigger modules load) shares one copy.

import time

try:
    from supervisor import ticks_ms as _ticks          # small whole numbers: no memory per call
except ImportError:                                    # tests on a computer
    _ticks = None
_TICKS_WRAP = 1 << 29
_clock = [0, 0, None]        # [wraps added, last ticks, ticks at the first call]


def now_ms():
    """Milliseconds since the firmware started (from 10 000).

    supervisor.ticks_ms() wraps every 2**29 ms (6.2 days); the wraps are added here. It stays
    a small number, which CircuitPython keeps without allocating memory, for the first 12 days.
    (time.monotonic_ns() made a big-number object on every call: at up to 1000 mouse reports a
    second that garbage caused frequent memory clean-ups, and jerky mouse movement.)"""
    if _ticks is None:
        return time.monotonic_ns() // 1000000
    t = _ticks()
    c = _clock
    if c[2] is None:
        c[2] = t
    if t < c[1]:
        c[0] += _TICKS_WRAP
    c[1] = t
    return c[0] + t - c[2] + 10000
