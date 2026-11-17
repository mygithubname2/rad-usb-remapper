# mouselight.py - sets a Logitech or Razer mouse's own RGB lighting to the active profile's color.
#
# Loaded only when "Match the mouse lighting" is on (Settings > Feedback) and a Logitech or
# Razer device is attached. Uses the connection the battery reader (battery_logitech.py,
# battery_razer.py) already found, like haptics.py.
#
# Logitech: two HID++ lighting features exist (community documentation: OpenLogi, Solaar);
# the mouse is asked which one it has:
#
#   0x8070 colorLedEffects (per zone)
#     fn 0 get_info            -> 0: zone count
#     fn 1 get_zone_info  [z]  -> 3: number of effects
#     fn 2 get_effect_info [z, e] -> 2-3: effect ID (1 = fixed color)
#     fn 3 set_zone_effect (long) [z, e, R, G, B, 0 x7, persistence 0 = RAM only]
#   0x8071 rgbEffects (per cluster; software must claim control first)
#     fn 0 [0xFF, 0xFF, 0] device info  -> 2: cluster count
#     fn 0 [c, 0xFF, 0]    cluster info -> 4: number of effects
#     fn 0 [c, e, 0]       effect info  -> 2-3: effect ID (1 = fixed color)
#     fn 5 set_sw_control [1, 1 = all clusters, 0 = no events]   (released with [1, 0, 0])
#     fn 1 set_rgb_cluster_effect (long) [c, e, R, G, B, 0 x7, flags 1 = RAM only]
#
# Razer: see light_razer.py (loaded only when a Razer device is attached).
#
# Logitech colors are written to the mouse's RAM only (no wear on its memory). It is sent again:
#   * when the profile or its color changes,
#   * when the mouse is used after a pause (a mouse may restore its own lighting when it
#     wakes up),
#   * after the mouse is attached.
# What the mouse reported (feature, zones, effects) and any reply it didn't accept are
# written to the debug log, to adjust this for mice that behave differently.

from hostinput import now_ms

FEAT_ZONES, FEAT_CLUSTERS = 0x8070, 0x8071
FIXED_COLOR = 1               # effect ID of a plain color
MAX_ZONES = 4                 # mice have 1-3 lighting zones (logo, sides, buttons)
MAX_EFFECTS = 16
LOOK_AGAIN_MS = 5000          # look again this soon while a mouse may still wake up (asleep, not found yet)
IDLE_MS = 30000               # input after a pause this long: send the color again
QUIET_MS = 500                # wait for a color to settle (dragging a color picker) before sending
STILL_MS = 1000               # ...and for the mouse to be still for a second: sending holds up the mouse for up
                              # to ~0.1 s, which shouldn't land just as you start moving again
RETRY_MS = (5000, 15000, 60000, 300000)   # after a color the mouse didn't take: wait longer each time


class MouseLight:
    def __init__(self, battery, log, send):
        self.battery = battery
        self.log = log
        self.send = send
        self.enabled = False
        self.state = {"available": False}
        self._found = None            # Logitech: ("logi", reader, device index, feature index, feature id, [(zone, effect)])
                                      # Razer:    ("razer", reader, interface, transaction id, store, [(LED, name)])
        self._look_at = 0
        self._want = None             # color the profile asks for (R, G, B)
        self._want_text = None        # ... as the profile has it ("#rrggbb"): unchanged text is not parsed again
        self._want_at = 0
        self._shown = None            # color last set on the mouse (after brightness)
        self.level = 100              # brightness, % of the profile color
        self._input_at = 0            # last input seen (to notice a mouse that wakes up)
        self._quiet_from = 0          # when the current pause started (last input, or the last color sent)
        self._unlit = set()           # (receiver, device index) checked: no lighting - not asked again
        self._razer_tries = {}        # Razer reader -> looks that got no answer (gives up after 3)
        self._misses = 0              # colors in a row the mouse didn't take
        self._retry_at = 0

    # ---- from the main firmware
    def set_enabled(self, on):
        on = bool(on)
        if on == self.enabled:
            return
        self.enabled = on
        if not on and self._found is not None:
            self._release()
        self._shown = None
        self._look_at = 0
        self.publish()

    def set_color(self, color):
        """The active profile's color, "#RRGGBB" (call often: only a change does anything)."""
        if color == self._want_text:      # called every loop: same text, nothing to do
            return
        self._want_text = color
        rgb = _parse(color)
        if rgb is not None and rgb != self._want:
            self._want = rgb
            self._want_at = now_ms() + QUIET_MS

    def set_level(self, percent):
        """Brightness of the mouse's lighting, 5-100 % of the profile color."""
        if percent == self.level:
            return
        percent = max(5, min(100, int(percent)))
        if percent != self.level:
            self.level = percent
            self._want_at = now_ms() + QUIET_MS     # sent once the slider settles
            self.publish()

    def devices_changed(self):
        self._found = None
        self._unlit = set()
        self._razer_tries = {}
        self._shown = None
        self._misses = 0
        self._retry_at = 0
        self._look_at = now_ms() + 4000    # after the battery reader's first look
        self.state = {"available": False}
        self.publish()

    def publish(self):
        msg = {"type": "mouselight", "enabled": self.enabled, "level": self.level}
        msg.update(self.state)
        self.send(msg)

    def describe(self):
        if not self.enabled:
            return "off"
        if not self.state.get("available"):
            return "no Logitech or Razer mouse with RGB lighting found"
        found = self._found
        if found[0] == "razer":
            return "%s: LEDs %s via Razer commands%s" % (self.state.get("device") or "mouse",
                                                        ", ".join(n for _, n in found[5]), " (kept by the mouse)" if found[4] else "")
        return "%s: %d zone(s) via HID++ %04X" % (self.state.get("device") or "mouse", len(found[5]), found[4])

    # ---- main loop
    def service(self):
        host = self.battery.host
        if not self.enabled or not host.targets:
            return
        if self._found is None:
            if now_ms() >= self._look_at:
                self._look()
            return
        t = host.last_input
        if t != self._input_at:
            if t - self._quiet_from > IDLE_MS:
                self._shown = None            # used again after a pause: the mouse may have reset its lights
            self._input_at = self._quiet_from = t
        now = now_ms()
        if self._want is None or now < self._want_at or now < self._retry_at or now - t < STILL_MS:
            return
        rgb = self._dimmed(self._want)
        if rgb != self._shown:
            self._apply(rgb)

    def _dimmed(self, rgb):
        level = self.level
        return (rgb[0] * level // 100, rgb[1] * level // 100, rgb[2] * level // 100)

    # ---- finding the lighting
    def _look(self):
        # after colors the mouse kept refusing, search only once a minute (each search pauses the mouse)
        self._look_at = now_ms() + (LOOK_AGAIN_MS if self._misses < 3 else RETRY_MS[2])
        for reader in self.battery.vendor_readers(("Razer",)):
            if (id(reader), 0) in self._unlit:
                continue
            self._unlit.add((id(reader), 0))
            import light_razer                   # loaded only when a Razer device is attached
            link = light_razer.link(self, reader)
            if link is None:
                tries = self._razer_tries.get(id(reader), 0) + 1
                self._razer_tries[id(reader)] = tries
                if tries < 3:
                    self._unlit.discard((id(reader), 0))     # asleep? try again in a few seconds
                continue
            leds = light_razer.leds(self, reader, link)
            if not leds:
                self.log("Mouse lighting: %s answers Razer commands but none of its LEDs could be found"
                         % (reader.t.get("name") or "the mouse"), "warn")
                continue
            self._unlit.discard((id(reader), 0))
            self._use(("razer", reader, link[0], link[1], 0, leds), reader.t.get("name") or "Razer mouse", len(leds))
            return
        for reader in self.battery.logitech_readers():
            if not reader.slots:
                try:
                    reader._discover()
                except Exception as err:
                    self.log("Mouse lighting: looking for the mouse failed: %r" % (err,), "warn")
                    continue
            for index, slot in sorted(reader.slots.items()):
                if slot.get("asleep") or (id(reader), index) in self._unlit:
                    continue
                self._unlit.add((id(reader), index))      # unless it turns out to have lighting below
                for fid in (FEAT_CLUSTERS, FEAT_ZONES):
                    fidx = reader._feature(index, fid)
                    if fidx is None:
                        continue
                    zones = self._zones(reader, index, fidx, fid)
                    if not zones:
                        self.log("Mouse lighting: %s has HID++ %04X but no plain-color effect"
                                 % (slot.get("name") or "the mouse", fid), "warn")
                        continue
                    self._unlit.discard((id(reader), index))
                    self._use(("logi", reader, index, fidx, fid, zones), slot.get("name") or "paired device %d" % index, len(zones))
                    return

    def _use(self, found, device, zones):
        self._found = found
        self._input_at = self.battery.host.last_input     # the next input isn't "after a pause"
        self._quiet_from = now_ms()
        self.state = {"available": True, "device": device, "zones": zones}
        self._shown = None
        self.log("Mouse lighting: %s" % self.describe(), "ok")
        self.publish()

    def _zones(self, reader, index, fidx, fid):
        """[(zone, index of its fixed-color effect)] for the first few zones."""
        clusters = fid == FEAT_CLUSTERS
        info = reader._send(index, fidx, 0, (0xFF, 0xFF, 0) if clusters else ())
        if not info or info[0] != "ok":
            self.log("Mouse lighting: HID++ %04X info request answered %r" % (fid, info), "warn")
            return []
        count = min(MAX_ZONES, info[1][2] if clusters else info[1][0])
        out, seen = [], []
        for zone in range(count):
            zinfo = reader._send(index, fidx, 0, (zone, 0xFF, 0)) if clusters else reader._send(index, fidx, 1, (zone, 0, 0))
            if not zinfo or zinfo[0] != "ok":
                continue
            effects = min(MAX_EFFECTS, zinfo[1][4] if clusters else zinfo[1][3])
            ids = []
            for effect in range(effects):
                einfo = reader._send(index, fidx, 0 if clusters else 2, (zone, effect, 0))
                ids.append(einfo[1][2] << 8 | einfo[1][3] if einfo and einfo[0] == "ok" and len(einfo[1]) > 3 else None)
                if ids[-1] == FIXED_COLOR:
                    out.append((zone, effect))
                    break
            seen.append("%d: effects %s" % (zone, ids))
        self.log("Mouse lighting: HID++ %04X, %d %s (%s)" % (fid, count, ("zone(s)", "cluster(s)")[clusters], "; ".join(seen)), "info")
        return out

    # ---- setting the color
    def _apply(self, rgb):
        if self._found[0] == "razer":
            import light_razer
            light_razer.apply(self, rgb)
            return
        _kind, reader, index, fidx, fid, zones = self._found
        clusters = fid == FEAT_CLUSTERS
        if clusters:                          # software takes the lighting over (RAM only, no events)
            claim = reader._send(index, fidx, 5, (1, 1, 0))
            if not claim or claim[0] != "ok":
                self._lost("didn't accept software control (%r)" % (claim,))
                return
        ok, refused = 0, None
        for zone, effect in zones:
            params = bytearray(16)
            params[0], params[1] = zone, effect
            params[2], params[3], params[4] = rgb
            params[12] = 1 if clusters else 0     # RAM only
            reply = reader._send(index, fidx, 1 if clusters else 3, params)
            if reply and reply[0] == "ok":
                ok += 1
            else:
                refused = reply
        if refused is not None and ok:
            self.log("Mouse lighting: a zone answered %r" % (refused,), "warn")
        if not ok:
            self._lost("didn't take the color (%r)" % (refused,))
            return
        self._done(rgb, ok)

    def _done(self, rgb, ok):
        # the pause that makes the color go again counts from now (the first movement after the
        # color was set, a few seconds later, is no "pause")
        self._quiet_from = now_ms()
        self._misses = 0
        self._shown = rgb
        self.state["color"] = "#%02x%02x%02x" % rgb
        self.log("Mouse lighting: %s (brightness %d %%) on %d zone(s)" % (self.state["color"], self.level, ok), "info")
        self.publish()

    def _release(self):
        """Give the lighting back to the mouse (0x8071); 0x8070 and Razer keep the color until they reset."""
        kind, reader, index, fidx, fid, zones = self._found
        if kind == "logi" and fid == FEAT_CLUSTERS:
            reader._send(index, fidx, 5, (1, 0, 0))

    def _lost(self, why):
        """The mouse didn't take the color: keep what was found and try again later, waiting
        longer each time (not a full new search every few seconds: each try pauses the mouse)."""
        self._misses += 1
        wait = RETRY_MS[min(self._misses, len(RETRY_MS)) - 1]
        self.log("Mouse lighting: the mouse %s (asleep?) - trying again in %d s" % (why, wait // 1000),
                 "warn" if self._misses == 1 else "info")
        self._retry_at = now_ms() + wait
        if self._misses == 3:                 # still nothing: maybe another mouse now - search again then
            self._found = None
            self._unlit = set()
            self._razer_tries = {}
            self._look_at = self._retry_at


def _parse(color):
    try:
        text = str(color).lstrip("#")
        if len(text) != 6:
            return None
        return (int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16))
    except (TypeError, ValueError):
        return None
