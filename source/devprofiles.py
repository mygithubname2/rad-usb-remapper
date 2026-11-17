# devprofiles.py - each input device's own profiles and layers, one file per device.
#
# Files live in /profiles on the CIRCUITPY drive, named after the device's USB ID:
#   /profiles/046d_c52b.json  = {"v": 1, "name": "MX Master 4", "active": 0, "profiles": [...]}
# (profiles packed the same compact way as in NVM, see settings_store.py).
#
# The board can only write the drive while the computer can't: boot.py remounts it for
# the board on every normal start-up (not in Update mode or with the D5 jumper). Reading
# always works. A file is written as <name>.tmp first and then renamed, so a power cut
# never leaves a half-written profile file (load() falls back to the .tmp copy).
#
# Only the attached device's profiles are in memory; the others stay in their files.

import gc
import json
import os

import settings_store

DIR = "/profiles"
MAX_FILE = 16384             # largest file read (a sanity limit; the real one is the memory budget)
# A device's whole set is held in memory while it is plugged in, packed as in the file (since
# v3.34; unpacked it took ~13 bytes of memory per byte of file). Packed, mappings take ~3 bytes
# per byte of file, text ~2 bytes per character. 16 KB for profiles leaves the rest to run in.
MEMORY_BUDGET = 16384
MAP_COST, TEXT_COST = 3, 2


def file_id(did):
    """'0x046d:0xc52b' -> '046d_c52b' (None for anything else)."""
    parts = str(did or "").lower().replace("0x", "").split(":")
    if len(parts) != 2:
        return None
    for p in parts:                       # (plain loops: no generator objects - memory may be short here)
        if len(p) != 4:
            return None
        for c in p:
            if c not in "0123456789abcdef":
                return None
    return parts[0] + "_" + parts[1]


def did_of(fid):
    return "0x%s:0x%s" % tuple(fid.split("_"))


def writable():
    """True when the board may write the drive now (not while the computer can)."""
    try:
        import storage
        return not storage.getmount("/").readonly
    except Exception:
        return False


def _path(fid, tmp=False):
    return "%s/%s.json%s" % (DIR, fid, (".tmp" if tmp else ""))


def _read(path):
    """Parse a profile file. json.load reads the file a little at a time, so no block of
    MAX_FILE bytes is needed (free memory is often there only in smaller pieces)."""
    if os.stat(path)[6] > MAX_FILE:
        raise ValueError("file too large")
    gc.collect()
    with open(path) as f:
        return json.load(f)


def load(did):
    """The device's saved set as (profiles, active, name), or None if it has none.
    MemoryError is passed on: the file is there but couldn't be read right now."""
    fid = file_id(did)
    if fid is None:
        return None
    gc.collect()
    for path in (_path(fid), _path(fid, True)):
        try:
            raw = _read(path)
        except (OSError, ValueError):
            continue
        profiles = settings_store.store_profiles(raw.get("profiles"))      # (kept packed in memory)
        if profiles:
            active = raw.get("active", 0)
            active = active if isinstance(active, int) and 0 <= active < len(profiles) else 0
            return profiles, active, str(raw.get("name") or "")
    return None


def _doc(profiles, active, name):
    return {"v": 1, "name": str(name or "")[:40], "active": active,
            "profiles": profiles}          # (already packed: see settings_store.store_profiles)


def _texts(profiles):
    """Characters in the text actions (and text steps of cycles and macros) of a set."""
    n = 0
    for profile in profiles:
        for part in [profile] + (profile.get("layers") or []):
            for table in settings_store.TABLES:
                for act in (part.get(table) or {}).values():
                    act = settings_store.unpack_action(act)
                    for step in (act["v"] if act["t"] in ("cycle", "macro") else (act,)):
                        if step["t"] == "text":
                            n += len(step["v"])
                        if step["t"] == "web" or step["t"] == "app":   # (an address / name is kept like a text)
                            n += len(step["v"][0])
    return n


def _cost(doc, profiles):
    file_bytes, text = settings_store.json_size(doc), _texts(profiles)
    return MAP_COST * max(file_bytes - text, 0) + TEXT_COST * text


def size(profiles, active, name):
    """Memory the set takes on the board (estimated, bytes), without building its text."""
    return _cost(_doc(profiles, active, name), profiles)


def save(did, profiles, active, name):
    """Write the device's set. Raises ValueError (too large / no device) or OSError."""
    fid = file_id(did)
    if fid is None:
        raise ValueError("no device to save the profiles for - plug it in first")
    doc = _doc(profiles, active, name)
    cost = _cost(doc, profiles)
    length = settings_store.json_size(doc)
    if cost > MEMORY_BUDGET or length > MAX_FILE:
        raise ValueError("this device's profiles need too much memory (about %d of %d KB) - remove some "
                         "mappings, layers or text" % ((cost + 1023) // 1024, MEMORY_BUDGET // 1024))
    try:
        os.mkdir(DIR)
    except OSError:
        pass                              # already there
    tmp, final = _path(fid, True), _path(fid)
    with open(tmp, "wb") as f:            # written in small pieces: no big block of memory needed
        out = settings_store.ChunkWriter(lambda _at, data: f.write(data))
        settings_store.emit_json(out, doc)
        out.flush()
    try:
        os.remove(final)                  # FAT can't rename onto an existing file
    except OSError:
        pass
    os.rename(tmp, final)
    try:
        os.sync()
    except (AttributeError, OSError):
        pass
    return cost


def remove(did):
    fid = file_id(did)
    for path in (_path(fid), _path(fid, True)):
        try:
            os.remove(path)
        except (OSError, TypeError):
            pass


def _file_name(path):
    """The "name" at the top level of a profile file, read in small pieces (the file isn't
    parsed: it can take more memory than is free while the page is connected)."""
    depth, in_str, esc, key, text, want = 0, False, False, None, [], False
    with open(path) as f:
        while True:
            chunk = f.read(128)
            if not chunk:
                return ""
            for ch in chunk:
                if in_str:
                    if esc:
                        esc = False
                        text.append(ch)
                        continue
                    if ch == "\\":
                        esc = True
                        continue
                    if ch != '"':
                        if len(text) < 60:
                            text.append(ch)
                        continue
                    in_str = False
                    word = "".join(text)
                    if want:
                        return word
                    key = word if depth == 1 else None
                    continue
                if ch == '"':
                    in_str, text = True, []
                    continue
                if ch == ":":
                    want = key == "name"
                    continue
                if ch in "{[":
                    depth += 1
                if ch in "}]":
                    depth -= 1
                if ch in ",{[}]":
                    key, want = None, False


def saved_list():
    """[{"id": '0x046d:0xc52b', "name": ..., "bytes": n}] for every device with a file."""
    out = []
    try:
        names = os.listdir(DIR)
    except OSError:
        return out
    for entry in names:
        fid = entry[:-5] if entry.endswith(".json") else None
        if fid is None or file_id(did_of(fid)) != fid:
            continue
        name, size = "", 0
        try:
            size = os.stat(_path(fid))[6]
            name = _file_name(_path(fid))
        except (OSError, ValueError, AttributeError):
            pass
        gc.collect()
        out.append({"id": did_of(fid), "name": name, "bytes": size})
    return out
