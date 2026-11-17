# battery_base.py - shared parts of the battery readers (see battery.py), and the
# standard reader: a battery field declared in the HID report descriptor (Generic Device
# Controls "Battery Strength", Battery System page). The readers only ask questions;
# haptics.py is the one part that changes a device setting (its haptic strength).
#
# Readers and parsers use dispatch tables instead of if/elif chains.


import hidparse

from clock import now_ms          # noqa: F401  (battery.py and the readers import it from here)

POLL_MS = 5 * 60 * 1000      # automatic check every 5 minutes
FIRST_DELAY_MS = 5000        # after a device attaches (wireless links need a moment)
EARLY_RETRIES_MS = (15000, 30000, 60000, 60000, 60000)   # when a device that should answer didn't
WAKE_GAP_MS = 5000           # an asleep device that sends input is re-checked, at most this often
RETRY_MS = 60 * 1000         # check again sooner while a wireless device sleeps

RAZER_VID = 0x1532
LOGITECH_VID = 0x046D
KEYCHRON_VID = 0x3434


def hexstr(data):
    return " ".join("%02x" % b for b in data)


# ============================================================ HID descriptor facts
REPORT_BYTES_MAX = 64        # a full-speed HID report never needs more


def scan(iface):
    """{"fields", "vendor_ids", "sizes"} of an interface's report descriptor (hidparse.py):
    battery fields, report IDs on vendor pages, (type, report ID) -> bits. Worked out
    once per interface and kept, since every reader asks."""
    info = iface.get("scan")
    if info is None:
        try:
            info = hidparse.parse_full(iface.get("desc") or b"")[2]
        except Exception:                   # unreadable descriptor: nothing to use
            info = {"fields": [], "vendor_ids": set(), "sizes": {}}
        iface["scan"] = info
    return info


def report_bytes(info, rtype, rid, default):
    """Bytes of one report (without its ID byte), at most REPORT_BYTES_MAX."""
    bits = info["sizes"].get((rtype, rid))
    return min((bits + 7) // 8, REPORT_BYTES_MAX) if bits else default


# ============================================================ result helpers
def result(method, level=None, charging=None, status=None, approx=False, device=None, text=None):
    out = {"method": method, "level": level, "charging": charging, "status": status,
           "approx": approx, "device": device}
    out["text"] = text or (("%d %%" % level) if level is not None else "unknown")
    return out


# ============================================================ reader: standard HID
class StandardReader:
    name = "HID battery field"

    def __init__(self, mon, target):
        self.mon = mon
        self.t = target
        self.fields = []
        for iface in target["interfaces"]:
            info = scan(iface)
            for f in info["fields"]:
                size = report_bytes(info, f["type"], f["rid"], 1)
                self.fields.append(dict(f, iface=iface["number"], bytes=size + (1 if f["rid"] else 0)))

    def usable(self):
        return any(f["what"] == "level" for f in self.fields)

    def read(self):
        values = {}
        for f in self.fields:
            buf = bytearray(max(f["bytes"], 1))
            got = self.mon.ctrl(self.t, 0xA1, 0x01, (f["type"] << 8) | f["rid"], f["iface"], buf)
            if got is None:
                continue
            payload = buf[1:] if f["rid"] else buf
            raw = hidparse.get_bits(payload, f["offset"], f["size"]) or 0
            span = f["max"] - f["min"]
            values[f["what"]] = ((raw - f["min"]) * 100 // span) if span > 0 else raw
        if "level" not in values:
            return None
        level = max(0, min(100, values["level"]))
        charging = bool(values["charging"]) if "charging" in values else None
        return result(self.name, level, charging, ("discharging", "charging")[bool(charging)] if charging is not None else None)
