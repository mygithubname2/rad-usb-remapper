# battery_keychron.py - battery probe for Keychron devices, loaded by battery.py only when
# a Keychron device is plugged in.
from battery_base import result, hexstr, scan, report_bytes, KEYCHRON_VID

# ============================================================ reader: Keychron (probe)
# Keychron's battery protocol is not public. Stock firmware is not known to report the
# battery through the Keychron Link dongle, so this reader asks a few READ-ONLY questions
# on the QMK/VIA raw-HID channel (usage page 0xFF60) and logs every answer:
#   0x01 VIA protocol version, 0xA0 Keychron protocol version, 0xA1 firmware version,
#   0xA2 supported features, 0xA4 battery (added by community firmware, e.g. for the
#   K10 HE: [0xA4, percent, mV lo, mV hi, 0 battery / 1 charging / 2 full, link, model])
# Such firmware may also push 0xA4 reports by itself every couple of seconds.
VIA_PAGE = b"\x06\x60\xff"           # Usage Page (0xFF60) item in a report descriptor
KC_QUERIES = ((0x01, "VIA protocol"), (0xA0, "Keychron protocol"), (0xA1, "firmware version"),
              (0xA2, "supported features"))
KC_BATTERY = 0xA4
KC_CHARGING = {0: "discharging", 1: "charging", 2: "full"}


class KeychronReader:
    name = "Keychron raw HID"
    speculative = True                  # failing is expected: no quick automatic retries

    def __init__(self, mon, target):
        self.mon = mon
        self.t = target
        self.iface = None
        self.rid = 0                    # report ID of the raw-HID reports (QMK uses none)
        self.out_len = 32
        for iface in target["interfaces"]:
            desc = iface.get("desc") or b""
            if VIA_PAGE in desc:
                self.iface = iface
                info = scan(iface)
                self.rid = max(info["vendor_ids"]) if info["vendor_ids"] else 0
                self.out_len = report_bytes(info, 2, self.rid, 32)    # output report, capped at 64
                break
        self.probed = False
        self.silent = False             # nothing ever answered: skip automatic checks
        self.via = "?"

    def usable(self):
        return (self.t["vid"] == KEYCHRON_VID and self.iface is not None
                and (self.mon._raw or not self.silent))

    def _ask(self, cmd, timeout_ms):
        start = 1 if self.rid else 0
        msg = bytearray(self.out_len + start)
        msg[0] = self.rid
        msg[start] = cmd
        out_ep = self.iface.get("out_endpoint")
        senders = (("OUT endpoint %02x" % out_ep, lambda: self.mon.write(self.t, out_ep, msg))
                   if out_ep is not None else None,
                   ("SET_REPORT", lambda: self.mon.ctrl(self.t, 0x21, 0x09, 0x0200 | self.rid,
                                                        self.iface["number"], msg) is not None))
        self.via = "none"
        for how, send in [s for s in senders if s]:
            if send():
                self.via = how
                break
        if self.via == "none":
            return None
        return self._listen(lambda p: p if p[0] in (cmd, 0xFF) else None, timeout_ms)

    def _listen(self, match, timeout_ms):
        def strip(data):                # drop the report ID, then let `match` look at the payload
            payload = bytes(data[1:]) if self.rid and len(data) and data[0] == self.rid else bytes(data)
            return match(payload) if payload else None
        return self.mon.await_report(self.t, self.iface, strip, timeout_ms)

    def _battery(self, reply):
        if not reply or reply[0] != KC_BATTERY or len(reply) < 5 or reply[1] > 100:
            return None
        charge = KC_CHARGING.get(reply[4])
        model = reply[6] if len(reply) > 6 else 0
        return result(self.name, reply[1], reply[4] in (1, 2), charge,
                      device=("Keychron model %d" % model) if model else None)

    def read(self):
        raw = self.mon._raw
        if raw:
            i = self.iface
            self.mon.raw("Keychron raw HID: if%d, IN ep %02x, OUT ep %s, report ID %d, %d-byte requests"
                         % (i["number"], i["endpoint"], ("%02x" % i["out_endpoint"]) if "out_endpoint" in i else "none",
                            self.rid, self.out_len))
            self.mon.raw("Keychron if%d report descriptor: %s" % (i["number"], hexstr(i.get("desc") or b"")))
        answered = False
        if raw or not self.probed:          # log what the dongle / keyboard answers
            self.probed = True
            for cmd, what in KC_QUERIES:
                reply = self._ask(cmd, 300)
                answered = answered or bool(reply)
                self.mon.raw("Keychron %02x (%s) sent by %s -> %s" % (cmd, what, self.via, hexstr(reply[:16]) if reply else "no answer"
                             ) + (" = not supported" if reply and reply[0] == 0xFF else ""))
        reply = self._ask(KC_BATTERY, 300)
        answered = answered or bool(reply)
        self.mon.raw("Keychron a4 (battery) sent by %s -> %s" % (self.via, hexstr(reply[:8]) if reply else "no answer"))
        found = self._battery(reply)
        if found is None:                   # firmware that pushes the battery by itself
            pushed = self._listen(lambda d: bytes(d) if len(d) > 4 and d[0] == KC_BATTERY else None,
                                  (300, 2500)[raw])
            if raw:
                self.mon.raw("Keychron pushed battery report -> %s" % (hexstr(pushed[:8]) if pushed else "none"))
            found = self._battery(pushed)
            answered = answered or bool(pushed)
        self.silent = not answered          # automatic checks stop asking a dongle that never answers
        return found
