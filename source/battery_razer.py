# battery_razer.py - battery reader for Razer devices (90-byte feature reports), loaded by
# battery.py only when a Razer device is plugged in.
from battery_base import result, hexstr, RAZER_VID

# ============================================================ reader: Razer
RAZER_TRANSACTIONS = (0x1F, 0x3F, 0xFF, 0x9F, 0x08)
RAZER_STATUS = {0x00: "new", 0x01: "busy", 0x02: "ok", 0x03: "failed", 0x04: "no response", 0x05: "not supported"}


def razer_request(tid, cls, cmd, size=0x02):
    report = bytearray(90)
    report[1] = tid
    report[5] = size
    report[6] = cls
    report[7] = cmd
    crc = 0
    for b in report[2:88]:
        crc ^= b
    report[88] = crc
    return report


class RazerReader:
    name = "Razer"

    def __init__(self, mon, target):
        self.mon = mon
        self.t = target
        numbers = sorted(i["number"] for i in target["interfaces"])
        self.ifaces = numbers or [0]
        self.found = None                   # (interface, transaction id) that answered
        self.last = None                    # ...and the one that answered before (kept when found is reset)

    def usable(self):
        return self.t["vid"] == RAZER_VID

    def _query(self, iface, tid, cls, cmd):
        req = razer_request(tid, cls, cmd)
        if self.mon.ctrl(self.t, 0x21, 0x09, 0x0300, iface, req) is None:
            return None
        for _attempt in range(6):
            self.mon.wait(15)
            buf = bytearray(90)
            if self.mon.ctrl(self.t, 0xA1, 0x01, 0x0300, iface, buf) is None:
                return None
            status = buf[0]
            self.mon.raw("Razer if%d tid %02x cmd %02x:%02x -> %s (%s)"
                         % (iface, tid, cls, cmd, hexstr(buf[:12]), RAZER_STATUS.get(status, "?")))
            if status == 0x01:              # busy: ask again
                continue
            if status == 0x02 and buf[6] == cls and buf[7] == cmd:
                return buf
            return None
        return None

    def read(self):
        if self.found:
            tries = [self.found]
        else:                               # the channel that answered last time first (each miss is ~90 ms)
            tries = [(i, t) for i in self.ifaces for t in RAZER_TRANSACTIONS]
            if self.last in tries:
                tries.remove(self.last)
                tries.insert(0, self.last)
        for iface, tid in tries:
            reply = self._query(iface, tid, 0x07, 0x80)
            if reply is None:
                continue
            self.found = self.last = (iface, tid)
            level = (reply[9] * 100 + 127) // 255
            charge = self._query(iface, tid, 0x07, 0x84)
            charging = bool(charge[9]) if charge is not None else None
            status = None if charging is None else ("discharging", "charging")[charging]
            return result(self.name, level, charging, status)
        self.found = None
        return None
