# hidparse.py -- HID report decoder (the descriptor parser is in hidparse_desc.py)
#
# Pure Python (no hardware imports) so it can be unit-tested on a desktop.
#
# Gaming mice such as the Razer Naga V3 Pro do not fit in the 3-byte "boot"
# mouse report: they send 16-bit movement, a horizontal wheel, more buttons and
# put their side buttons on separate keyboard interfaces that use report IDs
# and key bitmaps.  To read every control, the firmware fetches each
# interface's HID report descriptor, parses it here, and builds a decoder for
# every report ID.  Decoding is driven by usages, so any mouse/keyboard whose
# descriptor uses standard usages is understood:
#
#   Button page 1..16                  -> mouse buttons 0..15
#   Generic Desktop X / Y / Wheel      -> movement and vertical scroll
#   Consumer AC Pan (0x238)            -> horizontal scroll / tilt
#   Keyboard page (bits or arrays)     -> key codes (incl. NKRO bitmaps)
#   other Consumer usages              -> media keys
#
# The same pass also notes what the battery readers need (battery.py): battery fields
# in input / feature reports, report IDs on vendor pages, and each report's size.
#
# Descriptors come from whatever is plugged in, so every count and size is capped: a
# faulty or malicious descriptor can't make the board loop for ages or run out of memory.
#
# Item-tag handling uses dispatch tables (no if/elif chains).

ERROR_ROLLOVER = 0x01
REPORT_INPUT, REPORT_OUTPUT, REPORT_FEATURE = 1, 2, 3


def get_bits(buf, offset, size, base=0):
    """Unsigned little-endian bit field at bit `offset` from byte `base` of buf, or None if it
    lies outside buf. Reads the bytes one by one: no slice or copy, so no memory is used for
    the fields of a mouse report (up to 1000 a second)."""
    start = base + (offset >> 3)
    end = base + ((offset + size + 7) >> 3)
    if end > len(buf):
        return None
    value = 0
    shift = 0
    while start < end:
        value |= buf[start] << shift
        shift += 8
        start += 1
    return (value >> (offset & 7)) & ((1 << size) - 1)


def get_signed(buf, offset, size, base=0):
    value = get_bits(buf, offset, size, base)
    if value is None:
        return 0
    return value - ((value >> (size - 1)) << size)


class Report:
    """Decoder for one report ID of one interface."""

    def __init__(self, report_id):
        self.report_id = report_id
        self.bits = 0               # running bit offset of the input report
        self.buttons = []           # (offset, count, first_index)
        self.axes = {}              # name -> (offset, size, signed)
        self.key_bits = []          # (offset, count, first_usage)
        self.key_arrays = []        # (offset, size, count, usage_min, logical_min)
        self.cons_bits = []         # (offset, usage)
        self.cons_arrays = []       # (offset, size, count, usage_min, logical_min)
        self.axis_list = ()         # (slot, offset, size, signed) for x, y, wheel, pan: made by finish()
        self.mouse_values = [0, 0, 0, 0, 0]   # buttons, x, y, wheel, pan: reused for every report

    # ---------------------------------------------------------------- summary
    def has_mouse(self):
        return bool(self.buttons or self.axes)

    def has_keys(self):
        return bool(self.key_bits or self.key_arrays)

    def has_consumer(self):
        return bool(self.cons_bits or self.cons_arrays)

    def useful(self):
        return self.has_mouse() or self.has_keys() or self.has_consumer()

    def describe(self):
        parts = []
        nbtn = sum(c for _o, c, _f in self.buttons)
        if self.has_mouse():
            axes = ",".join("%s%d" % (n, self.axes[n][1]) for n in ("x", "y", "wheel", "pan") if n in self.axes)
            parts.append("mouse(%d btn %s)" % (nbtn, axes))
        if self.has_keys():
            parts.append("keys(%d bits, %d slots)" % (sum(c for _o, c, _u in self.key_bits),
                                                       sum(a[2] for a in self.key_arrays)))
        if self.has_consumer():
            parts.append("media")
        return "id%d:%s" % (self.report_id, "+".join(parts) or "-")

    # ---------------------------------------------------------------- decode
    def decode_mouse(self, buf, base=0):
        """[buttons, x, y, wheel, pan] of a report whose data starts at byte `base` of buf.
        The same list is filled in for every report (no memory used per report)."""
        buttons = 0
        for offset, count, first in self.buttons:
            value = get_bits(buf, offset, count, base)
            buttons |= (value or 0) << first
        values = self.mouse_values
        values[0] = buttons & 0xFFFF
        for slot, offset, size, signed in self.axis_list:
            if signed:
                values[slot] = get_signed(buf, offset, size, base)
            else:
                values[slot] = get_bits(buf, offset, size, base) or 0
        return values

    def decode_keys(self, buf, base=0):
        """Set of pressed keyboard usages, or None for a rollover/phantom report."""
        codes = set()
        for offset, count, first in self.key_bits:
            value = get_bits(buf, offset, count, base) or 0
            index = first
            while value:
                byte = value & 0xFF
                bit = 0
                while byte:
                    if byte & 1:
                        codes.add(index + bit)
                    byte >>= 1
                    bit += 1
                value >>= 8
                index += 8
        for offset, size, count, umin, lmin in self.key_arrays:
            for k in range(count):
                value = get_bits(buf, offset + k * size, size, base)
                usage = umin + (value or 0) - lmin
                if value is None or usage == 0:
                    continue
                if usage == ERROR_ROLLOVER:
                    return None
                codes.add(usage)
        codes.discard(0)
        return codes

    def decode_consumer(self, buf, base=0):
        usages = set()
        for offset, usage in self.cons_bits:
            if get_bits(buf, offset, 1, base):
                usages.add(usage)
        for offset, size, count, umin, lmin in self.cons_arrays:
            for k in range(count):
                value = get_bits(buf, offset + k * size, size, base)
                usage = umin + (value or 0) - lmin
                if value and usage > 0:
                    usages.add(usage)
        return usages

    def finish(self):
        """Merge single bits into contiguous runs (faster decoding)."""
        self.buttons = _merge_runs(self.buttons)
        self.key_bits = _merge_runs(self.key_bits)
        self.axis_list = tuple((slot, self.axes[name][0], self.axes[name][1], self.axes[name][2])
                               for slot, name in ((1, "x"), (2, "y"), (3, "wheel"), (4, "pan")) if name in self.axes)


def _merge_runs(items):
    """[(offset, count, first)] -> the same bits as few runs as possible."""
    runs = []
    for offset, count, first in sorted(items, key=lambda b: b[0]):
        last = runs[-1] if runs else None
        if last and last[0] + last[1] == offset and last[2] + last[1] == first:
            runs[-1] = (last[0], last[1] + count, last[2])
            continue
        runs.append((offset, count, first))
    return runs


def parse_full(desc):
    """Parse a report descriptor -> ({report_id: Report}, uses_report_ids, info) (see
    hidparse_desc.py). The parser is loaded for the call and dropped again: it is needed only
    when a device is attached (and for battery checks), not while the mouse is in use."""
    import sys
    import hidparse_desc
    try:
        return hidparse_desc.parse_full(desc)
    finally:
        sys.modules.pop("hidparse_desc", None)


def parse(desc):
    """Parse a report descriptor -> ({report_id: Report}, uses_report_ids)."""
    return parse_full(desc)[:2]


# ---------------------------------------------------------------- boot protocol
def boot_mouse():
    rep = Report(0)
    rep.buttons = [(0, 8, 0)]
    rep.axes = {"x": (8, 8, True), "y": (16, 8, True), "wheel": (24, 8, True)}
    rep.finish()
    return {0: rep}, False


def boot_keyboard():
    rep = Report(0)
    rep.key_bits = [(0, 8, 0xE0)]
    rep.key_arrays = [(16, 8, 6, 0, 0)]
    return {0: rep}, False


BOOT_DECODERS = {1: boot_keyboard, 2: boot_mouse}
