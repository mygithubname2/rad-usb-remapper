# remapper_main.py -- (started by code.py) USB mouse/keyboard remapper for the
#            Adafruit Feather RP2040 with USB Type A Host (CircuitPython 11)
#
#   physical mouse/keyboard --> [USB-A host port, PIO USB] --> HID report parser
#        --> remap engine --> chosen USB devices on the USB-C port --> computer:
#        keyboard, hi-res mouse, media keys, foot pedal, MIDI
#
# Supports standard mice/keyboards and gaming mice that use several HID
# interfaces (e.g. side buttons that arrive as keyboard keys).
#
# The configuration web page talks to the board over the second USB serial
# port (usb_cdc.data) using one JSON object per line: commands in COMMANDS
# below (the help file lists them all), replies with a "type".
#
# Needs no libraries in /lib except adafruit_displayio_ssd1306 for the optional status display.

import gc
import os
import struct
import sys
import time

import board
import digitalio
import microcontroller
import supervisor
import usb_cdc
import usb_hid

# Free memory before each import, so leftovers don't fragment the heap. _mark() records
# what each part costs in RAM; the log shows it at start-up.
_MEM = []
gc.collect()
_mem_at = gc.mem_free()


def _mark(name):
    global _mem_at
    gc.collect()
    now = gc.mem_free()
    _MEM.append((name, _mem_at - now))
    _mem_at = now


import settings_store
config = settings_store.load()            # read from flash once; kept current from here on
_mark("settings")
try:
    from oled import Oled
except (MemoryError, ImportError) as _err:    # optional feature
    OLED_ERROR = "status display disabled: %r" % (_err,)

    class Oled:
        """Stand-in when oled.py can't be loaded."""
        status = ""

        def __init__(self, log):
            self.status = OLED_ERROR

        def configure(self, rotation):
            pass

        def service(self, info):
            pass

        def describe(self):
            return self.status

        def begin(self):
            pass
# Start the status display FIRST, before anything else is imported: its driver and
# buffers need a few blocks of ~1 KB, and later the heap is in pieces too small for them.
_early_log = []
oled = Oled(lambda msg, level="info": _early_log.append((msg, level)))
try:
    oled.configure(config["oled"]["rot"])
    gc.collect()
    oled.begin()
except Exception as _err:                    # never stop the remapper over the display
    _early_log.append(("Display: could not start (%r)" % (_err,), "warn"))
_mark("status display")
import devprofiles
_mark("per-device profiles")
from hostinput import HostInput, now_ms
_mark("USB host + HID parser")
from remap import Remapper
_mark("remapping")
try:
    from boardbuttons import BoardButtons
except (MemoryError, ImportError) as _err:    # optional feature
    BoardButtons = None
    BOARD_BUTTONS_ERROR = "board buttons disabled: %r" % (_err,)
else:
    BOARD_BUTTONS_ERROR = None
_mark("board buttons")
class Haptics:
    """haptics.py is loaded only once a mouse with a haptic motor is found (the battery
    reader notices the motor when it first talks to a Logitech mouse): other mice don't
    pay for it in memory."""
    def __init__(self, battery, log, send):
        self.battery, self.log, self.send = battery, log, send
        self.real = None
        self._check_at = 0

    @property
    def state(self):
        return self.real.state if self.real else {"available": False}

    def play(self, wave):
        if self.real:
            self.real.play(wave)

    def set_strength(self, enabled, intensity):
        if self.real:
            self.real.set_strength(enabled, intensity)

    def devices_changed(self):
        if self.real:
            self.real.devices_changed()
        self._check_at = 0

    def service(self):
        if self.real:
            self.real.service()
            return
        if now_ms() < self._check_at:
            return
        self._check_at = now_ms() + 2000
        for reader in self.battery.logitech_readers():
            if reader.slots and any(slot.get("haptic") is not None for slot in reader.slots.values()):
                try:
                    from haptics import Haptics as _Real
                except (MemoryError, ImportError) as err:
                    self.log("Haptic feedback disabled: %r" % (err,), "warn")
                    self._check_at = now_ms() + 600000
                    return
                self.real = _Real(self.battery, self.log, self.send)
                return

    def publish(self):
        if self.real:
            self.real.publish()
            return
        self.send({"type": "haptic", "available": False})

    def describe(self):
        return self.real.describe() if self.real else "no haptic mouse found"


_mark("haptics (loaded when a haptic mouse is found)")
try:
    from battery import BatteryMonitor, LOAD_ERRORS as BATTERY_ERRORS
except (MemoryError, ImportError) as _err:   # optional feature: never stop the remapper for it
    BATTERY_ERRORS = ["battery level reading disabled: %r" % (_err,)]

    class BatteryMonitor:
        """Stand-in when battery.py can't be loaded: the page shows no battery."""
        def __init__(self, host, log, send):
            self.state = []
            self.send = send

        def devices_changed(self):
            pass

        def service(self):
            pass

        def check(self, raw=False, fresh=False):
            return []

        def publish(self):
            self.send({"type": "battery", "list": []})

        def describe(self):
            return []
_mark("battery (vendor readers load when needed)")

FW_VERSION = "3.40.1"
HISTORY = 24               # debug lines kept for replay when the page connects...
HISTORY_CHARS = 1600       # ...and at most this much text (the lines took up to ~6 KB of memory)
supervisor.runtime.autoreload = True


# ======================================================================
#  Serial link to the web page + debug function
# ======================================================================
from seriallink import Link   # noqa: E402  (JSON link to the page)
_mark("page link")


link = Link((("data", usb_cdc.data), ("console", getattr(usb_cdc, "console", None))))
link.log = lambda msg, level="info": debug(msg, level)
_history = []
_history_chars = [0]


def debug(msg, level="info"):
    """Debug output: REPL console + web page debug panel (+ replay buffer).

    level: "info", "ok", "warn", "error"
    """
    stamp = now_ms()
    if not link.on_console():             # page on the console: JSON only, no duplicates
        print("[%9.3f] %-5s %s" % (stamp / 1000, level, msg))
    _history.append((stamp, level, msg))
    _history_chars[0] += len(msg)
    while len(_history) > HISTORY or (_history_chars[0] > HISTORY_CHARS and len(_history) > 1):
        _history_chars[0] -= len(_history.pop(0)[2])
    link.send({"type": "debug", "level": level, "msg": msg, "t": stamp})


# ======================================================================
#  USB HID output to the computer
# ======================================================================
class Keyboard:
    """The keyboard report the computer gets: [modifier bits, 0, up to 6 key codes] - what the
    adafruit_hid library's Keyboard sends. (That library, with its table of key names, took about
    8 KB of memory; the board only ever sends key codes.)"""

    def __init__(self, dev):
        self.dev = dev
        self.report = bytearray(8)

    def press(self, *codes):
        r = self.report
        for code in codes:
            if 0xE0 <= code <= 0xE7:              # Ctrl / Shift / Alt / GUI: a bit each
                r[0] |= 1 << (code - 0xE0)
                continue
            for i in range(2, 8):                # the first free slot (a 7th key is left out)
                if r[i] == code:
                    break
                if not r[i]:
                    r[i] = code
                    break
        self.dev.send_report(r)

    def release(self, *codes):
        r = self.report
        for code in codes:
            if 0xE0 <= code <= 0xE7:
                r[0] &= ~(1 << (code - 0xE0))
                continue
            for i in range(2, 8):
                if r[i] == code:                 # take it out; the keys after it move up
                    for j in range(i, 7):
                        r[j] = r[j + 1]
                    r[7] = 0
                    break
        self.dev.send_report(r)

    def release_all(self):
        r = self.report
        for i in range(8):
            r[i] = 0
        self.dev.send_report(r)


class ConsumerControl:
    """Media keys: a 2-byte report with the pressed usage (0 = none), as adafruit_hid sends."""

    def __init__(self, dev):
        self.dev = dev
        self.report = bytearray(2)

    def press(self, usage):
        self.report[0], self.report[1] = usage & 0xFF, usage >> 8
        self.dev.send_report(self.report)

    def release(self):
        self.report[0] = self.report[1] = 0
        self.dev.send_report(self.report)


_MIDI_STATUS = {"n": 0x90, "c": 0xB0, "p": 0xC0}   # note on, control change, program change


def _midi_status(kind, channel):
    return _MIDI_STATUS[kind] | (channel - 1)


def _midi_off(status, number):
    """Note on -> note off (0x80); a controller goes back to 0."""
    return (0x80 | (status & 0x0F), number, 0) if status & 0xF0 == 0x90 else (status, number, 0)


def _midi_port():
    """The USB MIDI output port, if boot.py presented MIDI."""
    try:
        import usb_midi
        return ([p for p in usb_midi.ports if hasattr(p, "write")] or [None])[0]
    except Exception:
        return None


def _find_hid(usage_page, usage):
    for dev in usb_hid.devices:
        if dev.usage_page == usage_page and dev.usage == usage:
            return dev
    return None


def _clamp(value, limit):
    return max(-limit, min(limit, value))


def _read_boot_out():
    """Read what boot.py actually did (and any errors) from boot_out.txt."""
    msc, forced, errors, usb = None, False, [], None
    values = {}
    try:
        with open("/boot_out.txt") as f:
            for line in f:
                line = line.strip()
                if line.startswith("REMAP_MSC="):
                    msc, forced = line.split()[0].endswith("1"), "FORCED=1" in line
                if line.startswith("REMAP_USB="):
                    usb = _parse_usb(line.split("=", 1)[1])
                if line.startswith("REMAP_") and "=" in line and " " not in line:
                    key, value = line[6:].split("=", 1)
                    values[key.lower()] = value
                if line.startswith("REMAP_ERROR") or "Error" in line or "Traceback" in line:
                    errors.append(line)
    except OSError:
        pass
    return msc, forced, errors[-6:], usb, values


def _parse_usb(text):
    """boot.py's 'xxx--|vvvv:pppp|ssss': devices on/off (settings_store.USB_DEVICES order),
    the USB ID as saved, and a checksum of the names (they don't fit in boot_out.txt).
    Names matching the checksum are taken from the settings; otherwise marked as different."""
    try:
        flags, ids, names = text.split("|")
        vid, pid = (int(x, 16) for x in ids.split(":"))
        names = int(names, 16)
    except ValueError:                                   # older boot.py: treat as unknown
        return None
    out = {name: flags[i:i + 1] == "x" for i, name in enumerate(settings_store.USB_DEVICES)}
    usb = config["usb"]
    same = names == settings_store.names_sum(usb["mfr"], usb["product"])
    out.update(mfr=usb["mfr"] if same else "(other names)", product=usb["product"] if same else "", vid=vid, pid=pid)
    return out


USB_LABELS = (("kbd", "keyboard"), ("mouse", "mouse"), ("media", "media keys"), ("pedal", "foot pedal"),
              ("midi", "MIDI"))


def usb_label(usb):
    if not usb:
        return "unknown"
    parts = ", ".join(n for k, n in USB_LABELS if usb.get(k)) or "no keyboard / mouse / pedal / MIDI"
    names = " / ".join(x for x in (usb.get("mfr"), usb.get("product")) if x)
    vid, pid = (0, 0) if usb.get("pedal") else (usb.get("vid", 0), usb.get("pid", 0))   # (pedal: its own ID)
    return (parts + ((" - named " + names) if names else "")
            + ((" - USB ID %04X:%04X" % (vid or 0x239A, pid or 0x812A)) if vid or pid else ""))


MSC_AT_BOOT, MSC_FORCED, BOOT_ERRORS, USB_AT_BOOT, BOOT_VALUES = _read_boot_out()


# The computer takes a report every 8 ms (CircuitPython's USB setting). Movement is sent once in
# each of those 8 ms slots, at the first loop pass after the computer took the previous report
# (HidOut.poll_at, the predicted time of that). The prediction is kept in step with the computer:
# a send that had to wait ended exactly when the computer took the previous report. (Sending
# "every 8 ms" drifted to every 9-13 ms - the first pass after 8 ms - and about a quarter of the
# computer's slots got no movement: uneven motion.)
POLL_MS = 8


class HidOut:
    """USB HID output to the computer.

    * keyboard and media keys: the Keyboard and ConsumerControl classes above
    * mouse via the custom 16-bit report defined in boot.py; movement is
      accumulated and sent once per main-loop pass (flush), so nothing is lost
      when the input mouse reports faster than the computer polls us
    * foot pedal (when chosen in Settings): 2-byte report,
      byte 0 = 1 left | 2 center | 4 right
    * MIDI notes / controllers / program changes through usb_midi (when chosen)
    * every device is optional (chosen on the page, set up by boot.py); an
      action for a missing device is dropped with a one-time warning
    * a sleeping/absent computer never crashes or stalls the loop: after a
      failed send, output pauses for a second (input is dropped meanwhile)
    """

    def __init__(self):
        self.kbd = None
        self.cc = None
        self.mouse_dev = None
        self.pedal_dev = None
        self.pedal_rid = 4               # None when the pedal is the only HID device (no report ID)
        self.midi_out = None             # usb_midi output port, when MIDI is presented
        self.notes = []                  # MIDI notes / controllers held down: (status, number)
        self.found = False
        self.wide = True                 # 7-byte report; False = old 4-byte mouse
        self.buttons = 0
        self.pedal = 0
        self.acc = [0, 0, 0, 0]          # dx, dy, wheel, pan
        self.dirty = False
        self.sent_at = 0                 # now_ms() of the last mouse report to the computer
        self.poll_at = 0                 # when the computer takes the last report sent (predicted)
        self.sends = 0                   # mouse reports sent (for "Measure smoothness")...
        self.waits = 0                   # ...and how many of them had to wait for the computer
        self.media = []
        self._r7 = bytearray(7)
        self._r4 = bytearray(4)
        self._rp = bytearray(2)
        self._retry_at = 0
        self._pause_until = 0
        self._last_error = None
        self._missing_warned = set()

    def ready(self):
        if self.found:
            return True
        if now_ms() < self._retry_at:
            return False
        self._retry_at = now_ms() + 2000
        self.mouse_dev = _find_hid(0x01, 0x02)
        self.pedal_dev = _find_hid(0x0C, 0x03)
        kbd = _find_hid(0x01, 0x06)
        self.kbd = Keyboard(kbd) if kbd is not None else None
        media = _find_hid(0x0C, 0x01)
        self.cc = ConsumerControl(media) if media is not None else None
        self.midi_out = _midi_port()
        parts = [name for name, dev in (("keyboard", self.kbd), (("standard mouse", "16-bit mouse")[self.wide], self.mouse_dev),
                                        ("media keys", self.cc), ("foot pedal", self.pedal_dev),
                                        ("MIDI", self.midi_out)) if dev]
        if not parts:
            self._report("No USB output devices - choose some in Settings › Devices, then press RESET")
            return False
        self.pedal_rid = 4 if len(getattr(usb_hid, "devices", ())) > 1 else None
        self.found = True
        debug("USB HID output ready: " + ", ".join(parts), "ok")
        return True

    def _report(self, text):
        if text != self._last_error:
            self._last_error = text
            debug(text, "warn")

    def _has(self, dev, what):
        if dev is not None:
            return True
        if self.ready() and what not in self._missing_warned:
            self._missing_warned.add(what)
            debug("%s output is off - turn it on in Settings › Devices the board presents, then reboot"
                  % what, "warn")
        return False

    def _call(self, fn, *args):
        if now_ms() < self._pause_until or not self.ready():
            return False
        try:
            fn(*args)
            return True
        except Exception as err:        # OSError when the computer is asleep, etc.
            self._pause_until = now_ms() + 1000
            self._report("HID send failed (computer asleep?): %r" % (err,))
            return False

    # keyboard
    def key_press(self, codes):
        if self._has(self.kbd, "Keyboard"):
            self._call(lambda: self.kbd.press(*codes))

    def key_release(self, codes):
        if self._has(self.kbd, "Keyboard"):
            self._call(lambda: self.kbd.release(*codes))

    # media
    def media_press(self, usage):
        self.media.append(usage)
        if self._has(self.cc, "Media key"):
            self._call(self.cc.press, usage)

    def media_release(self, usage):
        if usage in self.media:
            self.media.remove(usage)
        if self._has(self.cc, "Media key"):
            remaining = self.media[-1:]          # still-held usage, if any
            self._call(lambda: self.cc.press(remaining[0]) if remaining else self.cc.release())

    # foot pedal
    def pedal_press(self, mask):
        self.pedal |= mask
        self._send_pedal()

    def pedal_release(self, mask):
        self.pedal &= ~mask
        self._send_pedal()

    def _send_pedal(self):
        if not self._has(self.pedal_dev, "foot pedal"):
            return
        self._rp[0] = self.pedal & 0x07
        self._rp[1] = 0
        args = (self._rp,) + ((self.pedal_rid,) if self.pedal_rid else ())
        self._call(self.pedal_dev.send_report, *args)

    # mouse
    def mouse_press(self, mask):
        self.buttons |= mask
        self.dirty = True
        self.flush()

    def mouse_release(self, mask):
        self.buttons &= ~mask
        self.dirty = True
        self.flush()

    def mouse_move(self, dx, dy, wheel, pan):
        acc = self.acc
        acc[0] += dx
        acc[1] += dy
        acc[2] += wheel
        acc[3] += pan

    def flush(self):
        acc = self.acc
        if not (self.dirty or acc[0] or acc[1] or acc[2] or acc[3]):
            return
        # Movement is gathered and sent once per 8 ms slot of the computer (see POLL_MS); button
        # changes go at once.
        now = now_ms()
        if not self.dirty and now < self.poll_at:
            return
        self.sent_at = now
        self.sends += 1
        if not self._has(self.mouse_dev, "Mouse"):
            acc[0] = acc[1] = acc[2] = acc[3] = 0
            self.dirty = False
            return
        # (no tuples, bound methods or *args here: this runs up to 125 times a second)
        limit = 32767 if self.wide else 127
        x = _clamp(acc[0], limit)
        y = _clamp(acc[1], limit)
        w = _clamp(acc[2], 127)
        p = _clamp(acc[3], 127)
        sent = False
        if now >= self._pause_until and self.ready():
            try:
                start = now_ms()
                self._send_move(x, y, w, p)
                sent = True
                after = now_ms()
                # when the computer will take this report: if the send had to wait, the computer took
                # the previous one just now; otherwise at the next predicted 8 ms beat. Timed from
                # just before the send, and 1 ms is not counted as a wait: with a 1 ms clock that is
                # usually only the clock ticking over during a quick send, and treating it as a wait
                # put the predicted beat off (3.37.0: 638 of 930 sends "waited", 93 sent a second).
                waited = after - start >= 2
                base = after if waited else (self.poll_at or after)
                if waited:
                    self.waits += 1
                if after - base > 64:           # (no movement for a while: the beat is unknown)
                    base = after
                while base <= after:
                    base += POLL_MS
                self.poll_at = base
            except Exception as err:        # OSError when the computer is asleep, etc.
                self._pause_until = now_ms() + 1000
                self._report("HID send failed (computer asleep?): %r" % (err,))
        if sent:                            # keep the rest for the next pass
            acc[0] -= x
            acc[1] -= y
            acc[2] -= w
            acc[3] -= p
        else:
            acc[0] = acc[1] = acc[2] = acc[3] = 0
        self.dirty = False

    def _send_move(self, x, y, w, p):
        if self.wide:
            struct.pack_into("<Bhhbb", self._r7, 0, self.buttons & 0xFF, x, y, w, p)
            try:
                self.mouse_dev.send_report(self._r7, 2)
                return
            except ValueError:          # standard 4-byte mouse is active
                self.wide = False
                debug("Standard (8-bit) mouse active - choose Mouse type: high-resolution and reboot "
                      "for 16-bit motion and sideways scroll", "warn")
        struct.pack_into("<Bbbb", self._r4, 0, self.buttons & 0x1F, _clamp(x, 127), _clamp(y, 127), w)
        self.mouse_dev.send_report(self._r4, 2)

    # MIDI: [kind, channel, number, value]; kind n = note, c = controller, p = program change
    def midi_press(self, v):
        if not self._has(self.midi_out, "MIDI"):
            return
        kind, channel, number, value = v
        status = _midi_status(kind, channel)
        self._midi((status, number) if kind == "p" else (status, number, value))
        if kind != "p":
            self.notes.append((status, number))

    def midi_release(self, v):
        if self.midi_out is None:
            return
        status = _midi_status(v[0], v[1])
        number = v[2]
        if (status, number) in self.notes:
            self.notes.remove((status, number))
            self._midi(_midi_off(status, number))

    def _midi(self, data):
        self._call(lambda: self.midi_out.write(bytes(data)))

    def release_all(self):
        for status, number in self.notes:   # no hanging notes
            self._midi(_midi_off(status, number))
        self.notes = []
        self.media = []
        self.buttons = 0
        self.acc = [0, 0, 0, 0]
        if not self.ready():
            return
        if self.kbd:
            self._call(lambda: self.kbd.release_all())
        if self.cc:
            self._call(self.cc.release)
        held, self.pedal = self.pedal, 0
        if self.pedal_dev and held:
            self._send_pedal()
        self.dirty = self.mouse_dev is not None
        self.flush()


# ======================================================================
#  State
# ======================================================================
# Each input device has its own profiles, in its own file on the drive (devprofiles.py); a
# new device starts with one empty profile. The board writes the drive - except in Update
# mode (or with the D5 jumper), when the computer may, for one start-up: then profiles are
# used but can't be saved.
UPDATE_MODE = not devprofiles.writable()
_dev = {"id": None, "name": "", "saved": False, "dirty": False, "blocked": False}
_active_save_at = 0          # when to remember the active profile in the device's file
config = dict(config, profiles=settings_store.empty_profiles(), active=0)
hid_out = HidOut()
hid_out.wide = BOOT_VALUES.get("mouse_type", "hires") != "standard"   # before anything is sent


_move_hit_at = 0             # movement comes in hundreds of reports a second: tell the page twice a second


def _remap_hit(table, code):
    """Live highlight of the matching mapping row while the page is connected."""
    global _move_hit_at
    if not link.peer:
        return
    if table == "move":
        t = now_ms()
        if t < _move_hit_at:
            return
        _move_hit_at = t + 500
        link.send_raw(_MOVE_HIT)            # (made once: this one is sent twice a second while the mouse moves)
        return
    link.send({"type": "hit", "dev": table, "code": code})


_MOVE_HIT = b'{"type": "hit", "dev": "move", "code": 0}'


def _remap_event(table, code, down, action):
    if config["verbose"]:
        link.send({"type": "event", "dev": table, "code": code, "down": down, "action": action})


PEDAL_NAMES = ((1, "Left"), (2, "Center"), (4, "Right"))


def _pedal_latched(latched, changed):
    """Toggle (deadman) mode feedback: always logged, and shown live on the page."""
    for bit, name in PEDAL_NAMES:
        if changed & bit:
            debug("%s pedal %s (latched mapping)" % (name, ("unlatched", "LATCHED DOWN")[bool(latched & bit)]),
                  ("info", "ok")[bool(latched & bit)])
    link.send({"type": "pedal_state", "latched": latched, "count": remapper.latched_count()})


def _latch_changed(table, code, action, on):
    """A latched action turned on / off: the page logs it with names (always, not only verbose)."""
    link.send({"type": "latch", "dev": table, "code": code, "on": on, "action": action,
               "count": remapper.latched_count()})


def active_profile():
    return config["profiles"][config["active"]]


def store_settings(cfg):
    """Save the device-wide settings (LED, display, names...) in NVM. The profiles saved
    there stay as they are: unsaved test mappings never reach flash this way. (They are
    read back from flash for each save rather than kept: in per-device mode they would
    otherwise sit in RAM the whole time.)"""
    clean = settings_store.save(cfg)        # (profiles are not stored here: no read-back needed)
    return dict(clean, profiles=cfg["profiles"], active=cfg["active"])


def save_settings(**changes):
    """Change and save device-wide settings; config becomes the cleaned result."""
    global config
    config = store_settings(dict(config, **changes))
    return config


def device_info():
    """Whose profiles the page is editing, and where they are kept."""
    return {"per_device": True, "id": _dev["id"], "name": _dev["name"], "saved": _dev["saved"],
            "dirty": _dev["dirty"], "blocked": _dev["blocked"], "writable": devprofiles.writable(),
            "update_mode": UPDATE_MODE,
            "bytes": profile_bytes(), "capacity": devprofiles.MEMORY_BUDGET}


def profile_bytes():
    return devprofiles.size(config["profiles"], config["active"], _dev["name"])


def send_config():
    link.send({"type": "config", "config": config, "device": device_info()})


def use_profiles(profiles, active, note, level="ok"):
    """Make `profiles` the live set (device switch, copy, reset)."""
    global config
    config = dict(config, profiles=profiles, active=active)
    clear_holds()
    reset_layer()
    remapper.load(effective_profile())
    debug(note, level)
    send_config()
    _send_layer()


def _device_switch():
    """Per-device mode: load the attached device's own profiles when it changes."""
    if not host.devices:
        return                            # unplugged: keep the set (and unsaved edits) for a re-plug
    d = host.devices[0]
    did = "%s:%s" % (d["vid"], d["pid"])
    if did == _dev["id"] and not _dev["blocked"]:
        return
    if _dev["dirty"] and did != _dev["id"]:
        debug("Unsaved profile changes for %s were dropped (another device was plugged in)"
              % (_dev["name"] or _dev["id"]), "warn")
    name = (config.get("aliases") or {}).get(did) or d["name"]
    found = None
    _dev.update(blocked=False)
    for attempt in range(2):
        try:
            found = devprofiles.load(did)
            break
        except MemoryError as err:
            if attempt:
                # the file is there but couldn't be read: never treat this as a new device,
                # or the next Apply would overwrite the saved profiles
                _dev.update(id=did, name=name, saved=True, dirty=False, blocked=True)
                use_profiles(settings_store.empty_profiles(), 0,
                             "Not enough memory to load the saved profiles for %s (%r). They are kept "
                             "and won't be overwritten: unplug and re-plug the device to try again."
                             % (name, err), "warn")
                return
            remapper.release_all()
            config["profiles"] = settings_store.empty_profiles()   # free the old set, then retry
            config["active"] = 0
            remapper.load(active_profile())
            gc.collect()
    if not found and _dev["id"] is None and _dev["dirty"]:
        # edits made before any device was plugged in: they are for this one (not saved yet)
        _dev.update(id=did, name=name, saved=False)
        debug("%s has no profiles of its own yet: the changes you made before plugging it in are used "
              "(click Apply to save them for it)" % name, "info")
        send_config()
        return
    _dev.update(id=did, name=(found[2] if found and found[2] else name), saved=bool(found), dirty=False)
    if found:
        gc.collect()
        use_profiles(found[0], found[1], "Profiles for %s loaded (%d profile(s), about %d bytes of memory; %d bytes free)"
                     % (_dev["name"], len(found[0]), devprofiles.size(found[0], found[1], _dev["name"]), gc.mem_free()))
        return
    use_profiles(settings_store.empty_profiles(), 0,
                 "%s has no profiles of its own yet: starting with one empty profile" % name)


def _remember_active():
    """Per-device mode: write the active profile to the device's file (a few seconds
    after a switch, so switching quickly doesn't write the flash every time)."""
    global _active_save_at
    if not _active_save_at or now_ms() < _active_save_at:
        return
    _active_save_at = 0
    if not (_dev["saved"] and devprofiles.writable()) or _dev["blocked"]:
        return
    try:
        found = devprofiles.load(_dev["id"])          # the SAVED set, not unsaved test edits
        if found and found[1] != config["active"] and config["active"] < len(found[0]):
            devprofiles.save(_dev["id"], found[0], config["active"], found[2])
    except Exception as err:
        debug("Could not remember the active profile: %r" % (err,), "warn")


def profile_label(index=None):
    index = config["active"] if index is None else index
    return "%d: %s" % (index + 1, config["profiles"][index]["name"])


def rule_summary(profile):
    board = len(profile.get("board") or {})
    return "%d mouse, %d key, %d mouse-key, %d media%s rule(s)" % (
        len(profile["mouse"]), len(profile["keys"]), len(profile["mkeys"]), len(profile.get("media") or {}),
        (", %d board-button" % board) if board else "")


def _resolve_profile(value, current, count):
    """Profile / layer action value -> index: a number, or "next"/"prev" (wrapping)."""
    step = {"next": 1, "prev": -1}.get(value)
    if step is not None:
        return (current + step) % count
    index = _clamp_index(value, count)
    return current if index is None else index


def _clamp_index(value, count):
    """value as an index below count, or None."""
    try:
        index = int(value)
    except (TypeError, ValueError):
        return None
    return index if 0 <= index < count else None


class _HoldStack:
    """Hold-to-use buttons (profiles or layers): what each held button chose, in press
    order, and what to go back to when the last one is released."""

    def __init__(self):
        self.items = []          # [(source, index)]
        self.base = None

    def __bool__(self):
        return bool(self.items)

    def clear(self):
        del self.items[:]
        self.base = None

    def push(self, source, index, current):
        if self.base is None:
            self.base = current
        self.items.append((source, index))

    def pop(self, source):
        """Release `source`: the index to switch to, or None if it wasn't held (holds
        were cleared meanwhile by a normal switch, a new mapping...)."""
        found = [i for i, (src, _ix) in enumerate(self.items) if src == source]
        if not found:
            return None
        del self.items[found[0]]
        back = self.items[-1][1] if self.items else self.base
        if not self.items:
            self.base = None
        return back


# ---- hold-to-use profiles: active only while their button is held
_holds = _HoldStack()


def clear_holds():
    _holds.clear()


def _activate(index, note, pulse=True):
    """Make `index` the live profile without saving it (used by holds)."""
    global config
    if index == config["active"]:
        return
    config = dict(config, active=index)
    reset_layer()
    remapper.set_profile(active_profile())
    debug("Profile %s %s" % (profile_label(), note), "ok")
    if pulse:
        _pulse("p", index)
    link.send({"type": "active", "index": index, "temporary": bool(_holds)})


def profile_hold(value, down, source):
    if down:
        index = _clamp_index(value, len(config["profiles"]))
        if index is None:
            debug("Hold-profile %s doesn't exist" % (value,), "warn")
            return
        _holds.push(source, index, config["active"])
        _activate(index, "active while the button is held")
        return
    back = _holds.pop(source)
    if back is not None:
        _activate(back, ("back (button released)", "still held by another button")[bool(_holds)], pulse=False)


def switch_profile(value, persist=True):
    """Activate a profile (from a mapped button or from the page)."""
    global config
    count = len(config["profiles"])
    holding = bool(_holds)
    # next/prev during a hold step from the profile the hold will return to
    index = _resolve_profile(value, (config["active"], _holds.base)[holding], count)
    if holding:               # a normal switch during a hold takes over: no switch back
        clear_holds()
    left_layer = reset_layer()          # any profile switch returns to the Base layer
    if index == config["active"] and not holding and not left_layer:
        if count < 2:
            debug("Profile switch ignored: only one profile exists", "warn")
        return
    if index != config["active"] or left_layer:
        config = dict(config, active=index)
        remapper.set_profile(active_profile())
    debug("Profile %s active (%s)" % (profile_label(), rule_summary(active_profile())), "ok")
    _pulse("p", index)
    link.send({"type": "active", "index": index})
    if persist:
        _persist_active(index)


def _persist_active(index):
    """Remember the active profile in the device's SAVED file (a few seconds later, see
    _remember_active), so an unsaved "Apply (test)" mapping is never saved by a profile switch."""
    global _active_save_at
    _active_save_at = now_ms() + 3000


# ---- layers: extra button tables inside the active profile
# Layer 0 is the profile's own tables ("Base"); layer n uses profile["layers"][n - 1],
# and inputs that layer doesn't map fall through to Base. Layers are never saved:
# every profile starts on Base.
_layer = 0
_lholds = _HoldStack()   # hold-to-use layer buttons


def layer_count():
    return 1 + len(active_profile().get("layers") or [])


def layer_label(index=None):
    index = _layer if index is None else index
    layers = active_profile().get("layers") or []
    return "%d: %s" % (index + 1, (["Base"] + [l["name"] for l in layers])[index])


def effective_profile():
    """The active profile with the current layer's mappings laid over Base."""
    profile = active_profile()
    layers = profile.get("layers") or []
    if not 0 < _layer <= len(layers):
        return profile
    layer = layers[_layer - 1]
    out = dict(profile)
    for table in settings_store.TABLES:
        merged = dict(profile.get(table) or {})
        merged.update(layer.get(table) or {})
        out[table] = merged
    return out


def _send_layer():
    link.send({"type": "layer", "index": _layer, "temporary": bool(_lholds)})


def reset_layer(keep=False):
    """Forget layer holds; go back to Base unless `keep` and the layer still exists."""
    global _layer
    _lholds.clear()
    before = _layer
    if not keep or _layer >= layer_count():
        _layer = 0
    return before != _layer


def _set_layer(index, note, pulse=True):
    global _layer
    if index == _layer:
        return
    _layer = index
    remapper.set_profile(effective_profile(), keep_latches=True)   # latched keys etc. stay on
    debug("Layer %s %s (profile %s)" % (layer_label(), note, profile_label()), "ok")
    if pulse:
        _pulse("l", index)
    _send_layer()


def switch_layer(value):
    """Layer action: a layer number, or "next"/"prev" (wrapping) within the profile."""
    count = layer_count()
    if count < 2:
        debug("Layer switch ignored: profile %s has only the Base layer" % (profile_label(),), "warn")
        return
    holding = bool(_lholds)
    index = _resolve_profile(value, (_layer, _lholds.base)[holding], count)
    _lholds.clear()          # a normal switch during a hold takes over: no switch back
    if index == _layer:
        _send_layer()
        return
    _set_layer(index, "active")


def layer_hold(value, down, source):
    if down:
        index = _clamp_index(value, layer_count())
        if index is None:
            debug("Hold-layer %s doesn't exist in profile %s" % (value, profile_label()), "warn")
            return
        _lholds.push(source, index, _layer)
        _set_layer(index, "active while the button is held")
        return
    back = _lholds.pop(source)
    if back is None:
        return
    _set_layer(back, ("back (button released)", "still held by another button")[bool(_lholds)], pulse=False)
    _send_layer()            # also tells the page the hold is over


# ---- test mode: while the page's pointer is on its Test pad, presses are shown, not sent
TEST_HOLD_MS = 1500      # test mode ends unless the page renews it within this time
_test_until = 0


def _test_press(kind, code, down, action):
    link.send({"type": "test", "dev": kind, "code": code, "down": down, "action": action})


def cmd_test_mode(msg):
    global _test_until
    _test_until = now_ms() + TEST_HOLD_MS if msg.get("on") else 0
    _update_test_mode()


def _update_test_mode():
    want = now_ms() < _test_until
    if want == remapper.test:
        return
    remapper.test = want
    debug(("Test mode off: buttons and keys work normally again",
           "Test mode on: buttons and keys are shown on the page, not sent (pointer still moves)")[want],
          ("info", "ok")[want])
    link.send({"type": "test_mode", "on": want})


remapper = Remapper(hid_out, _remap_event, now_ms)
remapper.on_profile = switch_profile
remapper.on_profile_hold = profile_hold
remapper.on_layer = switch_layer
remapper.on_test = _test_press
remapper.on_text = lambda message: debug(message, "warn")
remapper.on_layer_hold = layer_hold
remapper.on_pedal = _pedal_latched
remapper.on_latch = _latch_changed
remapper.on_hit = _remap_hit
remapper.load(active_profile())

# buttons soldered to the board's free pins (and the BOOT button): watched unless turned off
board_buttons = BoardButtons(lambda m, level="info": debug(m, level)) if BoardButtons else None


def _board_buttons_apply():
    if board_buttons is None:
        return
    if config.get("board_buttons", True):
        board_buttons.start()
    else:
        board_buttons.stop()
        remapper.board_release_all()




def reboot_needed():
    msc = MSC_AT_BOOT is not None and MSC_AT_BOOT != config["msc"]
    usb = USB_AT_BOOT is not None and not MSC_FORCED and USB_AT_BOOT != config["usb"]
    options = [name for name in settings_store.BOOT_OPTIONS
               if name in BOOT_VALUES and not MSC_FORCED and BOOT_VALUES[name] != config[name]]
    return msc or usb or bool(options)


# ======================================================================
#  Status LED (on-board NeoPixel): red = no input device, green = attached
# ======================================================================
from statusled import StatusLed   # noqa: E402
_mark("main firmware objects + status LED")


status_led = StatusLed()
status_led.configure(config["led_brightness"], config["led_balance"], config["led_gamma"])


def send_devices():
    link.send({"type": "devices", "type_summary": host.device_type(), "list": host.devices,
               "caps": host.capabilities()})


def _devices_changed():
    remapper.release_all()
    _device_switch()
    status_led.update(host.endpoints)
    send_devices()
    battery.devices_changed()
    haptics.devices_changed()
    if mouse_light is not None:
        mouse_light.devices_changed()


def _port_power(on):
    _power_pin.value = bool(on)


host = HostInput(debug, _devices_changed)
battery = BatteryMonitor(host, debug, link.send)
haptics = Haptics(battery, debug, link.send)
remapper.on_haptic = haptics.play
mouse_light = None           # mouselight.MouseLight: loaded when turned on and a Logitech or Razer device is attached
_light_check_at = 0


def _mouse_light_service():
    """Match the mouse's own RGB lighting to the active profile's color (Settings > Feedback)."""
    global mouse_light, _light_check_at
    if mouse_light is None:
        if not config.get("mouse_light") or now_ms() < _light_check_at:
            return
        _light_check_at = now_ms() + 2000
        if not battery.vendor_readers(("Logitech HID++", "Razer")):
            return
        gc.collect()
        before = gc.mem_free()
        try:
            from mouselight import MouseLight
        except (MemoryError, ImportError) as err:
            debug("Mouse lighting disabled: %r" % (err,), "warn")
            _light_check_at = now_ms() + 600000
            return
        mouse_light = MouseLight(battery, debug, link.send)
        gc.collect()
        debug("Mouse lighting code loaded (%.1f KB of memory, %d bytes free now)"
              % ((before - gc.mem_free()) / 1024, gc.mem_free()), "info")
    mouse_light.set_enabled(config.get("mouse_light", False))
    mouse_light.set_level(config.get("mouse_light_level", 100))
    mouse_light.set_color(active_profile().get("color"))
    mouse_light.service()


def _publish_mouse_light():
    if mouse_light is not None:
        mouse_light.publish()
        return
    link.send({"type": "mouselight", "enabled": bool(config.get("mouse_light")), "available": False,
               "level": config.get("mouse_light_level", 100)})
# while a battery / haptics request waits for a reply, input keeps flowing (host.pump):
# keep the status LED in step with profile changes made meanwhile
def _on_pump():
    """While a battery / lighting / haptics request waits for the mouse's reply, the mouse is still
    read (host.pump) - and its movement must go on to the computer meanwhile, with the pointer and
    repeat timers running: before 3.37.3 it all waited for the reply (up to half a second, while a
    Razer mouse was searched for the channel its battery answers on)."""
    remapper.tick()
    hid_out.flush()
    status_led.tick(active_profile().get("color"))


host.on_pump = _on_pump


# ---- optional status display (STEMMA QT OLED)
oled.log = debug
for _msg, _level in _early_log:        # what the display reported before debug() existed
    debug(_msg, _level)
_early_log = None
_oled_status = None


def device_name():
    """Name of the attached device: the page's alias, the name a receiver reports for
    its paired mouse, else the USB name."""
    d = host.devices[0]
    named = [r["device"] for r in battery.state if r.get("device")]
    return (config.get("aliases") or {}).get("%s:%s" % (d["vid"], d["pid"])) or (named[0] if named else d["name"])


def _oled_info():
    """What the display shows: device name, battery, profile, layer."""
    devs = host.devices
    name, level, charging, text = "No device", None, None, ""
    if devs:
        name = device_name()
        readings = ([r for r in battery.state if r.get("level") is not None] or
                    [r for r in battery.state if r.get("method") != "none"])
        text = "no battery info"
        if readings:
            r = readings[0]
            level, charging = r.get("level"), bool(r.get("charging"))
            text = ("%d%%" % level) if level is not None else r.get("text", "")
            text += (" chg" if charging else "") + (" (last)" if r.get("stale") else "")
    layers = ["Base"] + [l["name"] for l in active_profile().get("layers") or []]
    return {"name": name, "level": level, "charging": charging, "battery_text": text,
            "profile": (("L " if (remapper.latched or remapper._latches) else "") + active_profile()["name"] +
                        (" (held)" if _holds.base is not None else "")),   # "L": something is latched
            "layer": layers[min(_layer, len(layers) - 1)] + (" (held)" if _lholds else "")}


def _where(err):
    """' at file:line in function' of where an error happened (the innermost two places)."""
    try:
        import traceback
        text = "".join(traceback.format_exception(err))
        spots = [line.strip() for line in text.split("\n") if line.strip().startswith("File ")]
        return " at " + " < ".join(s.replace('File "', "").replace('", line ', ":").replace(", in ", " in ").split("/")[-1]
                                   for s in spots[-2:][::-1])
    except Exception:                     # (no traceback module, or no memory to format it)
        return ""


_job_errors = {}                         # background job -> when its last error was logged


def _job_failed(name, err):
    gc.collect()
    t = now_ms()
    if t - _job_errors.get(name, -60000) >= 60000:     # at most once a minute per job
        _job_errors[name] = t
        debug("%s: skipped after an error: %r%s (free memory now %d bytes)"
              % (name.capitalize(), err, _where(err), gc.mem_free()), "warn")


_oled_seen = [-1]       # profile / layer / holds / latches as one number, as last shown


def _oled_service():
    global _oled_status
    # what changes at the press of a button is checked every pass, as a plain number (no memory
    # used); the rest (device name, battery) every few seconds - building the text uses memory
    seen = (config["active"] * 64 + _layer) * 8 + (4 if _lholds else 0) + (2 if _holds.base is not None else 0) + \
        (1 if (remapper.latched or remapper._latches) else 0)
    if seen != _oled_seen[0]:
        _oled_seen[0] = seen
        oled.poke()
    oled.service(_oled_info)
    if oled.status != _oled_status:          # tell the page (and the log) when the display state changes
        _oled_status = oled.status
        link.send({"type": "oled", "status": oled.status})
        quiet = (oled.status == "not used yet" or getattr(oled, "display", None) is not None
                 or oled.status.startswith("display stopped"))    # (the display logs that itself)
        if not quiet:
            debug("Display: " + oled.status, "warn")   # (a working display logs itself)


def _pulse(kind, index):
    """Haptic pulse on a profile ("p") or layer ("l") change, as set in Settings."""
    h = config.get("haptic") or {}
    if not h.get("on", True):
        return
    wave = h.get(kind, -1)
    waves = haptics.state.get("waves") or []
    if h.get("d") and waves:              # a different pulse for each profile / layer
        wave = waves[index % len(waves)]
    haptics.play(wave)


# ======================================================================
#  USB host port bring-up
# ======================================================================
_power_pin = None


def start_host_port():
    global _power_pin
    power = getattr(board, "USB_HOST_5V_POWER", None)
    if power is not None:
        try:
            _power_pin = digitalio.DigitalInOut(power)
            _power_pin.switch_to_output(value=True)
            host.port_power = _port_power          # enables port restarts
        except Exception as err:        # pin owned by CircuitPython itself
            debug("USB-A port power switch not available to the firmware (%r): "
                  "automatic port restarts are off" % (err,), "warn")
    dp = getattr(board, "USB_HOST_DATA_PLUS", None)
    dm = getattr(board, "USB_HOST_DATA_MINUS", None)
    if dp is None or dm is None:
        return "this board has no USB_HOST_DATA_PLUS/MINUS pins"
    try:
        import usb_host
        usb_host.Port(dp, dm)           # returns the existing port if already started
    except Exception as err:
        return "usb_host.Port: %r (continuing; port may already be running)" % (err,)
    return None


# ======================================================================
#  Commands from the web page (dispatch table, no elif chains)
# ======================================================================
def info_payload():
    uname = os.uname()
    return {
        "type": "info",
        "fw": FW_VERSION,
        "board": getattr(board, "board_id", uname.machine),
        "link_port": link.port_name(),
        "data_port": usb_cdc.data is not None,
        "boot_errors": BOOT_ERRORS,
        "circuitpython": uname.release,
        "uptime_s": now_ms() // 1000,
        "mem_free": gc.mem_free(),
        "board_pins": list(board_buttons.codes) if board_buttons else [],
        "cpu_mhz": microcontroller.cpu.frequency // 1000000,
        "device_type": host.device_type(),
        "devices": host.devices,
        "hid_ready": hid_out.ready(),
        "msc_now": MSC_AT_BOOT,
        "msc_forced": MSC_FORCED,
        "msc_saved": config["msc"],
        "reboot_needed": reboot_needed(),
        "usb_now": USB_AT_BOOT,
        "usb_now_text": usb_label(USB_AT_BOOT),
        "usb_saved_text": usb_label(config["usb"]),
        "boot_options_now": {n: BOOT_VALUES.get(n) for n in settings_store.BOOT_OPTIONS},
        "boot_options_saved": {n: config[n] for n in settings_store.BOOT_OPTIONS},
        "usb_saved": config["usb"],
        "pedal_output": hid_out.pedal_dev is not None,
        "midi_output": hid_out.midi_out is not None,
        "pedal_latched": remapper.latched,
        "verbose": config["verbose"],
        "profiles": [p["name"] for p in config["profiles"]],
        "active": config["active"],
        "hold_base": _holds.base,
        "layer": _layer,
        "layers": ["Base"] + [l["name"] for l in active_profile().get("layers") or []],
        "layer_held": bool(_lholds),
        "battery": battery.describe(),
        "haptics": haptics.describe(),
        "mouse_light": mouse_light.describe() if mouse_light is not None else (
            "waiting for a Logitech or Razer mouse" if config.get("mouse_light") else "off"),
        "display": oled.describe(),
        "rules": rule_summary(active_profile()),
        "caps": host.capabilities(),
        "config_bytes": profile_bytes(),
        "nvm_capacity": device_info()["capacity"],
        "profiles_for": _dev["name"] or "no device yet",
        "held": remapper.held_count(),
        "port_power_control": host.port_power is not None,
        "status_led": (("%s -> LED %s" % (status_led.state, status_led.color)) if status_led.pin is not None
                       else "not available"),
    }


def ack(cmd, ok=True, **extra):
    msg = {"type": "ack", "cmd": cmd, "ok": ok}
    msg.update(extra)
    link.send(msg)


def cmd_hello(msg):
    link.peer = True
    link.send({"type": "hello", "fw": FW_VERSION, "board": getattr(board, "board_id", "?"),
               "port": link.port_name(), "data_port": usb_cdc.data is not None})
    gc.collect()
    debug("Web page connected on the %s port - device %s, firmware %s, CircuitPython %s, %d bytes free"
          % (link.port_name(), getattr(board, "board_id", "?"), FW_VERSION, os.uname().release,
             gc.mem_free()), "ok")
    if usb_cdc.data is None:
        debug("Second (data) serial port is missing: boot.py has not run or failed. "
              "Press RESET once and check boot_out.txt. Using the console port meanwhile.", "warn")
    for stamp, level, text in _history[:-1]:
        link.send({"type": "debug", "level": level, "msg": text, "t": stamp, "replay": True})
    send_config()
    send_devices()
    link.send({"type": "pedal_state", "latched": remapper.latched})
    _send_layer()
    battery.publish()
    haptics.publish()
    _publish_mouse_light()
    link.send({"type": "oled", "status": oled.status})


def cmd_info(msg):
    payload = info_payload()
    link.send(payload)
    debug("Info requested: connected device type = %s" % payload["device_type"])


def cmd_get_config(msg):
    send_config()


# ---- the set in pieces: no message has to hold the whole set (each has to fit the
# board's line buffer and one block of free memory). config_begin, then config_profile
# for each profile (its settings and layer names, no mappings), config_rules with batches
# of mappings, then config_end applies (and saves) it.
_pending = None                           # {"save", "active", "profiles": [packed profile or None]}


def cmd_config_begin(msg):
    global _pending
    count = msg.get("count")
    if not isinstance(count, int) or not 0 < count <= settings_store.MAX_PROFILES:
        ack("config_end", False, error="bad profile count")
        return
    _pending = {"save": bool(msg.get("save")), "active": msg.get("active", 0), "profiles": [None] * count}
    gc.collect()


def cmd_config_profile(msg):
    i, profile = msg.get("i"), msg.get("profile")
    if _pending is None or not isinstance(i, int) or not 0 <= i < len(_pending["profiles"]) or not isinstance(profile, dict):
        return
    _pending["profiles"][i] = profile


def cmd_config_rules(msg):
    """A batch of one table's mappings for profile i (j = -1) or its layer j."""
    i, j, table, rules = msg.get("i"), msg.get("j", -1), msg.get("t"), msg.get("r")
    if (_pending is None or not isinstance(i, int) or not 0 <= i < len(_pending["profiles"])
            or table not in settings_store.TABLES or not isinstance(rules, dict)):
        return
    profile = _pending["profiles"][i]
    layers = (profile or {}).get("layers")
    part = profile if j == -1 else (layers[j] if isinstance(layers, list) and isinstance(j, int) and 0 <= j < len(layers) else None)
    if not isinstance(part, dict):
        return
    if not isinstance(part.get(table), dict):
        part[table] = {}
    part[table].update(rules)


def cmd_config_end(msg):
    global _pending
    if _pending is None or None in _pending["profiles"]:
        _pending = None
        ack("config_end", False, error="the profiles arrived incomplete - click Apply again")
        return
    pending = _pending
    _apply_config(pending["profiles"], pending["active"], pending["save"])
    _pending = None                       # (kept until applied: a retry after a memory clean-up can use it)


def _apply_config(raw, active, save):
    """Use a profile set from the page (packed or not), and save it for the device if asked."""
    global config
    profiles = settings_store.store_profiles(raw)      # cleaned, kept packed (one profile unpacked at a time)
    raw = None
    profiles = profiles or config["profiles"]
    active = active if isinstance(active, int) and 0 <= active < len(profiles) else 0
    error = None
    try:
        if save:
            _save_profiles(profiles, active)
    except (ValueError, OSError) as err:
        error = str(err) if isinstance(err, ValueError) else "could not write the profile file (%r)" % (err,)
        debug(error, "error")
        if isinstance(err, ValueError) and "too much memory" in error:   # not used either: it wouldn't fit
            ack("config_end", False, error=error)
            return
        save = False                      # not written: still apply it, unsaved
    same_profile = active == config["active"]
    config = dict(config, profiles=profiles, active=active)
    _dev["dirty"] = not save
    _dev["saved"] = _dev["saved"] or save
    clear_holds()
    reset_layer(keep=same_profile)      # stay on the layer being tested if it still exists
    remapper.load(effective_profile())
    debug("Mapping %s: %d profile(s), active %s (%s), about %.1f of %d KB of memory"
          % (("saved for " + (_dev["name"] or "this device")) if save else "applied (not saved)",
             len(config["profiles"]), profile_label(), rule_summary(active_profile()),
             profile_bytes() / 1024, devprofiles.MEMORY_BUDGET // 1024), "ok")
    ack("config_end", error is None, saved=save, error=error)
    send_config()
    _send_layer()


def _save_profiles(profiles, active):
    """Write the attached device's profile file. Raises ValueError / OSError."""
    if _dev["blocked"]:
        raise ValueError("the saved profiles for %s couldn't be loaded, so they aren't overwritten - "
                         "unplug and re-plug the device to try again" % (_dev["name"] or "this device"))
    if not devprofiles.writable():
        raise ValueError("Update mode: the computer has the drive for this start-up, so profiles can't be "
                         "saved - restart the board when the new files are copied")
    _dev["name"] = device_name() if host.devices else _dev["name"]
    devprofiles.save(_dev["id"], profiles, active, _dev["name"])


def cmd_saved_devices(msg):
    """Devices with saved profiles, for 'Copy profiles from' and the 'Saved on the board' list."""
    gc.collect()
    link.send({"type": "saved_devices", "list": devprofiles.saved_list()})


def cmd_copy_profiles(msg):
    """Copy another device's saved set into the attached device's profiles. Applied, not saved."""
    source = msg.get("from")
    gc.collect()
    got = devprofiles.load(source)
    found, label = (got[:2] if got else None), "%s" % ((got[2] if got else "") or source)
    if not found:
        ack("copy_profiles", False, error="nothing saved for %r" % (source,))
        return
    _dev["dirty"] = True
    use_profiles(found[0], found[1], "Copied %s - press Save to keep them for %s"
                 % (label, _dev["name"] or "this device"))
    ack("copy_profiles", True)


def cmd_forget_device(msg):
    """Per-device mode: delete a device's profile file."""
    did = msg.get("id")
    if not devprofiles.writable() or devprofiles.file_id(did) is None:
        ack("forget_device", False, error="the board can't write the drive right now")
        return
    devprofiles.remove(did)
    if did == _dev["id"]:
        _dev["saved"] = False
        send_config()
    debug("Saved profiles for %s deleted" % did, "ok")
    ack("forget_device", True)
    cmd_saved_devices(msg)


def _save_flag(name, value, label):
    save_settings(**{name: bool(value)})
    debug("%s %s (saved)" % (label, ("disabled", "enabled")[int(config[name])]), "ok")


def cmd_set_boot_option(msg):
    name, value = msg.get("name"), msg.get("value")
    allowed = settings_store.BOOT_OPTIONS.get(name)
    if not allowed or value not in allowed:
        ack("set_boot_option", False, error="bad option %r=%r" % (name, value))
        return
    save_settings(**{name: value})
    debug("%s set to %s (saved) - takes effect after a reboot" % (name, value), "ok")
    ack("set_boot_option", True, reboot_needed=reboot_needed())


def cmd_set_usb(msg):
    """Devices presented to the computer and their names (saved; used after a reboot)."""
    usb = save_settings(usb=settings_store.clean_usb(dict(config["usb"], **(msg.get("usb") or {}))))["usb"]
    debug("USB devices after the next reboot: %s (saved)" % usb_label(usb), "ok")
    if usb["pedal"] and not (usb["kbd"] or usb["mouse"] or usb["media"]):
        debug("Only the foot pedal: after the reboot keyboard, mouse and media actions do nothing. "
              "Recovery: D5 to GND + RESET.", "warn")
    if not (usb["kbd"] or usb["mouse"]):
        debug("No keyboard and no mouse: after the reboot the computer gets no clicks or key presses "
              "from this board (recovery: D5 to GND + RESET).", "warn")
    ack("set_usb", True, reboot_needed=reboot_needed())


def cmd_set_msc(msg):
    _save_flag("msc", msg.get("enabled", True), "Mass storage (CIRCUITPY drive)")
    debug("Mass storage change takes effect after a reboot", "warn")
    ack("set_msc", True, reboot_needed=reboot_needed())


def cmd_set_board_buttons(msg):
    """Watch the board's free pins for buttons (or free them for other add-ons): saved, applied at once."""
    _save_flag("board_buttons", msg.get("enabled", True), "Board buttons")
    _board_buttons_apply()
    ack("set_board_buttons", True, pins=list(board_buttons.codes) if board_buttons else [])


def cmd_set_verbose(msg):
    _save_flag("verbose", msg.get("enabled", False), "Verbose event logging")
    ack("set_verbose", True)


def cmd_reset_config(msg):
    profiles = settings_store.empty_profiles()
    saved = True
    try:
        _save_profiles(profiles, 0)
    except (ValueError, OSError) as err:
        saved = False
        debug("Profiles erased but not saved: %s" % (err,), "warn")
    _dev["dirty"] = not saved
    _dev["saved"] = _dev["saved"] or saved
    use_profiles(profiles, 0, "All profiles and mappings %s erased - every button/key passes through"
                 % ("of " + (_dev["name"] or "this device")))
    ack("reset_config", True)


def cmd_update_mode(msg):
    """Restart once with the drive editable by the computer (to copy new firmware files)."""
    settings_store.request_update_boot()
    debug("Restarting in Update mode: the CIRCUITPY drive will be editable by the computer until the "
          "next restart", "warn")
    cmd_reboot(msg)


def cmd_reboot(msg):
    debug("Rebooting now...", "warn")
    ack("reboot", True)
    remapper.release_all()
    time.sleep(0.3)                     # let the messages leave
    microcontroller.reset()


def cmd_restart_port(msg):
    ok = host.restart_port()
    ack("restart_port", ok)


def cmd_set_led(msg):
    """Status LED settings; applied at once, saved when msg["save"] is true."""
    global config
    updated = dict(config)
    for key in ("led_brightness", "led_balance", "led_gamma"):
        if key in msg:
            updated[key] = msg[key]
    updated = settings_store.sanitize(updated)
    if msg.get("save"):
        updated = store_settings(updated)
        debug("Status LED: brightness %d, balance R%d G%d B%d %%, gamma %s (saved)"
              % ((updated["led_brightness"],) + tuple(updated["led_balance"]) + (("off", "on")[updated["led_gamma"]],)), "ok")
    config = dict(config, led_brightness=updated["led_brightness"], led_balance=updated["led_balance"],
                  led_gamma=updated["led_gamma"])
    status_led.configure(config["led_brightness"], config["led_balance"], config["led_gamma"])
    ack("set_led", True, saved=bool(msg.get("save")), led=list(status_led.led_value(status_led._shown)))


def cmd_set_mouse_light(msg):
    """Match the mouse's RGB lighting to the profile color ({"enabled"} and / or {"level": 5-100 %}):
    saved, applied at once."""
    if "enabled" in msg:
        _save_flag("mouse_light", msg["enabled"], "Mouse lighting")
    if "level" in msg:
        save_settings(mouse_light_level=msg["level"])
    if mouse_light is not None:
        mouse_light.set_enabled(config["mouse_light"])     # off: gives the mouse its own lighting back
        mouse_light.set_level(config["mouse_light_level"])
        if not config["mouse_light"]:
            _unload_mouse_light()
    _publish_mouse_light()
    ack("set_mouse_light", True)


def _unload_mouse_light():
    """Turned off: drop the lighting code, so its memory (about 5 KB) is free for everything else."""
    global mouse_light, _light_check_at
    mouse_light = None
    _light_check_at = 0
    for name in ("mouselight", "light_razer"):
        sys.modules.pop(name, None)
    before = gc.mem_free()
    gc.collect()
    debug("Mouse lighting code unloaded (%d bytes free now, %d before)" % (gc.mem_free(), before), "info")


def cmd_haptic_play(msg):
    haptics.play(msg.get("wave"))


def cmd_haptic_strength(msg):
    haptics.set_strength(msg.get("enabled", True), msg.get("intensity", 50))


def cmd_set_haptic(msg):
    """Pulses on profile / layer changes: {"on", "p", "l", "d"} - saved."""
    current = dict(config.get("haptic") or {})
    current.update({k: msg[k] for k in ("on", "p", "l", "d") if k in msg})
    h = save_settings(haptic=current)["haptic"]
    debug("Haptic pulses on profile / layer change: %s (profile %d, layer %d%s) - saved"
          % (("off", "on")[h["on"]], h["p"], h["l"], (", different for each", "")[not h["d"]]), "ok")
    ack("set_haptic", True)


def cmd_set_oled(msg):
    """Status display rotation: saved, applied at once."""
    save_settings(oled={"rot": msg.get("rot", 0)})
    oled.configure(config["oled"]["rot"])
    debug("Display rotation %d (saved)" % config["oled"]["rot"], "ok")
    ack("set_oled", True)


def cmd_set_alias(msg):
    """A name given to a device on the page ("Show this device as"), kept for the display."""
    key, name = str(msg.get("id", ""))[:13], str(msg.get("name") or "")
    aliases = dict(config.get("aliases") or {})
    if name.strip():
        aliases[key] = name
    else:
        aliases.pop(key, None)
    try:
        save_settings(aliases=aliases)
    except ValueError as err:
        debug(str(err), "error")
        ack("set_alias", False, error=str(err))
        return
    ack("set_alias", True)


def cmd_led_preview(msg):
    """The page's color picker: show the color now, without saving anything."""
    color = settings_store.clean_color(msg.get("color"), 0)
    status_led.preview(color)




def cmd_read_battery(msg):
    if not host.targets:
        debug("Battery: nothing is plugged into the USB-A port", "warn")
        ack("read_battery", False, error="no device")
        return
    debug("Reading the battery level (requests and replies follow)...")
    battery.check(raw=True, fresh=True)
    for line in battery.describe():
        debug("Battery: " + line, "ok")
    ack("read_battery", True)


def cmd_usb_scan(msg):
    host.scan_report(debug)
    ack("usb_scan", True)


def cmd_set_active(msg):
    switch_profile(msg.get("index", 0))
    ack("set_active", True, index=config["active"])


def cmd_ping(msg):
    ack("ping", True, t=now_ms())


SLOW_MS = 25             # the main loop's other jobs run at most this often (see main())
_meter = [None]          # the running "Measure smoothness", if any
MEASURE_PARTS = ("page link", "USB devices", "mouse/keyboard reports", "board buttons",
                 "battery", "haptics", "mouse lighting", "display", "rest")


def cmd_measure(msg):
    """Measure for a few seconds where the main loop's time goes (see measure.py)."""
    seconds = msg.get("seconds", 10)
    if not isinstance(seconds, int) or seconds < 2 or seconds > 60:
        seconds = 10
    import measure
    gc.collect()
    _meter[0] = measure.Meter(MEASURE_PARTS, seconds, host, hid_out)
    debug("Measuring smoothness for %d s - use the mouse as usual meanwhile..." % seconds, "info")
    ack("measure", True, seconds=seconds)


def _measured(meter):
    """Log the measurement, then drop the measuring code."""
    try:
        gc.collect()
        for line in meter.report():
            debug(line, "ok")
    except MemoryError:
        host.timing = False
        gc.collect()
        debug("Smoothness: not enough free memory to show the result (%d bytes free)" % gc.mem_free(), "warn")
    sys.modules.pop("measure", None)
    gc.collect()


def cmd_unknown(msg):
    debug("Unknown command: %r" % (msg.get("cmd"),), "warn")
    ack(str(msg.get("cmd")), False, error="unknown command")


COMMANDS = {
    "hello": cmd_hello,
    "info": cmd_info,
    "get_config": cmd_get_config,
    "config_begin": cmd_config_begin,
    "config_profile": cmd_config_profile,
    "config_rules": cmd_config_rules,
    "config_end": cmd_config_end,
    "saved_devices": cmd_saved_devices,
    "copy_profiles": cmd_copy_profiles,
    "forget_device": cmd_forget_device,
    "set_msc": cmd_set_msc,
    "set_board_buttons": cmd_set_board_buttons,
    "set_mouse_light": cmd_set_mouse_light,
    "set_usb": cmd_set_usb,
    "set_boot_option": cmd_set_boot_option,
    "set_verbose": cmd_set_verbose,
    "reset_config": cmd_reset_config,
    "reboot": cmd_reboot,
    "update_mode": cmd_update_mode,
    "ping": cmd_ping,
    "set_active": cmd_set_active,
    "usb_scan": cmd_usb_scan,
    "read_battery": cmd_read_battery,
    "test_mode": cmd_test_mode,
    "restart_port": cmd_restart_port,
    "set_led": cmd_set_led,
    "led_preview": cmd_led_preview,
    "set_oled": cmd_set_oled,
    "set_alias": cmd_set_alias,
    "haptic_play": cmd_haptic_play,
    "haptic_strength": cmd_haptic_strength,
    "set_haptic": cmd_set_haptic,
    "measure": cmd_measure,
}


def dispatch(msg):
    """Run one command from the page. It always gets an answer: when memory is short for a
    moment it is tried once more after a clean-up, then reported to the page as failed."""
    if not isinstance(msg, dict):
        return
    cmd = msg.get("cmd")
    handler = COMMANDS.get(cmd, cmd_unknown)
    for attempt in (0, 1):
        try:
            handler(msg)
            return
        except MemoryError as err:
            gc.collect()
            if attempt == 0:
                continue
            problem = err
        except Exception as err:
            gc.collect()
            problem = err
        break
    debug("Command %s failed: %r%s (free memory now %d bytes)" % (cmd, problem, _where(problem), gc.mem_free()), "error")
    ack(cmd, False, error=("not enough free memory just now - try again in a moment"
                           if isinstance(problem, MemoryError) else "failed: %r" % (problem,)))


# ======================================================================
#  Main loop
# ======================================================================
def _stale_sources():
    """.py files next to their .mpy: the board loads the .py (older) one instead."""
    try:
        names = os.listdir("/")
    except OSError:
        return []
    return [n for n in names if n.endswith(".py") and n not in ("code.py", "boot.py") and n[:-3] + ".mpy" in names]


def main():
    stale = _stale_sources()
    if stale:
        debug("Delete these old files from CIRCUITPY: %s - the board uses them instead of the new .mpy "
              "files" % ", ".join(stale), "error")
    if usb_cdc.data is None:
        debug("usb_cdc.data is not enabled - press RESET once so boot.py runs "
              "(the page can still connect on the console port).", "warn")
    for line in BOOT_ERRORS:
        debug("boot.py: " + line, "error")
    if BOARD_BUTTONS_ERROR:
        debug(BOARD_BUTTONS_ERROR, "warn")
    try:
        _board_buttons_apply()
    except Exception as err:          # never stop the remapper over the extra buttons
        debug("Board buttons: %r" % (err,), "warn")
    for line in BATTERY_ERRORS:
        debug("Battery: " + line, "warn")
    _mark("rest of start-up")
    debug("Free memory after start-up: %d bytes" % gc.mem_free(), "info")
    debug("Memory used at start-up: " + ", ".join("%s %.1f KB" % (n, b / 1024) for n, b in _MEM if b > 0), "info")
    if UPDATE_MODE:
        debug("Update mode: the CIRCUITPY drive is editable by the computer for this start-up - copy the "
              "new files, then restart the board. Profiles can't be saved meanwhile.", "warn")
    status_led.update(False)          # red until an input device is attached
    problem = start_host_port()
    if problem:
        debug(problem, "warn")
    debug("Remapper firmware %s started - %d profile(s), active %s (%s)"
          % (FW_VERSION, len(config["profiles"]), profile_label(), rule_summary(active_profile())), "ok")
    link.send({"type": "boot", "fw": FW_VERSION})   # page re-sends hello after a reload

    was_connected = False
    # jobs that run in the background: an error in one (e.g. memory too fragmented for a moment)
    # is logged and skipped - it must not release held buttons or stop the remapping
    background = (("battery", lambda: battery.service()), ("haptics", lambda: haptics.service()),
                  ("mouse lighting", _mouse_light_service),
                  ("display", _oled_service))
    # Every pass reads the mouse and passes its movement on; everything else (the page link, plugging,
    # the background jobs, the status LED...) runs at most every SLOW_MS: a pass that does less is
    # shorter, so the mouse is read more often.
    slow_at = 0
    while True:
        try:
            meter = _meter[0]                  # "Measure smoothness" running (None nearly always)
            if meter is not None and meter.loop():
                _meter[0] = None
                _measured(meter)
                meter = None
            now = now_ms()
            if now >= slow_at:
                slow_at = now + SLOW_MS
                connected = link.active is not None and link.active.port.connected
                if was_connected and not connected:
                    link.peer = False
                was_connected = connected
                for msg in link.poll():
                    dispatch(msg)
                if meter is not None:
                    meter.mark(0)
                host.service()
                if meter is not None:
                    meter.mark(1)
                part = 4
                for name, job in background:
                    try:
                        job()
                    except Exception as err:
                        _job_failed(name, err)
                    if meter is not None:
                        meter.mark(part)
                    part += 1
                _update_test_mode()            # ends test mode if the page stopped renewing it
                _remember_active()
                status_led.tick(active_profile().get("color"))
                if meter is not None:
                    meter.mark(8)
            host.poll(remapper)
            if meter is not None:
                meter.mark(2)
            if board_buttons is not None:
                board_buttons.poll(remapper)
            if meter is not None:
                meter.mark(3)
            remapper.tick()
            hid_out.flush()
            if meter is not None:
                meter.mark(8)
            if not host.endpoints:
                time.sleep(0.01)
        except Exception as err:        # keep running no matter what
            gc.collect()
            debug("Main loop error: %r%s (free memory now %d bytes)" % (err, _where(err), gc.mem_free()), "error")
            try:
                remapper.release_all()
            except Exception:
                pass
            time.sleep(0.2)


main()
