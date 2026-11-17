# battery_logitech.py - battery reader for Logitech devices (HID++ 2.0), loaded by battery.py
# only when a Logitech device is plugged in. haptics.py reuses its connection.
from battery_base import result, scan, LOGITECH_VID

# ============================================================ reader: Logitech HID++
HIDPP_SHORT, HIDPP_LONG = 0x10, 0x11
# HID++ 1.0 error codes a receiver answers for a device index (logged in raw mode)
HIDPP10_ERRORS = {0x01: "invalid sub-ID (not a HID++ 2.0 device)", 0x02: "invalid address", 0x03: "invalid value",
                  0x04: "connection failed (paired device not reachable: asleep, off, or on another channel)",
                  0x05: "too many devices", 0x07: "busy", 0x08: "unknown device (empty slot)",
                  0x09: "resource error (nothing reachable in this slot)", 0x0A: "request unavailable",
                  0x0B: "invalid parameter"}
ERR_CONNECT_FAIL = 0x04
WAKE_MS = 700                # pause before asking a paired-but-unreachable device again
REPLY_MS = 1000              # devices paired to a Bolt receiver can take a while to wake
SEND_TRIES = 4
DRAIN_MS = 30                # time spent reading leftover replies before a request
# Software ID (low 4 bits of byte 3): echoed in the reply. A different one for every
# request, so a late or duplicated reply to an EARLIER request (a "refused" request may
# still have reached the receiver) can never be taken as the answer to a newer one.
SW_IDS = 15
FEAT_NAME, FEAT_UNIFIED, FEAT_STATUS, FEAT_VOLTAGE = 0x0005, 0x1004, 0x1000, 0x1001
FEAT_HAPTIC = 0x19B0         # haptic motor (MX Master 4): used by haptics.py
UNIFIED_CHARGING = {0: "discharging", 1: "charging", 2: "charging (slow)", 3: "full", 4: "charge error"}
UNIFIED_LEVELS = {8: "full", 4: "good", 2: "low", 1: "critical"}
STATUS_CHARGING = {0: "discharging", 1: "charging", 2: "almost full", 3: "full", 4: "charging (slow)",
                   5: "battery error", 6: "battery error"}
# approximate Li-ion discharge curve: (millivolts, percent)
VOLTAGE_CURVE = ((4186, 100), (4067, 90), (3989, 80), (3922, 70), (3859, 60), (3811, 50),
                 (3778, 40), (3751, 30), (3717, 20), (3671, 10), (3646, 5), (3579, 2), (3500, 0))


def voltage_percent(mv):
    for (hi_mv, hi_pct), (lo_mv, lo_pct) in zip(VOLTAGE_CURVE, VOLTAGE_CURVE[1:]):
        if mv >= lo_mv:
            return min(100, lo_pct + (min(mv, hi_mv) - lo_mv) * (hi_pct - lo_pct) // (hi_mv - lo_mv))
    return 0


class LogitechReader:
    name = "Logitech HID++"

    def __init__(self, mon, target):
        self.mon = mon
        self.t = target
        self.iface = None
        for iface in target["interfaces"]:
            vendor_ids = scan(iface)["vendor_ids"]
            if HIDPP_LONG in vendor_ids or HIDPP_SHORT in vendor_ids:
                self.iface = iface
                self.long_only = HIDPP_SHORT not in vendor_ids
                self.has_long = HIDPP_LONG in vendor_ids
                break
        self._sw = 0                        # last software ID used
        self._unread = False                # replies may be waiting unread in the receiver
        self.slots = None                   # {device index: {"name", "feat": (index, feature id)}}

    def usable(self):
        return self.t["vid"] == LOGITECH_VID and self.iface is not None

    def _build(self, index, feat_index, fn, params):
        # short (7-byte) requests when they fit, like Logitech's tools; long ones otherwise
        long = self.has_long and (len(params) > 3 or self.long_only)
        rid = (HIDPP_SHORT, HIDPP_LONG)[long]
        msg = bytearray((7, 20)[long])
        msg[0] = rid
        msg[1] = index
        msg[2] = feat_index
        self._sw = self._sw % SW_IDS + 1        # 1..15, never 0 (0 = device notification)
        msg[3] = (fn << 4) | self._sw
        for i, p in enumerate(params[:len(msg) - 4]):
            msg[4 + i] = p
        return msg, rid

    def _drain(self):
        """Read and discard replies nobody waited for. A receiver only holds a couple of
        unread replies; with its buffer full it refuses new requests ("pipe error")."""
        self.mon.await_report(self.t, self.iface, lambda data: None, DRAIN_MS)
        self._unread = False

    def _send(self, index, feat_index, fn, params=(), timeout=None):
        if self._unread:
            self._drain()
        msg, rid = self._build(index, feat_index, fn, params)
        if not self._transmit(msg, rid):
            self._unread = True             # the request may still have arrived
            return None
        want = (index, feat_index, (fn << 4) | self._sw)

        def match(data):
            if len(data) < 5 or data[0] not in (HIDPP_SHORT, HIDPP_LONG) or data[1] != index:
                return None
            if (data[1], data[2], data[3]) == want:
                return ("ok", bytes(data[4:]))
            if data[2] in (0x8F, 0xFF) and data[3] == feat_index and data[4] == want[2]:
                return ("error", bytes(data[5:6]))
            return None

        reply = self.mon.await_report(self.t, self.iface, match, timeout or REPLY_MS)
        if reply is None:
            self._unread = True             # a late reply may still come: clear it out first next time
        meaning = ""
        if reply and reply[0] == "error" and reply[1]:
            meaning = " = " + HIDPP10_ERRORS.get(reply[1][0], "error %d" % reply[1][0])
        self.mon.raw("HID++ dev %02x feat %02x fn %d -> %s%s" % (index, feat_index, fn, reply, meaning))
        return reply

    def _transmit(self, msg, rid):
        """Send one HID++ request. The interrupt OUT endpoint is used when the
        interface has one (like Logitech's software does), else SET_REPORT. A busy
        receiver may refuse a request (USB "pipe error"), so try a few times."""
        out_ep = self.iface.get("out_endpoint")
        for attempt in range(SEND_TRIES):
            if attempt:
                self.mon.wait(30 * attempt)
            if out_ep is not None and self.mon.write(self.t, out_ep, msg):
                return True
            if self.mon.ctrl(self.t, 0x21, 0x09, 0x0200 | rid, self.iface["number"], msg) is not None:
                return True
            self.mon.raw("HID++ request to dev %02x refused, %s"
                         % (msg[1], ("trying again", "giving up")[attempt == SEND_TRIES - 1]))
        return False

    def _feature(self, index, feature_id):
        reply = self._send(index, 0x00, 0, (feature_id >> 8, feature_id & 0xFF))
        if not reply or reply[0] != "ok" or not reply[1][0]:
            return None
        return reply[1][0]

    def _name(self, index):
        feat = self._feature(index, FEAT_NAME)
        if feat is None:
            return None
        reply = self._send(index, feat, 0)
        if not reply or reply[0] != "ok":
            return None
        length = reply[1][0]
        name = bytearray()
        while len(name) < length:
            part = self._send(index, feat, 1, (len(name),))
            if not part or part[0] != "ok":
                break
            chunk = part[1][:length - len(name)]
            if not chunk:
                break
            name.extend(chunk)
        try:
            text = bytes(name).decode().strip("\x00 ")
        except UnicodeError:
            return None
        # a device name is readable text; anything else is a mixed-up reply: ask again later
        if len(text) < 2 or any(ord(ch) < 32 or ord(ch) > 126 for ch in text):
            self.mon.raw("HID++ dev %02x: name %r doesn't look like a name - ignored" % (index, text))
            return None
        return text

    def _ping(self, index):
        """'ok', 'asleep' (paired but not reachable) or None (nothing there)."""
        for attempt in range(2):
            reply = self._send(index, 0x00, 1, (0, 0, 0x5A))
            if reply and reply[0] == "ok":
                return "ok"
            asleep = bool(reply) and reply[0] == "error" and reply[1][:1] == bytes((ERR_CONNECT_FAIL,))
            if not asleep:
                return None
            if not attempt:                 # the first request may have woken it up
                self.mon.wait(WAKE_MS)
        return "asleep"

    def _probe(self, index):
        state = self._ping(index)
        if state is None:
            return None
        if state == "asleep":
            return {"name": None, "feat": None, "asleep": True}
        feat = None
        for fid in (FEAT_UNIFIED, FEAT_STATUS, FEAT_VOLTAGE):
            fidx = self._feature(index, fid)
            if fidx is not None:
                feat = (fidx, fid)
                break
        slot = {"name": self._name(index), "feat": feat, "asleep": False}
        haptic = self._feature(index, FEAT_HAPTIC)
        if haptic is not None:                  # a haptic motor: which waveforms it can play
            caps = self._send(index, haptic, 0)
            mask = 0
            if caps and caps[0] == "ok" and len(caps[1]) >= 8:
                mask = (caps[1][4] << 24) | (caps[1][5] << 16) | (caps[1][6] << 8) | caps[1][7]
            slot["haptic"] = haptic
            slot["waves"] = [n for n in range(32) if mask & (1 << n)]
        return slot

    def _discover(self):
        self.slots = {}
        for index in [0xFF] + list(range(1, 7)):
            slot = self._probe(index)
            if slot is not None:
                self.slots[index] = slot
                if index == 0xFF:
                    break                   # a corded device answers for itself

    def _battery(self, index, slot):
        if slot.get("asleep"):
            return dict(result(self.name, device=slot["name"] or "paired device %d" % index,
                               text="asleep or out of range"), retry=True)
        if slot["feat"] is None:
            return result(self.name, device=slot["name"], text="not reported")
        fidx, fid = slot["feat"]
        reply = self._send(index, fidx, (0, 1)[fid == FEAT_UNIFIED])
        if not reply or reply[0] != "ok":
            return dict(result(self.name, device=slot["name"], text="asleep or out of range"), retry=True)
        p = reply[1]
        return {FEAT_UNIFIED: self._unified, FEAT_STATUS: self._status, FEAT_VOLTAGE: self._voltage}[fid](p, slot["name"])

    def _unified(self, p, device):
        status = UNIFIED_CHARGING.get(p[2], "unknown")
        charging = p[2] in (1, 2)
        if p[0]:
            return result(self.name, p[0], charging, status, device=device)
        level = UNIFIED_LEVELS.get(p[1] & 0x0F, "unknown")
        return result(self.name, None, charging, status, approx=True, device=device, text=level)

    def _status(self, p, device):
        status = STATUS_CHARGING.get(p[2], "unknown")
        return result(self.name, p[0] or None, p[2] in (1, 4), status, device=device)

    def _voltage(self, p, device):
        mv = (p[0] << 8) | p[1]
        charging = bool(p[2] & 0x80)
        return result(self.name, voltage_percent(mv), charging, ("discharging", "charging")[charging],
                      approx=True, device=device)

    def read(self):
        if self.slots is None or not self.slots:
            self._discover()
        for index, slot in list(self.slots.items()):
            if slot.get("asleep"):              # it was asleep when last asked: try again
                self.slots[index] = self._probe(index) or slot
            current = self.slots[index]
            if current["name"] is None and not current.get("asleep"):
                current["name"] = self._name(index)
        found = [self._battery(index, slot) for index, slot in sorted(self.slots.items())]
        if not found:
            self.slots = None
            return None
        return found
