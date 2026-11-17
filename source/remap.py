# remap.py -- button / key remapping engine
#
# Pure Python (no hardware imports) so it can be unit-tested on a desktop.
#
# Inputs arrive already decoded (hidparse.py) as
#   mouse(sid, buttons, dx, dy, wheel, pan)   16 button bits, signed deltas
#   keys(sid, codes, table)                   set of keyboard usages; table is "key",
#                                             or "mkey" for keys sent by a mouse
#   consumer(sid, usages)                     set of media usages
#
# Sources (what can be remapped) live in four tables:
#   "mouse"    0..15 = mouse buttons 1..16, 16 = tilt left, 17 = tilt right,
#              18 = wheel up, 19 = wheel down (one tap per wheel notch)
#   "key"      keyboard usage codes
#   "mkey"     keys sent by a device that is also a mouse (gaming-mouse side
#              buttons often type 1, 2, 3...) -- kept apart from keyboard keys
#   "consumer" media keys from the input device (profile table "media")
#   "board"    buttons wired to the board's own pins (code = GPIO number; 7 = BOOT button).
#              Unmapped they do nothing (boardbuttons.py)
#
# Actions:
#   {"t": "mouse",  "v": mask}            mouse buttons (bit 0 = left ... bit 7)
#   {"t": "key",    "v": [usage, ...]}    key or combo (modifiers 0xE0..0xE7)
#   {"t": "media",  "v": usage}           consumer-control usage (volume, play...)
#   {"t": "pedal",  "v": mask}            foot pedal, MOMENTARY:
#                                         down while the button is held
#                                         (mask: 1 left, 2 center, 4 right)
#   {"t": "pedal_latch", "v": mask}       foot pedal, LATCHED (deadman toggle):
#                                         each press flips the pedal down/up,
#                                         releasing the button does nothing
#   {"t": "scroll", "v": [h, v]}          scroll steps, repeats while held
#   {"t": "profile", "v": n|"next"|"prev"} switch mapping profile (on_profile callback)
#   {"t": "profile_hold", "v": n}         use profile n only while the button is held
#                                         (on_profile_hold callback, press and release)
#   {"t": "layer", "v": n|"next"|"prev"}  switch layer within the profile (on_layer)
#   {"t": "layer_hold", "v": n}           use layer n only while held (on_layer_hold)
#   {"t": "text",   "v": "Best regards"}  types the text once per press (US layout)
#   {"t": "midi",   "v": [kind, ch, n, val]}  MIDI note / controller / program change
#   {"t": "cycle",  "v": [action, ...]}   each press does the next step ("rt": restart after ms)
#   {"t": "macro",  "v": [action, ...]}   one press runs every step in order: each step is pressed and
#                                         released ("tapped"); text steps are typed in full before the
#                                         next step; {"t": "wait", "v": ms} pauses. A press while a
#                                         macro is still running is ignored (on_text says so)
#   {"t": "movement", "v": [speed, flags]}  changes pointer movement while held: speed in % of
#                                         the profile's (0 = unchanged); flags 1 X off, 2 Y off,
#                                         4 flip X, 8 flip Y, 16 swap X/Y, 32 movement scrolls,
#                                         64 movement on (even when the profile's movement is Off).
#                                         Several at once: the last pressed counts
#   {"t": "unlatch", "v": 0}              lets go of everything latched (keys, buttons, pedals...)
#   {"t": "pointer", "v": [ux, uy, step, speed, accel]}  moves the pointer: direction ux, uy (-1/0/1),
#                                         `step` counts at once on the press, then (speed > 0) while
#                                         held `speed` counts a second, reached after `accel` ms
#                                         (starting at 10 %); held movement starts after HOLD_START_MS
#                                         when there is a step (a tap moves exactly one step)
#   {"t": "web",    "v": [url, how]}      opens a web page: a macro of opener shortcut, wait, the
#                                         address typed, Enter. how (WEB_OPENERS): 0 Windows Run box
#                                         (Win+R), 1 Mac Spotlight (Cmd+Space), 2 new browser tab
#                                         (Ctrl+T), 3 new browser tab on a Mac (Cmd+T), 4 the Browser home
#                                         media key, then Ctrl+L (the address bar) before the address
#   {"t": "app",    "v": [name, how]}     opens an application the same way: how (APP_OPENERS): 0 the
#                                         Start menu (Win), 1 Spotlight (Cmd+Space), 2 Run box (Win+R)
#   {"t": "none",   "v": 0}               disabled
#   Optional on any action: "k": 1 latched (key/mouse/media/midi/movement): each press
#   toggles it on / off, releasing the button does nothing,
#   "r": 1 repeat while held (key/mouse/media),
#   "o": 1 also send the input's original action, "h": n also a haptic pulse,
#   "l": label (page only).
#
# Test mode (self.test, switched on by the web page while the pointer is on its
# Test pad): presses are reported through on_test but NOT sent to the computer;
# pointer movement still passes (unscaled) so the pointer can leave the pad.
# Profile and layer actions still work, since they don't send anything.
#
# Mouse movement is not a button: each profile has a "move" setting --
#   mode "pass" (normal), "none" (disabled) or "scroll" (movement scrolls),
#   speed in percent, invert X, invert Y, swap axes.
#
# Design notes
#   * Unmapped sources pass through unchanged (default action per table).
#   * The action's type selects press/release handlers from dispatch tables
#     -- no if/elif chains.
#   * The action captured at press time is remembered, so a release always
#     undoes exactly what the press did, even if the mapping changed meanwhile.
#   * Output keys / buttons / media usages are reference counted, so several
#     sources mapped to the same target overlap correctly.
#   * A pedal is down when any momentary source holds it OR it is latched.
#     Latched pedals are released whenever everything is released (input
#     device unplugged/swapped, mapping applied, board restart).
#   * Other latched actions are kept per input (table, code) in self._latches and let go
#     the same way, and also on a profile switch (not on a layer switch).

TILT_LEFT = 16
TILT_RIGHT = 17
OUT_MOUSE_MASK = 0xFF
SCROLL_DELAY_MS = 350
SCROLL_RATE_MS = 70
REPEAT_MS = 50            # "repeat while held": tap again this often
TILT_RELEASE_MS = 300     # default: tilt counts as released after this long without a tilt report
WHEEL_UP = 18
WHEEL_DOWN = 19
MAX_WHEEL_TAPS = 5        # notches handled per report when the wheel is remapped
SCROLL_COUNTS = 2000      # movement counts per scroll notch = SCROLL_COUNTS / speed%


_TABLE_OF = {"tilt": "mouse", "wheel": "mouse"}     # source kind -> mapping table
INTERNAL_TYPES = ("profile", "profile_hold", "layer", "layer_hold")   # change state, send nothing
# "web": the shortcut that opens somewhere to type an address, how long that takes to appear, and
# how long to let the typed address settle before Enter (Spotlight looks it up as you type)
WEB_OPENERS = ((0xE3, 0x15), (0xE3, 0x2C), (0xE0, 0x17), (0xE3, 0x17))
WEB_OPEN_MS = (700, 700, 400, 400)
WEB_ENTER_MS = (150, 500, 150, 150)
WEB_BROWSER_KEY = 0x223          # how 4: "Browser home" (opens the default browser on Windows and Linux)
WEB_BROWSER_MS = 1500            # ...given this long to open (or come to the front) before Ctrl+L
# "app": the same for an application's name (the Start menu and Spotlight search as you type: a
# longer pause before Enter)
APP_OPENERS = ((0xE3,), (0xE3, 0x2C), (0xE3, 0x15))
APP_OPEN_MS = (700, 700, 700)
APP_ENTER_MS = (700, 500, 150)
TYPE_STEP_MS = 6         # between key press and release reports while typing text
MACRO_TAP_MS = 20        # a macro step is held this long, then released
MACRO_GAP_MS = 20        # pause after each macro step, so the computer sees separate presses


_US_PLAIN = "1234567890" + "-=[]\\;'`,./"        # keys 0x1E-0x27, then the symbol keys below
_US_SHIFTED = "!@#$%^&*()" + "_+{}|:\"~<>?"
_US_SYMBOL_CODES = b"\x2d\x2e\x2f\x30\x31\x33\x34\x35\x36\x37\x38"
_US_SPECIAL = " \n\t"
_US_SPECIAL_CODES = b"\x2c\x28\x2b"


def us_key(ch):
    """HID key codes for a character on a US keyboard layout (0xE1 = left Shift), or None.
    Worked out per character instead of a table, which would take ~2 KB of RAM."""
    o = ord(ch)
    if 97 <= o <= 122:
        return [0x04 + o - 97]
    if 65 <= o <= 90:
        return [0xE1, 0x04 + o - 65]
    i = _US_SPECIAL.find(ch)
    if i >= 0:
        return [_US_SPECIAL_CODES[i]]
    shift = 0
    i = _US_PLAIN.find(ch)
    if i < 0:
        i = _US_SHIFTED.find(ch)
        shift = 1
    if i < 0:
        return None
    code = 0x1E + i if i < 10 else _US_SYMBOL_CODES[i - 10]
    return [0xE1, code] if shift else [code]


def _trunc_div(value, div):
    """Integer division rounding toward zero (keeps the remainder's sign)."""
    return value // div if value >= 0 else -((-value) // div)


def _noop(*_args):
    pass


# what tilt / wheel sources do when unmapped (never changed, so shared)
_SCROLL_DEFAULTS = {TILT_LEFT: {"t": "scroll", "v": [-1, 0]}, TILT_RIGHT: {"t": "scroll", "v": [1, 0]},
                    WHEEL_UP: {"t": "scroll", "v": [0, 1]}, WHEEL_DOWN: {"t": "scroll", "v": [0, -1]}}


def _mouse_default(code, _orig):
    return _SCROLL_DEFAULTS.get(code) or {"t": "mouse", "v": (1 << code) & OUT_MOUSE_MASK if code < 8 else 0}


def _key_default(code, _orig):
    return {"t": "key", "v": [code]}


def _media_default(code, _orig):
    return {"t": "media", "v": code}


_NOTHING = {"t": "none", "v": 0}


def _board_default(code, _orig):
    return _NOTHING


from settings_store import unpack_action   # (mappings are kept packed: see action())

HOLD_TYPES = ("profile_hold", "layer_hold")   # kept pressed across profile/layer switches
LATCH_TYPES = ("key", "mouse", "media", "midi", "movement", "pointer")
HOLD_START_MS = 200       # pointer action: held movement starts this long after the press (when it has a step)   # can carry "k": 1 (latched)
MOVE_X_OFF, MOVE_Y_OFF, MOVE_FLIP_X, MOVE_FLIP_Y, MOVE_SWAP, MOVE_SCROLL, MOVE_ON = 1, 2, 4, 8, 16, 32, 64

class Remapper:
    def __init__(self, output, on_event=None, clock=None):
        """
        output   -- object with key_press(list), key_release(list),
                    mouse_press(mask), mouse_release(mask),
                    mouse_move(dx, dy, wheel, pan), media_press(u), media_release(u),
                    midi_press(v), midi_release(v), release_all()
        on_event -- optional callback(table, code, down, action) for debugging
        clock    -- function returning milliseconds (for scroll auto-repeat)
        """
        self.out = output
        self.clock = clock or (lambda: 0)
        self.latched = 0         # pedal bits latched down by pedal_latch actions
        self._pedal_out = 0      # pedal bits currently reported as down
        # callbacks (do nothing until set)
        self.on_event = on_event or _noop   # (table, code, down, action) for debugging
        self.on_pedal = _noop         # (latched_mask, changed_mask)
        self.on_hit = _noop           # (table, code) for every physical press
        self.on_profile = _noop       # (value) for profile actions: index, "next", "prev"
        self.on_profile_hold = _noop  # (index, down, source) for hold-to-use profiles
        self.on_layer = _noop         # (value) for layer actions: index, "next", "prev"
        self.on_layer_hold = _noop    # (index, down, source) for hold-to-use layers
        self.on_test = _noop          # (kind, code, down, action) for presses held back in test mode
        self.on_text = _noop          # (message) about text typing
        self.on_haptic = _noop        # (waveform) for an action's "h" pulse
        self.on_latch = _noop         # (table, code, action, on) when a latched action turns on / off
        self.test = False             # True: report presses instead of sending them
        self._testing = {}            # source -> action, for presses made in test mode
        self._typing = None           # text being typed: [text, next index, keys held down or None]
        self._macro = None            # macro being run: [steps, next index, step held down or None]
        self._macro_next = 0
        self._cycles = {}             # (table, code) -> (cycle action, next step, time of last press)
        self._type_next = 0
        self._src = None         # source whose press/release is being handled
        self._base_move = {"m": "pass", "s": 100, "ix": 0, "iy": 0, "sw": 0}   # the profile's
        self.move = self._base_move          # what applies now (the profile's, changed by movement actions)
        self._move_changes = []              # [(input, [speed, flags])], last pressed last
        self._latches = {}                   # (table, code) -> latched action that is on
        self._pointers = {}                  # (table, code) -> [value, held since, last tick] of held pointer actions
        self._pfrac = [0, 0]                 # pointer movement not sent yet (counts x 100000)
        self._frac = [0, 0]      # sub-count remainders when movement speed != 100 %
        self._sacc = [0, 0]      # movement accumulated toward the next scroll notch
        self._tv = [0, 0, 0, 0]       # transformed movement (see _move_pass)
        self._move_modes = {"pass": self._move_pass, "none": self._move_none,
                            "scroll": self._move_scroll}
        self.maps = {"mouse": {}, "key": {}, "mkey": {}, "consumer": {}, "board": {}}
        self._unpacked = {}
        self._defaults = {"mouse": _mouse_default, "key": _key_default,
                          "mkey": _key_default, "consumer": _media_default, "board": _board_default}
        self._board_down = set()      # board buttons held down (GPIO numbers)
        self._active = {}        # source -> action captured at press time
        self._repeat = {}        # source -> [action, next_ms]
        self._tilt_seen = {}     # held tilt source -> time of last tilt report
        self.tilt_hold = True    # True: tilt acts like a held button (no repeats)
        self.tilt_release_ms = TILT_RELEASE_MS
        self._tilt_modes = (self._tilt_tap, self._tilt_held)   # index = tilt_hold
        self._counts = {}        # ("k"|"b"|"m", code) -> holders
        self._prev_buttons = {}
        self._prev_keys = {}
        self._prev_media = {}
        self._down = {"mouse": self._mouse_down, "key": self._key_down, "media": self._media_down,
                      "pedal": self._pedal_down, "pedal_latch": self._pedal_toggle,
                      "profile": lambda v: self.on_profile(v),
                      "profile_hold": lambda v: self.on_profile_hold(v, True, self._src),
                      "layer": lambda v: self.on_layer(v),
                      "layer_hold": lambda v: self.on_layer_hold(v, True, self._src),
                      "text": self._text_down, "scroll": _noop, "none": _noop,
                      "macro": self._macro_down,
                      "midi": lambda v: self.out.midi_press(v),
                      "movement": lambda v: self._move_change(self._src, v),
                      "unlatch": lambda v: self.unlatch_all(), "web": self._web_down, "app": self._app_down,
                      "pointer": lambda v: self._pointer_down(self._src, v)}
        self._up = {"mouse": self._mouse_up, "key": self._key_up, "media": self._media_up,
                    "pedal": self._pedal_up, "pedal_latch": _noop,
                    "profile": _noop, "profile_hold": lambda v: self.on_profile_hold(v, False, self._src),
                    "layer": _noop, "layer_hold": lambda v: self.on_layer_hold(v, False, self._src),
                    "text": _noop, "scroll": _noop, "none": _noop, "web": _noop, "app": _noop,
                    "midi": lambda v: self.out.midi_release(v),
                    "movement": lambda v: self._move_change(self._src, None), "unlatch": _noop,
                    "pointer": lambda v: self._pointers.pop((self._src[1], self._src[2]), None)}
        self._edge = (self._release, self._press)      # index 0 = up, 1 = down

    # ------------------------------------------------------------ config
    def load(self, profile):
        """Install a profile after releasing EVERYTHING (mapping applied/saved)."""
        self.release_all()
        self._cycles = {}
        self._install(profile)

    def set_profile(self, profile, keep_latches=False):
        """Switch profile on the fly: release held outputs but keep latched pedals
        and the physical button state, so a button that is still held (e.g. the
        one that switched the profile) does not fire again in the new profile.
        Hold-to-use profile/layer buttons stay pressed: their release switches back.
        Other latched actions are let go too, unless keep_latches (a layer switch)."""
        for src in [s for s, a in self._active.items() if a["t"] not in HOLD_TYPES and s[-1] != "o"]:
            self._release(src)
        if not keep_latches:
            self._drop_latches()
        self._repeat.clear()
        self._tilt_seen.clear()
        self._install(profile)

    def _install(self, profile):
        for table, key in (("mouse", "mouse"), ("key", "keys"), ("mkey", "mkeys"), ("consumer", "media"),
                           ("board", "board")):
            self.maps[table] = {int(k): v for k, v in profile.get(key, {}).items()}
        self._unpacked = {}                      # (table, code) -> unpacked mapping (see action())
        self.tilt_hold = bool(profile.get("tilt_hold", True))
        self.tilt_release_ms = int(profile.get("tilt_release_ms", TILT_RELEASE_MS))
        self._base_move = dict(self._base_move, **(profile.get("move") or {}))
        self._apply_move()

    def action(self, table, code, orig=None):
        a = self.maps[table].get(code)
        if a is None:
            return self._defaults[table](code, orig)
        if isinstance(a, list):                  # kept packed (settings_store.store_profiles): unpacked
            key = (table, code)                  # the first time it's used, then kept - the same object
            u = self._unpacked.get(key)          # each press, which a cycle's position relies on
            if u is None:
                u = self._unpacked[key] = unpack_action(a)
            return u
        return a

    # ------------------------------------------------------------ inputs
    def mouse(self, sid, buttons, dx=0, dy=0, wheel=0, pan=0):
        changed = buttons ^ self._prev_buttons.get(sid, 0)
        self._prev_buttons[sid] = buttons
        index = 0
        while changed:
            if changed & 1:
                src = (sid, "mouse", index)
                self._edge[(buttons >> index) & 1](src, self.action("mouse", index))
            changed >>= 1
            index += 1
        if wheel:
            notch = WHEEL_UP if wheel > 0 else WHEEL_DOWN
            if notch in self.maps["mouse"]:          # wheel remapped: one tap per notch
                src = (sid, "wheel", notch)
                for _ in range(min(abs(wheel), MAX_WHEEL_TAPS)):
                    self._press(src, self.action("mouse", notch))
                    self._release(src)
                wheel = 0
            if wheel:
                self.on_hit("wheel", notch)
            if wheel and self.test:                  # report the scroll instead of sending it
                self._test_report("wheel", notch, self.action("mouse", notch))
                wheel = 0
        if dx or dy:
            self.on_hit("move", 0)                   # (throttled by the receiver)
        if (dx or dy) and not self.test:             # test mode: plain pointer movement only
            t = self._transform(dx, dy)          # (a list reused for every report: no memory used)
            dx, dy = t[0], t[1]
            wheel += t[2]
            pan += t[3]
        if pan:
            tilt = TILT_RIGHT if pan > 0 else TILT_LEFT
            if tilt in self.maps["mouse"]:          # tilt remapped (unmapped tilt just scrolls)
                self._tilt_modes[self.tilt_hold](sid, tilt)
                pan = 0
            if pan:
                self.on_hit("tilt", tilt)
            if pan and self.test:
                self._test_report("tilt", tilt, self.action("mouse", tilt))
                pan = 0
        if dx or dy or wheel or pan:
            self.out.mouse_move(dx, dy, wheel, pan)

    # ------------------------------------------------------------ movement
    def _transform(self, dx, dy):
        move = self.move
        if move["sw"]:
            dx, dy = dy, dx
        if move["ix"]:                  # (plain ifs: a (a, b)[flag] pick would make a tuple per report)
            dx = -dx
        if move["iy"]:
            dy = -dy
        off = move.get("off", 0)
        if off & MOVE_X_OFF:
            dx = 0
        if off & MOVE_Y_OFF:
            dy = 0
        return self._move_modes[move["m"]](dx, dy)     # (modes are checked when settings are cleaned)

    # ------------------------------------------------------------ pointer actions
    def _pointer_scale(self):
        """% to scale pointer actions by: a Move action's speed change applies to them too."""
        base = self._base_move["s"] or 100
        return self.move["s"] * 100 // base

    def _pointer_down(self, src, value):
        ux, uy, step, speed = value[0], value[1], value[2], value[3]
        if step:
            n = step * self._pointer_scale() // 100
            self.out.mouse_move(ux * n, uy * n, 0, 0)
        if speed:
            now = self.clock()
            start = now + (HOLD_START_MS if step else 0)
            self._pointers[(src[1], src[2])] = [value, start, start]

    def _pointer_tick(self):
        """Held pointer actions: move by speed x time since the last tick (several add up)."""
        now = self.clock()
        scale = self._pointer_scale()
        frac = self._pfrac
        pointers = self._pointers
        for key in pointers:
            entry = pointers[key]
            value, start, last = entry[0], entry[1], entry[2]
            if now <= last:
                continue
            entry[2] = now
            accel = value[4]
            pct = 100 if not accel else max(10, min(100, (now - start) * 100 // accel))
            amount = value[3] * (now - last) * pct * scale // 100    # counts x 100000
            frac[0] += value[0] * amount
            frac[1] += value[1] * amount
        dx = _trunc_div(frac[0], 100000)
        dy = _trunc_div(frac[1], 100000)
        if dx or dy:
            frac[0] -= dx * 100000
            frac[1] -= dy * 100000
            self.out.mouse_move(dx, dy, 0, 0)

    def _move_change(self, src, value):
        """A movement action pressed (value) or released (None) by input src: the last one
        pressed and still on counts; when none is, the profile's own movement applies."""
        key = (src[1], src[2])
        self._move_changes = [c for c in self._move_changes if c[0] != key]
        if value is not None:
            self._move_changes.append((key, value))
        self._apply_move()

    def _apply_move(self):
        base = self._base_move
        move = base
        if self._move_changes:
            speed, flags = self._move_changes[-1][1]
            move = dict(base)
            if speed:
                move["s"] = max(1, base["s"] * speed // 100)
            if flags & MOVE_SWAP:
                move["sw"] = 0 if base["sw"] else 1
            if flags & MOVE_FLIP_X:
                move["ix"] = 0 if base["ix"] else 1
            if flags & MOVE_FLIP_Y:
                move["iy"] = 0 if base["iy"] else 1
            if flags & MOVE_ON and base["m"] == "none":     # profile movement Off: moves the pointer
                move["m"] = "pass"
            if flags & MOVE_SCROLL:
                move["m"] = "scroll"
            move["off"] = flags & (MOVE_X_OFF | MOVE_Y_OFF)
        self.move = move
        self._frac = [0, 0]
        self._sacc = [0, 0]

    def _scaled(self, axis, value):
        speed = self.move["s"]
        if speed == 100:
            return value
        self._frac[axis] += value * speed
        out = _trunc_div(self._frac[axis], 100)
        self._frac[axis] -= out * 100
        return out

    # movement modes: fill in and return self._tv = [dx, dy, wheel, pan] (one list, reused)
    def _move_pass(self, dx, dy):
        tv = self._tv
        tv[0] = self._scaled(0, dx)
        tv[1] = self._scaled(1, dy)
        tv[2] = tv[3] = 0
        return tv

    def _move_none(self, dx, dy):
        tv = self._tv
        tv[0] = tv[1] = tv[2] = tv[3] = 0
        return tv

    def _move_scroll(self, dx, dy):
        """Movement becomes scrolling: up/down -> wheel, left/right -> horizontal."""
        per_notch = max(1, SCROLL_COUNTS // self.move["s"])
        acc = self._sacc
        acc[0] += dx
        acc[1] += dy
        pan = _trunc_div(acc[0], per_notch)
        notches = _trunc_div(acc[1], per_notch)
        acc[0] -= pan * per_notch
        acc[1] -= notches * per_notch
        tv = self._tv
        tv[0] = tv[1] = 0
        tv[2] = -notches                       # pushing up (negative dy) scrolls up
        tv[3] = pan
        return tv

    def keys(self, sid, codes, table="key"):
        if codes is None:                    # rollover / phantom report: keep state
            return
        previous = self._prev_keys.get(sid, set())
        self._prev_keys[sid] = codes
        for code in previous - codes:
            self._release((sid, table, code))
        for code in codes - previous:
            self._press((sid, table, code), self.action(table, code))

    def board(self, code, down):
        """A button on the board's own pins went down / up (boardbuttons.py)."""
        src = ("board", "board", code)
        if down:
            self._board_down.add(code)
            self._press(src, self.action("board", code))
            return
        if code in self._board_down:
            self._board_down.discard(code)
            self._release(src)

    def board_release_all(self):
        """Missed events (queue overflow): let go of every board button."""
        for code in list(self._board_down):
            self.board(code, False)

    def consumer(self, sid, usages):
        previous = self._prev_media.get(sid, set())
        self._prev_media[sid] = usages
        for usage in previous - usages:
            self._release((sid, "consumer", usage))
        for usage in usages - previous:
            self._press((sid, "consumer", usage), self.action("consumer", usage))

    # ------------------------------------------------------------ edges
    def _test_report(self, kind, code, action):
        """A wheel notch / tilt tick in test mode: shown as a press and its release."""
        self.on_test(kind, code, True, action)
        self.on_test(kind, code, False, action)

    def _cycle_step(self, src, action):
        """A cycle action: the step this press does (and move on to the next one).
        Each button keeps its own position; it restarts when the mapping changes, or
        after action["rt"] ms without a press."""
        key = (src[1], src[2])
        steps = action["v"]
        now = self.clock()
        state = self._cycles.get(key)
        index = 0
        if state and state[0] is action and not (action.get("rt") and now - state[2] >= action["rt"]):
            index = state[1] % len(steps)
        self._cycles[key] = (action, (index + 1) % len(steps), now)
        step = dict(steps[index])
        for extra in ("o", "h"):          # the cycle's own options apply to every step
            if extra in action and extra not in step:
                step[extra] = action[extra]
        step["step"] = (index + 1, len(steps))
        return step

    def _press(self, src, action):
        self._release(src)          # never double-press the same source
        if action["t"] == "cycle":
            action = self._cycle_step(src, action)
        if self.test and action["t"] not in INTERNAL_TYPES:
            self._testing[src] = action          # held back: only reported
            self.on_hit(src[1], src[2])
            self.on_test(src[1], src[2], True, action)
            return
        if self.test:
            self.on_test(src[1], src[2], True, action)
            action = dict(action)
            action.pop("o", None)                # never send the original click in test mode
            action.pop("h", None)                # ...nor an extra haptic pulse
        if action.get("k") and action["t"] in LATCH_TYPES:
            self.on_hit(src[1], src[2])
            self.on_event(src[1], src[2], True, action)
            self._latch(src, action)
            return
        self._active[src] = action
        self.on_hit(src[1], src[2])
        self.on_event(src[1], src[2], True, action)
        if action["t"] == "scroll":
            self._scroll(action["v"])
            self._repeat[src] = [action, self.clock() + SCROLL_DELAY_MS, SCROLL_RATE_MS]
        if action.get("r"):
            self._repeat[src] = [action, self.clock() + REPEAT_MS, REPEAT_MS]
        if "h" in action:                   # extra haptic pulse, so you feel it happened (once per press)
            self.on_haptic(action["h"])
        if action.get("o"):                 # also send what the input normally does
            original = self._defaults[_TABLE_OF.get(src[1], src[1])](src[2], None)
            self._active[src + ("o",)] = original
            if original["t"] == "scroll":
                self._scroll(original["v"])
            self._down.get(original["t"], _noop)(original["v"])
        self._src = src
        self._down.get(action["t"], _noop)(action["v"])   # last: may switch profile

    def _release(self, src, _action=None):
        """Undo what the press of `src` did (its action as captured then; _action is only
        there so press and release share a signature in _edge)."""
        tested = self._testing.pop(src, None)
        if tested is not None:
            self.on_test(src[1], src[2], False, tested)
            return
        action = self._active.pop(src, None)
        if action is None:
            return
        self._repeat.pop(src, None)
        original = self._active.pop(src + ("o",), None)
        if original:
            self._up.get(original["t"], _noop)(original["v"])
        self._src = src
        self._up.get(action["t"], _noop)(action["v"])
        self.on_event(src[1], src[2], False, action)

    # ------------------------------------------------------------ latching
    def _latch(self, src, action):
        """A latched action: the first press turns it on, the next press of the same input
        turns it off (whatever that input is mapped to by then). Releasing does nothing."""
        key = (src[1], src[2])
        self._src = src
        held = self._latches.pop(key, None)
        if held is not None:
            self._up.get(held["t"], _noop)(held["v"])
            self.on_latch(key[0], key[1], held, False)
            return
        if "h" in action:
            self.on_haptic(action["h"])
        self._latches[key] = action
        self._down.get(action["t"], _noop)(action["v"])
        self.on_latch(key[0], key[1], action, True)

    def _drop_latches(self):
        """Let go of every latched action (not the latched pedals)."""
        latches, self._latches = self._latches, {}
        for key, action in latches.items():
            self._src = (None,) + key
            self._up.get(action["t"], _noop)(action["v"])
            self.on_latch(key[0], key[1], action, False)

    def unlatch_all(self):
        """The "release everything latched" action: latched actions and latched pedals."""
        self._drop_latches()
        changed = self.latched
        if changed:
            self.latched = 0
            self._pedal_sync()
            self.on_pedal(0, changed)

    def latched_count(self):
        return len(self._latches) + bin(self.latched).count("1")

    # ------------------------------------------------------------ wheel tilt
    # A tilting wheel reports horizontal-scroll "ticks" over and over while it
    # is held and never reports a release.
    def _tilt_tap(self, sid, tilt):
        """Repeat mode: every tilt report is a full press + release."""
        src = (sid, "tilt", tilt)
        self._press(src, self.action("mouse", tilt))
        self._release(src)

    def _tilt_held(self, sid, tilt):
        """Hold mode: first report presses, further reports keep it held; tick()
        releases it once reports stop for tilt_release_ms (or on direction change)."""
        src = (sid, "tilt", tilt)
        other = (sid, "tilt", TILT_LEFT + TILT_RIGHT - tilt)
        if other in self._tilt_seen:
            del self._tilt_seen[other]
            self._release(other)
        if src not in self._tilt_seen:
            self._press(src, self.action("mouse", tilt))
        self._tilt_seen[src] = self.clock()

    def tick(self):
        """Call regularly: types queued text, runs macros, releases finished tilts, auto-repeats held actions."""
        if self._typing and self.clock() >= self._type_next:
            self._type_step()
        if self._macro and not self._typing and self.clock() >= self._macro_next:
            self._macro_step()
        if self._pointers:
            self._pointer_tick()
        # (loops over the dicts themselves, not .items() / .values() or a new list: no memory used
        # on the passes while something is held)
        if self._tilt_seen:
            now = self.clock()
            seen = self._tilt_seen
            while seen:
                gone = None
                for src in seen:
                    if now - seen[src] >= self.tilt_release_ms:
                        gone = src
                        break
                if gone is None:
                    break
                del seen[gone]
                self._release(gone)
        if not self._repeat:
            return
        now = self.clock()
        repeat = self._repeat
        for key in repeat:
            entry = repeat[key]
            if now >= entry[1]:
                self._again(entry[0])
                entry[1] = now + entry[2]

    def _again(self, action):
        """One more repetition of a held action: a scroll step, or release + press."""
        if action["t"] == "scroll":
            self._scroll(action["v"])
            return
        self._up.get(action["t"], _noop)(action["v"])
        self._down.get(action["t"], _noop)(action["v"])

    def _scroll(self, value):
        self.out.mouse_move(0, 0, value[1], value[0])

    # ------------------------------------------------------------ outputs
    def _hold(self, kind, code):
        held = self._counts.get((kind, code), 0)
        self._counts[(kind, code)] = held + 1
        return held == 0

    def _drop(self, kind, code):
        left = self._counts.pop((kind, code), 0) - 1
        if left > 0:
            self._counts[(kind, code)] = left
        return left == 0

    def _key_down(self, codes):
        fresh = [c for c in codes if self._hold("k", c)]
        if fresh:
            self.out.key_press(fresh)

    def _key_up(self, codes):
        gone = [c for c in codes if self._drop("k", c)]
        if gone:
            self.out.key_release(gone)

    def _mouse_down(self, mask):
        fresh = 0
        for index in range(8):
            bit = 1 << index
            if mask & bit and self._hold("b", bit):
                fresh |= bit
        if fresh:
            self.out.mouse_press(fresh)

    def _mouse_up(self, mask):
        gone = 0
        for index in range(8):
            bit = 1 << index
            if mask & bit and self._drop("b", bit):
                gone |= bit
        if gone:
            self.out.mouse_release(gone)

    def _pedal_down(self, mask):
        for bit in (1, 2, 4):
            if mask & bit:
                self._hold("p", bit)
        self._pedal_sync()

    def _pedal_up(self, mask):
        for bit in (1, 2, 4):
            if mask & bit:
                self._drop("p", bit)
        self._pedal_sync()

    def _pedal_toggle(self, mask):
        self.latched ^= mask & 0x07
        self._pedal_sync()
        self.on_pedal(self.latched, mask & 0x07)

    def _pedal_sync(self):
        """Report pedal = (held by a momentary source) OR (latched)."""
        want = self.latched
        for bit in (1, 2, 4):
            if self._counts.get(("p", bit)):
                want |= bit
        have = self._pedal_out
        self._pedal_out = want
        if want & ~have:
            self.out.pedal_press(want & ~have)
        if have & ~want:
            self.out.pedal_release(have & ~want)

    # ------------------------------------------------------------ macros
    def _macro_down(self, steps):
        if self._macro:
            self.on_text("A macro is still running - press ignored")
            return
        self._macro = [steps, 0, None]
        self._macro_next = self.clock()

    def _macro_step(self):
        """One step of the macro: let go of the step that is down, or start the next one."""
        macro = self._macro
        held = macro[2]
        if held:
            macro[2] = None
            self._up.get(held["t"], _noop)(held["v"])
            self._macro_next = self.clock() + MACRO_GAP_MS
            return
        if macro[1] >= len(macro[0]):
            self._macro = None               # done
            return
        step = macro[0][macro[1]]
        macro[1] += 1
        wait = step["v"] if step["t"] == "wait" else 0
        if step["t"] == "scroll":
            self._scroll(step["v"])
        self._down.get(step["t"], _noop)(step["v"])     # text: typed in full before the next step (tick)
        macro[2] = None if wait or step["t"] in ("text", "scroll") else step
        self._macro_next = self.clock() + (wait or MACRO_TAP_MS)

    def _web_down(self, value):
        if value[1] == 4 and value[0]:          # the browser's media key, then its address bar
            self._macro_down([{"t": "media", "v": WEB_BROWSER_KEY}, {"t": "wait", "v": WEB_BROWSER_MS},
                              {"t": "key", "v": [0xE0, 0x0F]}, {"t": "wait", "v": 200},
                              {"t": "text", "v": value[0]}, {"t": "wait", "v": 150}, {"t": "key", "v": [0x28]}])
            return
        self._launch(value, WEB_OPENERS, WEB_OPEN_MS, WEB_ENTER_MS)

    def _app_down(self, value):
        self._launch(value, APP_OPENERS, APP_OPEN_MS, APP_ENTER_MS)

    def _launch(self, value, openers, open_ms, enter_ms):
        """Open a web page or an application: run as a macro of the opener shortcut, a wait, the address
        or name typed, a wait and Enter (made at the press: these are pressed seldom)."""
        text, how = value[0], value[1]
        if not text or not 0 <= how < len(openers):
            return
        self._macro_down([{"t": "key", "v": list(openers[how])}, {"t": "wait", "v": open_ms[how]},
                          {"t": "text", "v": text}, {"t": "wait", "v": enter_ms[how]},
                          {"t": "key", "v": [0x28]}])

    def _text_down(self, text):
        if self._typing:                     # still typing the last one: ignore
            self.on_text("Still typing the previous text - press ignored")
            return
        self._typing = [text, 0, None]       # typed straight from the text: memory use doesn't grow with it
        self._type_next = self.clock()

    def _type_step(self):
        """One step of typing: release the key that is down, or press the next character's keys."""
        typing = self._typing
        if typing[2]:
            self._key_up(typing[2])
            typing[2] = None
        else:
            text, i, codes = typing[0], typing[1], None
            while i < len(text) and not codes:   # (characters with no key are skipped)
                codes = us_key(text[i])
                i += 1
            typing[1] = i
            if codes:
                self._key_down(codes)
                typing[2] = codes
            else:
                self._typing = None              # done
        self._type_next = self.clock() + TYPE_STEP_MS

    def _media_down(self, usage):
        if self._hold("m", usage):
            self.out.media_press(usage)

    def _media_up(self, usage):
        if self._drop("m", usage):
            self.out.media_release(usage)

    # ------------------------------------------------------------ safety
    def release_all(self):
        """Release everything (device unplugged, config changed, error...)."""
        for src in list(self._active):
            self._release(src)
        self._drop_latches()
        self._pointers.clear()
        self._move_changes = []
        self._apply_move()
        self._active.clear()
        self._testing.clear()
        self._typing = None
        self._macro = None
        self._repeat.clear()
        self._tilt_seen.clear()
        self._counts.clear()
        self._prev_buttons.clear()
        self._prev_keys.clear()
        self._prev_media.clear()
        changed = self.latched
        self.latched = 0
        self._pedal_out = 0
        self.out.release_all()
        if changed:
            self.on_pedal(0, changed)

    def held_count(self):
        return len(self._active)
