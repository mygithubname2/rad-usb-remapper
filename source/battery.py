# battery.py - read the battery level of the attached mouse / keyboard.
#
# There is no single USB method every device uses, so several readers are tried:
#   battery_razer.py     Razer vendor protocol (90-byte feature reports)
#   battery_logitech.py  Logitech HID++ 2.0 (also names the device paired to a receiver)
#   battery_keychron.py  Keychron raw-HID probe
#   battery_base.py      standard HID battery field + shared helpers
# A vendor's reader is loaded only when one of that vendor's devices is plugged in. A
# reader that can't be loaded (MemoryError) is skipped and reported; the remapper keeps
# running.

import gc
import time

from battery_base import (now_ms, result, StandardReader, RAZER_VID, LOGITECH_VID,
                          KEYCHRON_VID, POLL_MS, FIRST_DELAY_MS, EARLY_RETRIES_MS, RETRY_MS, WAKE_GAP_MS)

LOAD_ERRORS = []         # readers that could not be loaded, for the debug log
LOAD_INFO = []           # readers loaded since the last look, for the debug log


def _load(module, name):
    gc.collect()
    free = getattr(gc, "mem_free", lambda: 0)       # (CircuitPython; not on a desktop)
    before = free()
    try:
        cls = getattr(__import__(module), name)
        gc.collect()
        LOAD_INFO.append("%s loaded (%.1f KB of memory)" % (module, (before - free()) / 1024))
        return cls
    except MemoryError as err:
        LOAD_ERRORS.append("%s not loaded (%r)" % (module, err))
    except ImportError as err:
        LOAD_ERRORS.append("%s missing - copy it to CIRCUITPY (%r)" % (module, err))
    return None


# A vendor's reader is loaded only once one of its devices is plugged in: each one takes
# 1.5-5 KB of RAM, which the board doesn't have to spare for devices that aren't there.
VENDOR_READERS = {RAZER_VID: ("battery_razer", "RazerReader"),
                  LOGITECH_VID: ("battery_logitech", "LogitechReader"),
                  KEYCHRON_VID: ("battery_keychron", "KeychronReader")}
_loaded = {}             # vendor ID -> reader class (None: couldn't be loaded)
LOW_MEMORY_RETRY_MS = 10000   # a check that ran out of memory is tried again this much later


def readers_for(vid):
    """Reader classes to try for a device, most specific first."""
    spec = VENDOR_READERS.get(vid)
    if spec and vid not in _loaded:
        _loaded[vid] = _load(*spec)
    special = _loaded.get(vid)
    return (special, StandardReader) if special else (StandardReader,)


class BatteryMonitor:
    def __init__(self, host, log, send):
        self.host = host
        self.log = log
        self.send = send
        self.state = []           # what the page shows: one dict per reading
        self._readers = {}        # device key -> (target, reader or None)
        self._due = None
        self._raw = False
        self._last_good = {}      # device key -> readings from the last successful check
        self._keys = ()
        self._misses = {}         # device key -> failed checks in a row (device should answer)
        self._sleeping = False    # last check found a device asleep (or kept a last-known level)
        self._checked_at = 0

    def _classes(self, target):
        classes = readers_for(target["vid"])
        while LOAD_ERRORS:                      # a vendor reader that couldn't be loaded just now
            self.log("Battery: " + LOAD_ERRORS.pop(0), "warn")
        while LOAD_INFO:
            self.log("Battery: " + LOAD_INFO.pop(0), "info")
        return classes

    # ---- plumbing used by the readers
    def ctrl(self, target, rtype, request, value, index, data):
        try:
            got = target["dev"].ctrl_transfer(rtype, request, value, index, data)
            return got if got is not None else 0
        except Exception as err:
            self.raw("ctrl %02x/%02x %04x if%d failed: %r" % (rtype, request, value, index, err))
            return None

    def write(self, target, endpoint, data):
        try:
            target["dev"].write(endpoint, data, timeout=100)
            return True
        except Exception as err:
            self.raw("write to ep %02x failed: %r" % (endpoint, err))
            return False

    def wait(self, ms):
        end = now_ms() + ms
        while now_ms() < end:
            self.host.pump()                  # keep the mouse working meanwhile
            time.sleep(0.002)

    def await_report(self, target, iface, match, timeout_ms):
        return self.host.await_vendor_report(target["dev"], iface, match, timeout_ms)

    def raw(self, text):
        if self._raw:
            self.log("  battery: " + text, "info")

    def logitech_readers(self):
        """The Logitech HID++ readers of the attached devices (haptics.py reuses them)."""
        return self.vendor_readers(("Logitech HID++",))

    def vendor_readers(self, names):
        """The attached devices' readers with one of these names (mouselight.py reuses them)."""
        out = []
        for target in self.host.targets:
            key = target["key"]
            if key not in self._readers:
                self._readers[key] = [r(self, target) for r in self._classes(target)]
            out.extend(r for r in self._readers[key] if r.name in names and r.usable())
        return out

    # ---- scheduling
    def devices_changed(self):
        keys = tuple(sorted(str(t["key"]) for t in self.host.targets))
        if keys == self._keys:            # same devices: keep what we know
            return
        self._keys = keys
        self._readers = {}
        self._last_good = {}
        self._misses = {}
        self._sleeping = False
        self.state = []
        self._due = now_ms() + FIRST_DELAY_MS if self.host.targets else None
        self.publish()

    def service(self):
        t = now_ms()
        # a device reported asleep is clearly awake again once it sends input: check now
        # (not on every movement - at most every WAKE_GAP_MS)
        woke = (self._sleeping and self.host.last_input > self._checked_at
                and t - self._checked_at >= WAKE_GAP_MS)
        if woke or (self._due is not None and t >= self._due):
            try:
                self.check()
            except MemoryError as err:            # fragmented memory: never take the remapper down,
                gc.collect()                      # just try again a little later
                self._raw = False
                self._due = now_ms() + LOW_MEMORY_RETRY_MS
                self._checked_at = now_ms()
                try:
                    self.log("Battery check postponed: not enough memory just now (%r) - trying again in %d s"
                             % (err, LOW_MEMORY_RETRY_MS // 1000), "info")
                except MemoryError:
                    pass

    def check(self, raw=False, fresh=False):
        """Read every attached device. raw=True logs each request and reply."""
        self._raw = raw
        if fresh:                                  # forget the remembered method, not the last reading
            self._readers = {}
        readings = []                              # (target, reading)
        sleeping = False
        retry_at = now_ms() + POLL_MS
        for target in list(self.host.targets):
            key = target["key"]
            if key not in self._readers:
                self._readers[key] = [r(self, target) for r in self._classes(target)]
            readers = self._readers[key]       # local: an unplug during the read resets self._readers
            for reader in [r for r in readers if r.usable()]:
                try:
                    got = reader.read()
                except Exception as err:          # a reader must never take the remapper down
                    self.raw("%s reader error: %r" % (reader.name, err))
                    got = None
                if got is None:
                    continue
                got = got if isinstance(got, list) else [got]
                for reading in got:
                    reading["name"] = target["name"]
                    reading["t"] = now_ms()
                if all(r["level"] is None and r.get("retry") for r in got) and key in self._last_good:
                    got = self._stale(key)        # asleep now: show the last level we had
                for reading in got:
                    readings.append((target, reading))
                    sleeping = sleeping or bool(reading.get("retry"))
                if any(r["level"] is not None and not r.get("stale") for r in got):
                    self._last_good[key] = [dict(r) for r in got]
                self._readers[key] = [reader] + [r for r in readers if r is not reader]
                break
            else:
                if key in self._last_good:        # failed this time: keep the last good reading
                    for reading in self._stale(key):
                        readings.append((target, reading))
                    sleeping = True
                    self._readers[key] = [r(self, target) for r in self._classes(target)]   # rediscover next time
                    continue
                readings.append((target, dict(result("none", text="not reported"), name=target["name"])))
                # a Razer / Logitech device (or one with a battery field) should answer: the USB
                # link may just have been busy, so try again soon a few times before giving up
                misses = self._misses.get(key, 0)
                if misses < len(EARLY_RETRIES_MS) and any(r.usable() and not getattr(r, "speculative", False)
                                                              for r in readers):
                    self._misses[key] = misses + 1
                    retry_at = min(retry_at, now_ms() + EARLY_RETRIES_MS[misses])
                continue
            self._misses.pop(key, None)
        self._raw = False
        # a device unplugged while it was being read must not come back on the page
        readings = [r for target, r in readings if any(target is t for t in self.host.targets)]
        before = self.describe()
        self.state = readings
        if not raw:
            for line in self.describe():
                if line not in before and "last known" not in line:   # log new / changed readings only
                    self.log("Battery: " + line, "info")
        self._due = min(retry_at, now_ms() + (POLL_MS, RETRY_MS)[sleeping]) if self.host.targets else None
        self._sleeping = sleeping
        self._checked_at = now_ms()
        self.publish()
        return readings

    def _stale(self, key):
        out = []
        for r in self._last_good[key]:
            r = dict(r, stale=True, retry=True)
            minutes = max(0, (now_ms() - r.get("t", now_ms())) // 60000)
            r["status"] = "last known, %d min ago" % minutes if minutes else "last known"
            r["charging"] = None
            out.append(r)
        return out

    def publish(self):
        self.send({"type": "battery", "list": self.state})

    def describe(self):
        return ["%s: battery not reported" % r["name"] if r["method"] == "none" else
                "%s%s: %s%s%s via %s" % (r["name"], (" > " + r["device"]) if r.get("device") else "",
                                        ("~" if r["approx"] else ""), r["text"],
                                        (", " + r["status"]) if r.get("status") else "", r["method"])
                for r in self.state]
