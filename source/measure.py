# measure.py - "Measure smoothness": where the main loop's time goes, for a few seconds.
#
# Loaded only when the page asks for a measurement (cmd "measure"), and dropped afterwards.
# The main loop calls loop() at the top of every pass and mark(i) after each of its parts; the
# result is one log line on the page. Times come from a millisecond clock: a part that takes
# 0.3 ms is counted as 1 ms about 3 times in 10, so the totals over thousands of passes are right
# even though one pass can't be timed finer than 1 ms.
import gc
from hostinput import now_ms

MEM_EVERY_MS = 250            # free memory looked at 4 times a second (looking costs a little)


class Meter:
    def __init__(self, names, seconds, host, out):
        self.names = names
        self.host = host
        self.out = out
        self.sends0 = out.sends
        self.waits0 = out.waits
        self.eps = [(ep, ep.reads, ep.got, ep.ms) for ep in host.endpoints]
        host.timing = True
        n = len(names)
        self.sums = [0] * n
        self.peaks = [0] * n
        now = now_ms()
        self.start = now
        self.end = now + seconds * 1000
        self.loop_t = 0
        self.last = now
        self.loops = 0
        self.gap_max = 0
        self.slow = 0                 # passes that took 5 ms or more (a 1000 Hz mouse sends 5+ reports meanwhile)
        self.reports0 = host.report_count
        self.mem_at = now
        self.free = gc.mem_free()
        self.used = 0                 # bytes taken between looks
        self.cleanups = 0             # looks where free memory went up: a clean-up ran

    def loop(self):
        """Top of a main-loop pass. True when the measurement is over."""
        t = now_ms()
        if self.loop_t:
            gap = t - self.loop_t
            if gap > self.gap_max:
                self.gap_max = gap
            if gap >= 5:
                self.slow += 1
        self.loops += 1
        self.loop_t = t
        if t >= self.mem_at:
            self.mem_at = t + MEM_EVERY_MS
            free = gc.mem_free()
            if free > self.free:
                self.cleanups += 1
            else:
                self.used += self.free - free
            self.free = free
        self.last = now_ms()          # the look at memory above is not counted in any part
        return t >= self.end

    def mark(self, i):
        t = now_ms()
        d = t - self.last
        self.sums[i] += d
        if d > self.peaks[i]:
            self.peaks[i] = d
        self.last = t

    def report(self):
        """The result, as a few short lines (memory may be short: no one big string)."""
        self.host.timing = False
        ms = max(1, now_ms() - self.start)
        sec = ms / 1000
        total = max(1, sum(self.sums))
        loops = max(1, self.loops)
        reports = self.host.report_count - self.reports0
        parts = []
        for i in range(len(self.names)):
            s = self.sums[i]
            if s or self.peaks[i]:
                parts.append("%s %d%% (longest %d ms)" % (self.names[i], s * 100 // total, self.peaks[i]))
        rate = self.used / sec
        clean = ("about every %.1f s" % (sec / self.cleanups)) if self.cleanups else "none seen"
        eps = []
        for ep, reads, got, spent in self.eps:
            n = ep.reads - reads
            if n:
                eps.append("if%d %d reads/s, %d with data, %.1f ms per read"
                           % (ep.interface, n / sec, (ep.got - got) / sec, (ep.ms - spent) / n))
        return ("Smoothness over %.0f s: %d passes (%.2f ms each on average, longest %d ms, %d took 5 ms or more); "
                "reports read %d/s, mouse reports sent to the computer %d/s (of at most 125; %d waited for it)."
                % (sec, self.loops, ms / loops, self.gap_max, self.slow, reports / sec,
                   (self.out.sends - self.sends0) / sec, self.out.waits - self.waits0),
                "Smoothness - time spent: " + ", ".join(parts) + ".",
                "Smoothness - endpoints: " + ("; ".join(eps) or "none") + "." +
                ("" if getattr(self.host, "_no_raise", True) else
                 " (Every empty read raises an error here: this CircuitPython has no raise_on_timeout - memory used.)"),
                "Smoothness - memory: %.1f KB/s used, clean-ups %s, %d bytes free after a clean-up."
                % (rate / 1024, clean, gc.mem_free()))
