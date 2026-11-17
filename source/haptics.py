# haptics.py - haptic feedback on Logitech mice with a haptic motor (MX Master 4).
#
# Uses HID++ feature 0x19B0 through the same receiver connection the battery reader
# (battery_logitech.py) already found. Logitech hasn't published this feature; the
# functions below follow community documentation (Solaar, OpenLogi):
#   fn 0 get capabilities -> bytes 4-7: big-endian mask, bit n = waveform n supported
#   fn 1 get configuration -> byte 0 flags (bit 0 = enabled), byte 1 intensity 0-100
#   fn 2 set configuration  <- [flags, intensity, 0]
#   fn 4 play               <- [waveform, 0, 0]
#
# Requests are queued and sent from the main loop (service()), never from inside the
# remapper's button handling: sending may wait briefly and keeps polling the input.

from hostinput import now_ms
from settings_store import _clamp_int

LOOK_AGAIN_MS = 30000        # how often to look for a haptic mouse when none was found
PLAY_REPLY_MS = 400           # wait this long for a pulse's acknowledgement
QUEUE_MAX = 4                # pulses waiting to be sent (more are dropped, not delayed)


class Haptics:
    def __init__(self, battery, log, send):
        self.battery = battery           # BatteryMonitor: owns the Logitech readers
        self.log = log
        self.send = send
        self.state = {"available": False}
        self._queue = []
        self._look_at = 0                # when to look for a haptic mouse (0 = now)
        self._found = None               # (reader, device index, slot)
        self._misses = 0                 # pulses in a row without an acknowledgement

    # ---- requests (safe to call from anywhere: they only queue)
    def play(self, wave):
        if not isinstance(wave, int) or not 0 <= wave <= 31:    # -1 / None = no pulse
            return
        if len(self._queue) < QUEUE_MAX:
            self._queue.append(("play", wave))

    def set_strength(self, enabled, intensity):
        """Only the latest strength matters: it replaces one still waiting to be sent."""
        self._queue = [op for op in self._queue if op[0] != "set"]
        self._queue.append(("set", bool(enabled), _clamp_int(intensity, 0, 100, 50)))

    def devices_changed(self):
        self._queue = []
        self._found = None
        self._look_at = now_ms() + 3000  # after the battery reader's first look
        self.state = {"available": False}
        self.publish()

    def publish(self):
        msg = {"type": "haptic"}
        msg.update(self.state)
        self.send(msg)

    def describe(self):
        if not self.state.get("available"):
            return "no haptic mouse found"
        return "%s: %s, strength %d %%, waveforms %s" % (
            self.state.get("device") or "mouse", ("off", "on")[bool(self.state.get("enabled"))],
            self.state.get("intensity", 0), ",".join(str(w) for w in self.state.get("waves", [])) or "?")

    # ---- main loop
    def service(self):
        if not self.battery.host.targets:
            self._queue = []
            return
        if self._found is None and (self._queue or now_ms() >= self._look_at):
            self._look()
        if not self._queue:
            return
        op = self._queue.pop(0)
        if self._found is None:
            return                       # nothing to send it to
        {"play": self._play, "set": self._set}[op[0]](*op[1:])

    def _look(self):
        self._look_at = now_ms() + LOOK_AGAIN_MS
        for reader in self.battery.logitech_readers():
            if reader.slots is None or not reader.slots:
                try:
                    reader._discover()       # usually already done by the battery check
                except Exception as err:
                    self.log("Haptics: looking for the mouse failed: %r" % (err,), "warn")
                    continue
            for index, slot in sorted(reader.slots.items()):
                if slot.get("haptic") is not None:
                    self._found = (reader, index, slot)
                    self.state = {"available": True, "device": slot.get("name") or "paired device %d" % index,
                                  "waves": slot.get("waves") or []}
                    self._read_config()
                    self.log("Haptics: %s" % self.describe(), "ok")
                    self.publish()
                    return

    def _read_config(self):
        reader, index, slot = self._found
        reply = reader._send(index, slot["haptic"], 1)
        if reply and reply[0] == "ok" and len(reply[1]) >= 2:
            self.state["flags"] = reply[1][0]
            self.state["enabled"] = bool(reply[1][0] & 1)
            self.state["intensity"] = reply[1][1]

    def _play(self, wave):
        reader, index, slot = self._found
        # wait for the acknowledgement: that also takes it out of the receiver's small reply
        # buffer - unread acknowledgements fill it and the receiver then refuses requests
        reply = reader._send(index, slot["haptic"], 4, (wave, 0, 0), PLAY_REPLY_MS)
        if reply is None:
            self._misses += 1
            if self._misses >= 3:            # receiver gone or the mouse asleep: look again later
                self.log("Haptics: the mouse doesn't answer (asleep?)", "warn")
                self._found = None
                self._misses = 0
                self._look_at = now_ms() + 2000
            return
        self._misses = 0
        if reply[0] != "ok":
            self.log("Haptics: the mouse refused pulse %d (error %s)" % (wave, reply[1][:1]), "warn")

    def _set(self, enabled, intensity):
        reader, index, slot = self._found
        flags = (self.state.get("flags", 0) & ~1) | int(enabled)
        reply = reader._send(index, slot["haptic"], 2, (flags, intensity, 0))
        if not reply or reply[0] != "ok":
            self.log("Haptics: the mouse didn't accept the new strength (asleep?)", "warn")
            return
        self._read_config()
        self.log("Haptics: %s" % self.describe(), "ok")
        self.publish()
