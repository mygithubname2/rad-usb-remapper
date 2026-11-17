# hidparse_desc.py -- HID report-descriptor parser (see hidparse.py for what is understood).
#
# Loaded only while a descriptor is parsed (a device attached, a battery check, USB scan) and
# dropped afterwards by hidparse.parse_full: the board doesn't keep the parser's code in memory
# while the mouse is in use.
#
# Descriptors come from whatever is plugged in, so every count and size is capped: a
# faulty or malicious descriptor can't make the board loop for ages or run out of memory.
#
# Item-tag handling uses dispatch tables (no if/elif chains).

from hidparse import Report, REPORT_INPUT, REPORT_OUTPUT, REPORT_FEATURE

PAGE_GD = 0x01
PAGE_KEYBOARD = 0x07
PAGE_BUTTON = 0x09
PAGE_CONSUMER = 0x0C
AC_PAN = 0x0238
AXIS_BY_GD_USAGE = {0x30: "x", 0x31: "y", 0x38: "wheel"}
# battery fields: (usage page << 16) | usage -> what it holds
BATTERY_USAGES = {(0x06 << 16) | 0x20: "level", (0x85 << 16) | 0x65: "level", (0x85 << 16) | 0x66: "level",
                  (0x85 << 16) | 0x44: "charging"}
_SIZES = (0, 1, 2, 4)
# limits for untrusted descriptors
MAX_FIELD_BITS = 32       # one field (Report Size); larger ones are skipped
MAX_FIELDS = 512          # variable fields handled per item (an NKRO key bitmap has 256)
MAX_SLOTS = 64            # array slots per item (a keyboard has 6-32)
MAX_STACK = 16            # Push nesting


class _Parser:
    def __init__(self):
        self.g = {"page": 0, "lmin": 0, "lmax": 0, "size": 0, "count": 0, "id": 0}
        self.stack = []
        self.usages = []
        self.umin = None
        self.umax = None
        self.reports = {}
        self.uses_ids = False
        self.sizes = {}             # (report type, report ID) -> bits so far
        self.vendor_ids = set()     # report IDs used on vendor pages (>= 0xFF00)
        self.fields = []            # battery fields (see BATTERY_USAGES)
        self.globals = {
            0x0: self._g_page, 0x1: self._g_lmin, 0x2: self._g_lmax, 0x7: self._g_size,
            0x8: self._g_id, 0x9: self._g_count, 0xA: self._g_push, 0xB: self._g_pop,
        }
        self.locals = {0x0: self._l_usage, 0x1: self._l_umin, 0x2: self._l_umax}
        self.mains = {0x8: self._m_input, 0x9: self._m_output, 0xB: self._m_feature}
        self.types = {0: self._main, 1: self._global, 2: self._local, 3: _ignore}

    # ---------------------------------------------------------------- items
    def run(self, desc):
        i = 0
        n = len(desc)
        while i < n:
            prefix = desc[i]
            if prefix == 0xFE:                      # long item: skip
                i += 3 + (desc[i + 1] if i + 1 < n else 0)
                continue
            size = _SIZES[prefix & 3]
            data = bytes(desc[i + 1:i + 1 + size])
            i += 1 + size
            if len(data) != size:
                break                               # truncated descriptor
            uval = int.from_bytes(data, "little") if size else 0
            sval = uval - ((uval >> (8 * size - 1)) << (8 * size)) if size else 0
            self.types[(prefix >> 2) & 3](prefix >> 4, uval, sval, size)
        for rep in self.reports.values():
            rep.finish()
        return self.reports, self.uses_ids

    def _main(self, tag, uval, _sval, _size):
        self.mains.get(tag, self._m_skip)(uval)
        self.usages = []
        self.umin = self.umax = None

    def _global(self, tag, uval, sval, size):
        self.globals.get(tag, _ignore)(uval, sval, size)

    def _local(self, tag, uval, _sval, size):
        full = uval if size == 4 else (self.g["page"] << 16) | uval
        self.locals.get(tag, _ignore)(full)

    # globals
    def _g_page(self, u, s, n):
        self.g["page"] = u

    def _g_lmin(self, u, s, n):
        self.g["lmin"] = s

    def _g_lmax(self, u, s, n):
        # logical max is unsigned when logical min is not negative
        self.g["lmax"] = (u, s)[self.g["lmin"] < 0]

    def _g_size(self, u, s, n):
        self.g["size"] = u

    def _g_count(self, u, s, n):
        self.g["count"] = u

    def _g_id(self, u, s, n):
        self.g["id"] = u
        self.uses_ids = True

    def _g_push(self, u, s, n):
        if len(self.stack) < MAX_STACK:
            self.stack.append(dict(self.g))

    def _g_pop(self, u, s, n):
        if self.stack:
            self.g = self.stack.pop()

    # locals
    def _l_usage(self, full):
        self.usages.append(full)

    def _l_umin(self, full):
        self.umin = full

    def _l_umax(self, full):
        self.umax = full

    # mains: input / output / feature items (collections need nothing)
    def _m_skip(self, _flags):
        pass

    def _m_output(self, flags):
        self._field_item(REPORT_OUTPUT, flags)

    def _m_feature(self, flags):
        self._field_item(REPORT_FEATURE, flags)

    def _field_item(self, rtype, flags):
        """Note the report's size, vendor report IDs and battery fields; returns the item's
        bit offset in its report."""
        g = self.g
        key = (rtype, g["id"])
        offset = self.sizes.get(key, 0)
        self.sizes[key] = offset + g["size"] * g["count"]
        if g["page"] >= 0xFF00:
            self.vendor_ids.add(g["id"])
        if not flags & 0x01 and 0 < g["size"] <= MAX_FIELD_BITS:     # data, not padding
            usages = self._usage_list(g["count"])
            for k in range(min(g["count"], len(usages))):
                what = BATTERY_USAGES.get(usages[k])
                if what:
                    self.fields.append({"what": what, "type": rtype, "rid": g["id"],
                                        "offset": offset + k * g["size"], "size": g["size"],
                                        "min": g["lmin"], "max": g["lmax"]})
        return offset

    def _report(self):
        rid = self.g["id"]
        rep = self.reports.get(rid)
        if rep is None:
            rep = self.reports[rid] = Report(rid)
        return rep

    def _usage_list(self, count):
        if self.usages:
            return self.usages
        if self.umin is not None and self.umax is not None and self.umax >= self.umin:
            last = min(self.umax, self.umin + max(min(count, MAX_FIELDS), 1) - 1)
            return list(range(self.umin, last + 1))
        return []

    def _m_input(self, flags):
        g = self.g
        rep = self._report()
        offset = self._field_item(REPORT_INPUT, flags)
        size, count = g["size"], g["count"]
        rep.bits = offset + size * count
        if flags & 0x01 or size == 0 or count == 0 or size > MAX_FIELD_BITS:   # padding / unusable
            return
        if flags & 0x02:                                    # variable
            # fields past the end of the usage list would all repeat its last usage (the
            # same button or axis again): nothing to add, so they are left out
            usages = self._usage_list(count)
            for k in range(min(count, MAX_FIELDS, len(usages))):
                _add_variable(rep, usages[k], offset + k * size, size, g["lmin"] < 0)
            return
        umin = self.umin if self.umin is not None else (self.usages[0] if self.usages else 0)
        page = umin >> 16
        spec = (offset, size, min(count, MAX_SLOTS), umin & 0xFFFF, g["lmin"])
        {PAGE_KEYBOARD: rep.key_arrays, PAGE_CONSUMER: rep.cons_arrays}.get(page, [])\
            .append(spec)


def _ignore(*_args):
    pass


def _var_button(rep, uid, offset, size, signed):
    if size == 1 and 1 <= uid <= 16:
        rep.buttons.append((offset, 1, uid - 1))


def _var_gd(rep, uid, offset, size, signed):
    name = AXIS_BY_GD_USAGE.get(uid)
    if name and name not in rep.axes:
        rep.axes[name] = (offset, size, signed)


def _var_key(rep, uid, offset, size, signed):
    if size == 1 and uid:
        rep.key_bits.append((offset, 1, uid))


def _var_consumer(rep, uid, offset, size, signed):
    if uid == AC_PAN:
        rep.axes.setdefault("pan", (offset, size, signed))
        return
    if size == 1 and uid:
        rep.cons_bits.append((offset, uid))


_VARIABLE_BY_PAGE = {
    PAGE_BUTTON: _var_button,
    PAGE_GD: _var_gd,
    PAGE_KEYBOARD: _var_key,
    PAGE_CONSUMER: _var_consumer,
}


def _add_variable(rep, usage, offset, size, signed):
    _VARIABLE_BY_PAGE.get(usage >> 16, _ignore)(rep, usage & 0xFFFF, offset, size, signed)


def parse_full(desc):
    """Parse a report descriptor -> ({report_id: Report}, uses_report_ids, info) where
    info = {"fields": battery fields, "vendor_ids": set, "sizes": {(type, rid): bits}}."""
    parser = _Parser()
    reports, uses_ids = parser.run(desc)
    useful = {rid: rep for rid, rep in reports.items() if rep.useful()}
    return useful, uses_ids, {"fields": parser.fields, "vendor_ids": parser.vendor_ids, "sizes": parser.sizes}
