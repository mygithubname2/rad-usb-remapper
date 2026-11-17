# profile_clean.py - checks profiles and mappings from the page or a profile file, and packs them.
#
# Loaded only while a set is loaded or applied (settings_store.store_profiles) and dropped
# afterwards: the board doesn't keep this code in memory while the mouse is in use. What a
# mapping may contain is described at the top of settings_store.py.
#
# Cleaners are looked up in a table (no if/elif chains).

from settings_store import (MAX_PROFILES, MAX_LAYERS, NAME_LEN, TEXT_LEN, URL_LEN, APP_LEN, MOVE_MODES, BOARD_PINS, MOUSE_SOURCES,
                            default_move, default_profile, clean_color, _clamp_int, _unpack_action, _map_tables,
                            _CODE_BY_TYPE, _LIST_TYPES, _STEP_LISTS, _EXTRAS)


def _clean_mouse(value):
    try:
        mask = int(value) & 0xFF
    except (TypeError, ValueError):
        return None
    return {"t": "mouse", "v": mask} if mask else None


def _clean_key(value):
    if isinstance(value, int):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return None
    codes = []
    for code in value:
        if isinstance(code, int) and 3 < code < 256 and code not in codes:
            codes.append(code)
    return {"t": "key", "v": codes[:6]} if codes else None


def _clean_media(value):
    try:
        usage = int(value)
    except (TypeError, ValueError):
        return None
    return {"t": "media", "v": usage} if 0 < usage <= 0x3FF else None


def _clean_scroll(value):
    try:
        h, v = (max(-10, min(10, int(x))) for x in value)
    except (TypeError, ValueError):
        return None
    return {"t": "scroll", "v": [h, v]} if (h or v) else None


def _pedal_cleaner(kind):
    def clean(value):
        try:
            mask = int(value) & 0x07
        except (TypeError, ValueError):
            return None
        return {"t": kind, "v": mask} if mask else None
    return clean


def _index_cleaner(kind, count, steps):
    """Cleaner for actions that pick a profile / layer: an index below `count`, or (when
    `steps`) "next" / "prev"."""
    def clean(value):
        if steps and value in ("next", "prev"):
            return {"t": kind, "v": value}
        try:
            index = int(value)
        except (TypeError, ValueError):
            return None
        return {"t": kind, "v": index} if 0 <= index < count else None
    return clean


MIDI_KINDS = ("n", "c", "p")    # note, control change, program change


def _clean_midi(value):
    """[kind, channel 1-16, number 0-127, value 0-127]: a note (velocity), a controller
    (value sent on press, 0 on release) or a program change (value unused)."""
    if not isinstance(value, list) or len(value) < 3 or value[0] not in MIDI_KINDS:
        return None
    channel = _clamp_int(value[1], 1, 16, 1)
    number = _clamp_int(value[2], 0, 127, 60)
    level = _clamp_int(value[3] if len(value) > 3 else 100, 0, 127, 100)
    return {"t": "midi", "v": [value[0], channel, number, level]}


def _clean_text(value):
    if not isinstance(value, str):
        return None
    text = "".join(ch for ch in value if 32 <= ord(ch) < 127 or ch in "\n\t")[:TEXT_LEN]
    return {"t": "text", "v": text} if text else None


def _clean_web(value):
    """[address, how]: printable ASCII without spaces (what a US keyboard types), how 0-4."""
    if not isinstance(value, list) or len(value) != 2 or not isinstance(value[0], str):
        return None
    url = value[0]
    if not url or len(url) > URL_LEN or any(not 33 <= ord(ch) < 127 for ch in url):
        return None
    return {"t": "web", "v": [url, _clamp_int(value[1], 0, 4, 0)]}


def _clean_app(value):
    """[name, how]: printable ASCII (spaces allowed inside), how 0-2."""
    if not isinstance(value, list) or len(value) != 2 or not isinstance(value[0], str):
        return None
    name = value[0].strip()
    if not name or len(name) > APP_LEN or any(not 32 <= ord(ch) < 127 for ch in name):
        return None
    return {"t": "app", "v": [name, _clamp_int(value[1], 0, 2, 0)]}


def _clean_none(_value):
    return {"t": "none", "v": 0}


# a cycle: each press does the next step; steps are ordinary output actions
CYCLE_STEP_TYPES = ("key", "mouse", "media", "scroll", "text", "pedal", "pedal_latch", "midi", "none")
CYCLE_STEPS = 8
CYCLE_RESET_MS = 60000


def _clean_cycle(value):
    if not isinstance(value, list):
        return None
    steps = [clean_action(a) for a in value[:CYCLE_STEPS] if isinstance(a, dict) and a.get("t") in CYCLE_STEP_TYPES]
    steps = [_unlatched(a) for a in steps if a]
    return {"t": "cycle", "v": steps} if steps else None


# a macro: one press runs all its steps in order; "wait" steps pause between them
MACRO_STEP_TYPES = ("key", "mouse", "media", "scroll", "text", "pedal", "pedal_latch", "midi", "wait")
MACRO_STEPS = 16
WAIT_MS = (10, 10000)


def _clean_wait(value):
    return {"t": "wait", "v": _clamp_int(value, WAIT_MS[0], WAIT_MS[1], 500)}


def _clean_macro(value):
    if not isinstance(value, list):
        return None
    steps = [clean_action(a) for a in value[:MACRO_STEPS] if isinstance(a, dict) and a.get("t") in MACRO_STEP_TYPES]
    steps = [_unlatched(a) for a in steps if a]
    return {"t": "macro", "v": steps} if steps else None


def _unlatched(step):
    """Steps of a cycle / macro are never latched (the latch belongs to the input)."""
    step.pop("k", None)
    return step


def _clean_movement(value):
    if not isinstance(value, list) or len(value) < 2:
        return None
    speed = _clamp_int(value[0], 0, 400, 0)
    speed = 0 if speed < 10 else speed
    flags = _clamp_int(value[1], 0, 127, 0)
    return {"t": "movement", "v": [speed, flags]} if speed or flags else None


def _clean_pointer(value):
    if not isinstance(value, list) or len(value) < 5:
        return None
    ux = _clamp_int(value[0], -1, 1, 0)
    uy = _clamp_int(value[1], -1, 1, 0)
    step = _clamp_int(value[2], 0, 500, 0)
    speed = _clamp_int(value[3], 0, 5000, 0)
    accel = _clamp_int(value[4], 0, 3000, 0)
    if not (ux or uy) or not (step or speed):
        return None
    return {"t": "pointer", "v": [ux, uy, step, speed, accel]}


_CLEANERS = {"mouse": _clean_mouse, "key": _clean_key, "media": _clean_media,
             "scroll": _clean_scroll, "pedal": _pedal_cleaner("pedal"),
             "pedal_latch": _pedal_cleaner("pedal_latch"),
             "profile": _index_cleaner("profile", MAX_PROFILES, True),
             "profile_hold": _index_cleaner("profile_hold", MAX_PROFILES, False),
             "layer": _index_cleaner("layer", MAX_LAYERS, True),
             "layer_hold": _index_cleaner("layer_hold", MAX_LAYERS, False), "text": _clean_text,
             "cycle": _clean_cycle, "midi": _clean_midi,
             "macro": _clean_macro, "wait": _clean_wait,
             "movement": _clean_movement, "pointer": _clean_pointer, "web": _clean_web, "app": _clean_app, "unlatch": lambda v: {"t": "unlatch", "v": 0},
             "none": _clean_none}


REPEATABLE = ("key", "mouse", "media")
LATCHABLE = ("key", "mouse", "media", "midi", "movement", "pointer")


def clean_action(action):
    if not isinstance(action, dict):
        return None
    cleaner = _CLEANERS.get(action.get("t"))
    cleaned = cleaner(action.get("v", 0)) if cleaner else None
    if not cleaned:
        return None
    label = action.get("l")
    if isinstance(label, str) and label.strip():
        cleaned["l"] = label.strip()[:NAME_LEN]
    latched = bool(action.get("k")) and cleaned["t"] in LATCHABLE
    if latched:
        cleaned["k"] = 1
    if action.get("r") and cleaned["t"] in REPEATABLE and not latched:
        cleaned["r"] = 1
    if action.get("o") and cleaned["t"] != "none" and not latched:     # (a latched action has no "original")
        cleaned["o"] = 1
    pulse = action.get("h")                 # also a haptic pulse (Logitech haptic mouse)
    if isinstance(pulse, int) and 0 <= pulse <= 31:
        cleaned["h"] = pulse
    reset = _clamp_int(action.get("rt", 0), 0, CYCLE_RESET_MS, 0)
    if reset and cleaned["t"] == "cycle":   # start again at step 1 after this long without a press
        cleaned["rt"] = reset
    return cleaned


def _clean_table(table, lo, hi):
    out = {}
    if not isinstance(table, dict):
        return out
    for key, action in table.items():
        try:
            code = int(key)
        except (TypeError, ValueError):
            continue
        if not lo <= code <= hi:
            continue
        cleaned = clean_action(action)
        if cleaned:
            out[code] = cleaned          # (a number: no text object per mapping)
    return out


def _clean_move(move):
    out = default_move()
    if not isinstance(move, dict):
        return out
    out["m"] = move.get("m") if move.get("m") in MOVE_MODES else "pass"
    out["s"] = _clamp_int(move.get("s", 100), 10, 400, 100)
    for flag in ("ix", "iy", "sw"):
        out[flag] = 1 if move.get(flag) else 0
    return out


def clean_profile(raw, index=0):
    profile = default_profile("Profile %d" % (index + 1), index)
    if not isinstance(raw, dict):
        return profile
    name = raw.get("name")
    if isinstance(name, str) and name.strip():
        profile["name"] = name.strip()[:NAME_LEN]
    profile["color"] = clean_color(raw.get("color"), index)
    profile.update(_clean_tables(raw))
    profile["move"] = _clean_move(raw.get("move"))
    profile["tilt_hold"] = bool(raw.get("tilt_hold", True))
    profile["tilt_release_ms"] = _clamp_int(raw.get("tilt_release_ms", 300), 50, 2000, 300)
    profile["layers"] = [_clean_layer_tables(layer, i + 2)
                         for i, layer in enumerate((raw.get("layers") or [])[:MAX_LAYERS - 1])
                         if isinstance(layer, dict)]
    return profile


def _clean_tables(raw):
    """The mapping tables of a profile or layer."""
    return {"mouse": _clean_table(raw.get("mouse"), 0, MOUSE_SOURCES),
            "keys": _clean_table(raw.get("keys"), 4, 0xE7),
            "mkeys": _clean_table(raw.get("mkeys"), 4, 0xE7),
            "media": _clean_table(raw.get("media"), 1, 0x3FF),
            "board": _clean_table(raw.get("board"), BOARD_PINS[0], BOARD_PINS[1])}


def _clean_layer_tables(raw, number):
    name = raw.get("name")
    out = {"name": name.strip()[:NAME_LEN] if isinstance(name, str) and name.strip() else "Layer %d" % number}
    out.update(_clean_tables(raw))
    return out


def _pack_cycle(steps):
    return [_pack_action(a) for a in steps]


def _pack_action(action):
    if isinstance(action, list):
        return action                      # already packed
    value = action["v"]
    rest = list(value) if action["t"] in _LIST_TYPES else [value]
    rest = _pack_cycle(value) if action["t"] in _STEP_LISTS else rest
    extras = {k: action[k] for k in _EXTRAS if k in action}
    return [_CODE_BY_TYPE[action["t"]]] + rest + ([extras] if extras else [])


def store_profiles(raw):
    """Profiles (packed or not, from the page or a file) -> cleaned and packed: the form the set is
    kept in while in use. Packed, a mapping takes about a fifth of the memory of its dict form; the
    remapper unpacks a mapping the first time it is used (Remapper.action). Done one profile at a
    time (each raw profile is let go as soon as it's done), so only one is ever unpacked at once."""
    if not isinstance(raw, list):
        return []
    out = []
    for i in range(min(len(raw), MAX_PROFILES)):
        p = raw[i]
        raw[i] = None
        if isinstance(p, dict):
            out.append(_map_tables(clean_profile(_map_tables(p, _unpack_action), len(out)), _pack_action))
    return out
