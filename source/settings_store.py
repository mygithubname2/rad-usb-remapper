# settings_store.py -- persistent configuration for the USB remapper
#
# The configuration is stored in microcontroller.nvm (a 4 KB block of flash
# that CircuitPython reserves for user data).  NVM is used instead of a file
# because it is ALWAYS writable from code, even while the CIRCUITPY drive is
# mounted on a computer, and it survives power cycles and reboots.
#
# Layout:  b"RM" | len_lo | len_hi | <len bytes of compact JSON> ... | update flag (last 2 bytes)
#
# Only the board-wide settings are kept here. Each input device's profiles are in its own
# file on the drive (devprofiles.py). Profiles stored here by versions before v3.19 are
# ignored, and dropped by the next save.
#
# Configuration format (what the rest of the firmware and the web page use):
#   {
#     "v": 4,
#     "msc": true,                  expose CIRCUITPY drive at next boot
#     "verbose": false,             stream every remapped press to the web page
#     "usb": {"kbd": true, "mouse": true, "media": true, "pedal": false, "midi": false,
#             "mfr": "", "product": "", "vid": 0, "pid": 0}
#                                   devices presented to the computer, their names and USB ID
#                                   (boot; "" / 0 = default; a pedal alone = exact pedal clone;
#                                   with the pedal the ID is always the pedal's 05F3:00FF)
#     "mouse_type": "hires" | "standard"      mouse presented to the computer (boot)
#     "led_brightness": 32          status LED: value sent for a full channel (1-255)
#     "led_balance": [100, 69, 94]  status LED: red / green / blue gain in % (white balance)
#     "led_gamma": true             status LED: convert screen colors to linear light
#     "oled": {"rot": 0}            STEMMA QT display (SSD1306 128x32 at 0x3C): rotation 0 / 180
#     "aliases": {"0x3434:0xd030": "Keychron Q0 HE"}   names given on the page (shown on the display)
#     "haptic": {"on": true, "p": 1, "l": 4, "d": false}
#                                   haptic pulse (Logitech haptic mouse) when the profile (p) or
#                                   layer (l) changes; d: a different pulse for each profile/layer
#     "active": 0,                  index of the active profile
#     "profiles": [ <profile>, ... ]          1 to MAX_PROFILES
#   }
#   profile = {
#     "name": "Default",
#     "color": "#00c853",            status LED color while this profile is active
#     "mouse": { "<source>": <action> },   0-15 buttons, 16/17 tilt left/right,
#                                          18/19 wheel up/down
#     "keys":  { "<HID usage code>": <action> },
#     "mkeys": { "<HID usage code>": <action> },   keys sent by a device that is
#                                                   also a mouse (e.g. side buttons)
#     "media": { "<consumer usage>": <action> },   media keys sent by the device
#     "board": { "<GPIO number>": <action> },      buttons wired to the board's pins (7 = BOOT)
#                                                   (play/pause, volume...)
#     "move":  {"m": "pass"|"none"|"scroll", "s": 10..400 (speed %),
#               "ix": 0|1, "iy": 0|1, "sw": 0|1}     invert X / invert Y / swap axes
#     "tilt_hold": true, "tilt_release_ms": 300,
#     "layers": [ {"name": "Layer 2", "mouse": {...}, "keys": {...}, "mkeys": {...}, "media": {...}}, ... ]
#               extra layers (layer 1 = the profile's own tables above, the "Base" layer).
#               Inputs a layer doesn't map fall through to the Base layer.
#   }
#   action = {"t": "mouse",  "v": <button mask 1..255>}
#          | {"t": "key",    "v": [<HID usage>, ...]}   (up to 6, modifiers 0xE0-0xE7 allowed)
#          | {"t": "media",  "v": <consumer usage 1..0x3FF>}
#          | {"t": "layer",  "v": <layer index 0..> | "next" | "prev"}
#          | {"t": "layer_hold", "v": <layer index>}       (layer only while held)
#          | {"t": "text",   "v": "Best regards,\nOpus5.5"}   typed once per press (US layout,
#                            printable ASCII plus newline and tab, up to TEXT_LEN characters)
#          | {"t": "web",    "v": [<address>, <how 0-4>]}   opens a web page (remap.WEB_OPENERS;
#                            printable ASCII without spaces, up to URL_LEN characters)
#          | {"t": "app",    "v": [<name>, <how 0-2>]}   opens an application (remap.APP_OPENERS;
#                            printable ASCII, spaces allowed, up to APP_LEN characters)
#          | {"t": "pedal",  "v": <pedal mask 1=left 2=center 4=right>}   momentary
#          | {"t": "pedal_latch", "v": <pedal mask>}   latched: each press toggles
#          | {"t": "scroll", "v": [<h -10..10>, <v -10..10>]}
#          | {"t": "profile", "v": <profile index> | "next" | "prev"}
#          | {"t": "profile_hold", "v": <profile index>}   that profile only while held
#          | {"t": "cycle",  "v": [<action>, ...], "rt": <ms>}   each press does the next step
#          | {"t": "macro",  "v": [<action or {"t": "wait", "v": ms}>, ...]}   one press runs every step in order
#                            (up to 8 output actions); "rt": back to step 1 after this long
#                            without a press (0 / missing = never)
#          | {"t": "midi",   "v": ["n"|"c"|"p", <channel 1-16>, <note / controller / program>, <velocity / value>]}
#                            note on/off, control change (value, then 0 on release), program change
#          | {"t": "movement", "v": [<speed % of the profile's 10..400, 0 = unchanged>, <flags 0..127>]}
#                            pointer movement changed while held (flags: 1 X off, 2 Y off, 4 flip X,
#                            8 flip Y, 16 swap X/Y, 32 movement scrolls, 64 movement on even when
#                            the profile's movement is Off)
#          | {"t": "unlatch", "v": 0}                   lets go of everything latched
#          | {"t": "pointer", "v": [<ux -1..1>, <uy -1..1>, <step 0..500>, <speed 0..5000 /s>, <accel 0..3000 ms>]}
#                            moves the pointer: a step on the press, then speed while held
#          | {"t": "none",   "v": 0}                     (button/key disabled)
#   Every action may also carry
#     "l": "label"   shown on the web page (max 24 characters)
#     "r": 1         repeat while held (key / mouse / media: tap again every 50 ms)
#     "o": 1         also send the input's original action (e.g. the normal click)
#     "k": 1         latched (key / mouse / media / midi / movement): each press toggles it
#   Any source without an entry passes through unchanged.
#
# In flash the actions are packed as short lists (["k",224,6] instead of
# {"t":"key","v":[224,6]}), which keeps device files and page messages small.

import json

try:
    import microcontroller
    _NVM = microcontroller.nvm
except (ImportError, AttributeError):  # running on a desktop for tests
    _NVM = None

MAGIC = b"RM"
HEADER = 4
VERSION = 4
MAX_PROFILES = 8
NAME_LEN = 24
LED_BRIGHTNESS = 32            # default status LED brightness (full channel value, 1-255)
LED_BALANCE = (100, 69, 94)    # default red / green / blue gain (%): WS2812 green is brightest
APP_LEN = 100                  # characters in an "app" action's name
URL_LEN = 500                  # characters in a "web" action's address
TEXT_LEN = 2000                # characters in a "text" action (typed without a queue: cheap)
MAX_LAYERS = 8                 # per profile, including the Base layer
USB_DEVICES = ("kbd", "mouse", "media", "pedal", "midi")
TABLES = ("mouse", "keys", "mkeys", "media", "board")   # a profile's (and layer's) mapping tables
BOARD_PINS = (0, 29)          # board buttons: GPIO numbers (boardbuttons.py)
USB_NAME_LEN = 126          # what CircuitPython allows; USB names are plain ASCII
MOVE_MODES = ("pass", "none", "scroll")
MOUSE_SOURCES = 19          # highest mouse source index (19 = wheel down)
# boot-time options (take effect after a reboot), name -> allowed values (first = default)
BOOT_OPTIONS = {
    "mouse_type": ("hires", "standard"),   # 16-bit mouse with h-scroll / CircuitPython standard mouse
}


def _clean_usb_name(value):
    if not isinstance(value, str):
        return ""
    return "".join(ch for ch in value if " " <= ch <= "~").strip()[:USB_NAME_LEN]


def clean_usb(raw):
    """What the board presents to the computer (boot.py): which devices, and the names."""
    if not isinstance(raw, dict):
        raw = {"kbd": 1, "mouse": 1, "media": 1}
    out = {name: bool(raw.get(name)) for name in USB_DEVICES}
    out["mfr"] = _clean_usb_name(raw.get("mfr"))
    out["product"] = _clean_usb_name(raw.get("product"))
    for key in ("vid", "pid"):                      # 0 = the board's own ID
        value = raw.get(key)
        out[key] = value if type(value) is int and 0 < value <= 0xFFFF else 0
    return out


def names_sum(maker, product):
    """Short checksum of the USB names: boot_out.txt (max 512 bytes) has no room for them."""
    h = 5381
    for ch in maker + "\n" + product:
        h = (h * 33 + ord(ch)) & 0xFFFF
    return h


def default_move():
    return {"m": "pass", "s": 100, "ix": 0, "iy": 0, "sw": 0}


# default LED color for each profile slot (no red: red means "no device")
PROFILE_COLORS = ("#00c853", "#2962ff", "#aa00ff", "#ffab00", "#00b8d4", "#ff4081", "#ffffff", "#ff6d00")


def default_color(index=0):
    return PROFILE_COLORS[index % len(PROFILE_COLORS)]


def clean_color(value, index=0):
    """'#rrggbb' (any case) -> lower-case '#rrggbb'; anything else -> the slot's default."""
    try:
        if isinstance(value, str) and len(value) == 7 and value[0] == "#":
            int(value[1:], 16)
            return value.lower()
    except ValueError:
        pass
    return default_color(index)


def default_profile(name="Default", index=0):
    return {"name": name, "color": default_color(index), "mouse": {}, "keys": {}, "mkeys": {}, "media": {}, "board": {}, "move": default_move(),
            "tilt_hold": True, "tilt_release_ms": 300, "layers": []}


def default_config():
    return {"v": VERSION, "msc": True, "verbose": False, "board_buttons": True, "mouse_light": False, "mouse_light_level": 100, "usb": clean_usb({"kbd": 1, "mouse": 1, "media": 1}),
            "mouse_type": "hires",
            "led_brightness": LED_BRIGHTNESS, "led_balance": list(LED_BALANCE), "led_gamma": True,
            "haptic": clean_haptic_settings({}), "oled": clean_oled({}), "aliases": {},
            "active": 0, "profiles": [default_profile()]}


# ---------------------------------------------------------------- validation
# Each action type has its own cleaner; dispatch is a dictionary lookup.

def clean_haptic_settings(raw):
    raw = raw if isinstance(raw, dict) else {}
    return {"on": bool(raw.get("on", True)), "p": _clamp_int(raw.get("p", 1), -1, 31, 1),
            "l": _clamp_int(raw.get("l", 4), -1, 31, 4), "d": bool(raw.get("d", False))}


MAX_ALIASES = 8


def clean_oled(raw):
    raw = raw if isinstance(raw, dict) else {}
    return {"rot": 180 if raw.get("rot") == 180 else 0}


def clean_aliases(raw):
    out = {}
    if isinstance(raw, dict):
        for key, name in raw.items():
            if len(out) >= MAX_ALIASES or not isinstance(key, str) or not isinstance(name, str):
                continue
            name = "".join(ch for ch in name if 32 <= ord(ch) < 127).strip()[:NAME_LEN]
            if name and len(key) <= 13:
                out[key] = name
    return out


def _clamp_int(value, lo, hi, default):
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return default


def sanitize(cfg):
    """Return a fully valid config built from whatever was supplied."""
    base = default_config()
    if not isinstance(cfg, dict):
        return base
    base["msc"] = bool(cfg.get("msc", True))
    base["verbose"] = bool(cfg.get("verbose", False))
    base["board_buttons"] = bool(cfg.get("board_buttons", True))
    base["mouse_light"] = bool(cfg.get("mouse_light", False))
    base["mouse_light_level"] = _clamp_int(cfg.get("mouse_light_level", 100), 5, 100, 100)   # % of the profile color
    base["usb"] = clean_usb(cfg.get("usb"))
    base["led_brightness"] = _clamp_int(cfg.get("led_brightness", LED_BRIGHTNESS), 1, 255, LED_BRIGHTNESS)
    balance = cfg.get("led_balance")
    base["led_balance"] = ([_clamp_int(v, 10, 100, d) for v, d in zip(balance, LED_BALANCE)]
                           if isinstance(balance, list) and len(balance) == 3 else list(LED_BALANCE))
    base["led_gamma"] = bool(cfg.get("led_gamma", True))
    base["haptic"] = clean_haptic_settings(cfg.get("haptic"))
    base["oled"] = clean_oled(cfg.get("oled"))
    base["aliases"] = clean_aliases(cfg.get("aliases"))
    for name, allowed in BOOT_OPTIONS.items():
        base[name] = cfg.get(name) if cfg.get(name) in allowed else allowed[0]
    # (the profiles are not looked at here: they are kept in their own files and cleaned when they
    # are loaded or come from the page - cleaning a whole set for every settings change took memory)
    return base


# ---------------------------------------------------------------- compact form
_CODE_BY_TYPE = {"mouse": "m", "key": "k", "media": "c", "scroll": "s", "pedal": "p",
                 "pedal_latch": "l", "profile": "f", "profile_hold": "h", "none": "n",
                 "layer": "y", "layer_hold": "z", "text": "x",
                 "cycle": "u", "midi": "d", "macro": "a", "wait": "w",
                 "movement": "v", "unlatch": "g", "pointer": "j", "web": "e", "app": "q"}
_STEP_LISTS = ("cycle", "macro")      # actions whose value is a list of steps
_TYPE_BY_CODE = {code: kind for kind, code in _CODE_BY_TYPE.items()}
_LIST_TYPES = ("key", "scroll", "midi", "movement", "pointer", "web", "app")


_EXTRAS = ("l", "r", "o", "h", "rt", "k")


def _unpack_cycle(steps):
    """A cycle's or macro's steps; one inside another isn't allowed, so it isn't unpacked
    (clean_action drops it) - no unbounded recursion on a crafted message."""
    return [_unpack_action(a, False) for a in steps]


def _unpack_action(packed, top=True):
    if not isinstance(packed, list) or not packed:
        return packed                      # already a dict (the page's unpacked form) or junk
    packed = list(packed)
    extras = packed.pop() if len(packed) > 1 and isinstance(packed[-1], dict) else {}
    kind = _TYPE_BY_CODE.get(packed[0])
    value = list(packed[1:]) if kind in _LIST_TYPES else (packed[1] if len(packed) > 1 else 0)
    value = (_unpack_cycle(packed[1:]) if top else []) if kind in _STEP_LISTS else value
    action = {"t": kind, "v": value}
    action.update(extras)
    return action


unpack_action = _unpack_action


def _map_tables(profile, fn):
    out = dict(profile)
    for table in TABLES:
        out[table] = {k: fn(a) for k, a in (profile.get(table) or {}).items()}
    if not out["board"]:                # most sets have no board buttons: nothing stored for them
        del out["board"]
    if isinstance(profile.get("layers"), list):
        out["layers"] = [_map_tables(layer, fn) if isinstance(layer, dict) else layer
                         for layer in profile["layers"]]
    return out


# ---------------------------------------------------------------- piece-by-piece JSON
# The settings are written, measured and sent in small pieces, so they never need one big
# block of free memory (after a while the board's memory is too fragmented for that).

def emit_json(out, obj):
    """JSON-encode obj piece by piece into out.put(text) (the same text as compact json.dumps).

    No recursion: CircuitPython's stack for nested calls is small (1.5 KB), and a set of
    profiles nests deep (set > profiles > profile > layers > layer > table > macro > step),
    so the open lists and objects are kept on a list here instead ("pystack exhausted")."""
    open_ = []                    # [iterator, first item?, closing bracket, is a dict] per open container
    value = obj
    while True:
        kind = type(value)
        if kind is dict:
            out.put("{")
            open_.append([iter(value.items()), True, "}", True])
        if kind is list or kind is tuple:
            out.put("[")
            open_.append([iter(value), True, "]", False])
        if kind is not dict and kind is not list and kind is not tuple:
            out.put(json.dumps(value))
        value = _NOTHING_LEFT
        while open_ and value is _NOTHING_LEFT:
            top = open_[-1]
            try:
                item = next(top[0])
            except StopIteration:
                out.put(top[2])
                open_.pop()
                continue
            separator = "" if top[1] else ","
            top[1] = False
            if top[3]:
                out.put(separator + json.dumps(str(item[0])) + ":")
                value = item[1]
            else:
                out.put(separator)
                value = item
        if value is _NOTHING_LEFT:
            return


_NOTHING_LEFT = object()


class _Count:
    def __init__(self):
        self.n = 0

    def put(self, text):
        self.n += len(text.encode("utf-8"))


def json_size(obj):
    """Bytes compact JSON of obj takes, without building it."""
    out = _Count()
    emit_json(out, obj)
    return out.n


class ChunkWriter:
    """Collects text as UTF-8 in a small buffer and hands each full chunk to sink(offset, data)."""

    def __init__(self, sink, size=256):
        self.sink = sink
        self.buf = bytearray(size)
        self.n = 0
        self.at = 0                       # bytes handed on so far

    def put(self, text):
        data = text.encode("utf-8")
        i, size = 0, len(data)
        while i < size:
            k = min(size - i, len(self.buf) - self.n)
            self.buf[self.n:self.n + k] = data[i:i + k]
            self.n += k
            i += k
            if self.n == len(self.buf):
                self.flush()

    def flush(self):
        if self.n:
            self.sink(self.at, memoryview(self.buf)[:self.n])
            self.at += self.n
            self.n = 0


# ---------------------------------------------------------------- storage

UPDATE_FLAG = b"\xfe\xfe"     # in the last 2 bytes: the next start-up is an Update-mode one
                             # (0xFE never occurs in UTF-8 JSON, nor in erased flash: 0xFF)


def capacity():
    return (len(_NVM) - HEADER - len(UPDATE_FLAG)) if _NVM is not None else 0


def request_update_boot():
    """Make the next start-up an Update-mode one (the drive editable by the computer)."""
    if _NVM is not None:
        _NVM[len(_NVM) - 2:len(_NVM)] = UPDATE_FLAG


def take_update_boot():
    """boot.py: True once if an Update-mode start-up was asked for (the flag is cleared)."""
    if _NVM is None or bytes(_NVM[len(_NVM) - 2:len(_NVM)]) != UPDATE_FLAG:
        return False
    _NVM[len(_NVM) - 2:len(_NVM)] = b"\x00\x00"
    return True


def load():
    """Load the saved config; fall back to defaults if missing or corrupt."""
    if _NVM is None:
        return sanitize({})
    try:
        if bytes(_NVM[0:2]) != MAGIC:
            return sanitize({})
        length = _NVM[2] | (_NVM[3] << 8)
        if length == 0 or length > len(_NVM) - HEADER:
            return sanitize({})
        raw = json.loads(bytes(_NVM[HEADER:HEADER + length]))   # (json takes bytes: no copy as text)
        for key in ("profiles", "active"):
            raw.pop(key, None)            # profiles from before v3.19: not used any more
        return sanitize(raw)
    except Exception:  # corrupt JSON, bad UTF-8 ... never crash the boot
        return sanitize({})


def save(cfg):
    """Validate and persist cfg. Returns the cleaned config. Raises ValueError if too big."""
    clean = sanitize(cfg)
    if _NVM is None:
        return clean
    packed = {k: v for k, v in clean.items() if k not in ("profiles", "active")}   # settings only
    size = json_size(packed)              # measured first: nothing is written if it doesn't fit
    if size > capacity():
        raise ValueError("configuration too large (%d of %d bytes) - remove some mappings or profiles"
                         % (size, capacity()))
    out = ChunkWriter(_nvm_put)           # written in 256-byte pieces, only the ones that changed
    emit_json(out, packed)
    out.flush()
    _nvm_put(-HEADER, MAGIC + bytes((size & 0xFF, size >> 8)))   # header last: its length
    return clean


def _nvm_put(offset, data):
    """Write data at HEADER + offset unless it is already there (each write rewrites the
    4 KB flash sector, so unchanged pieces are skipped)."""
    at = HEADER + offset
    if _NVM[at:at + len(data)] != bytes(data):
        _NVM[at:at + len(data)] = bytes(data)


# ---------------------------------------------------------------- per-device sets
# (devprofiles.py stores each device's profiles in its own file with these)

def empty_profiles():
    return [default_profile()]


def store_profiles(raw):
    """Profiles (packed or not, from the page or a file) -> cleaned and packed: the form the set is
    kept in while in use (see profile_clean.py, which is loaded for this and dropped again)."""
    import sys
    import profile_clean
    try:
        return profile_clean.store_profiles(raw)
    finally:
        sys.modules.pop("profile_clean", None)
