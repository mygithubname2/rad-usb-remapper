# hostinput.py -- USB host side: find, attach and read mice / keyboards
#
# Every HID interface of every device on the USB-A port is examined:
#   1. its HID report descriptor is fetched and parsed (hidparse.py) and the
#      interface is read in REPORT protocol -- full resolution movement,
#      horizontal wheel, all buttons, extra keyboard interfaces (gaming mice);
#   2. if the descriptor can't be read/understood but the interface is a
#      standard boot mouse/keyboard, it falls back to BOOT protocol;
#   3. anything else (vendor/lighting interfaces, hubs, drives) is ignored.
#
# Hot-plug strategy
#   * While nothing is attached the bus is scanned every SCAN_IDLE_MS.
#   * While attached, the list of devices on the bus is compared every
#     SCAN_ATTACHED_MS.  Any difference (unplug, new device, swap) causes a
#     clean detach of everything followed by a fresh attach.
#   * A USB error while reading means the device went away: detach at once,
#     rescan shortly after.
#   * Every detach calls on_change() so the remapper releases all keys and
#     buttons -- nothing is left "stuck down" on the computer.

import gc
import time
import usb.core

import hidparse

SCAN_IDLE_MS = 1000
SCAN_ATTACHED_MS = 2000
RESCAN_AFTER_ERROR_MS = 300
RETRY_MS = 1000                # a device that failed to attach is retried this often...
MAX_RETRIES = 6                # ...this many times (wireless receivers can be slow to wake)
PORT_OFF_S = 0.4               # how long the port power stays off during a restart
READ_TIMEOUT_MS = 2            # per endpoint per loop; mice accumulate motion meanwhile
# A read of an endpoint with nothing to say waits the whole timeout and then cancels the transfer
# (about 2-3 ms in all). A Razer Naga has 3 endpoints: reading all of them every pass made the
# board read the mouse only ~80 times a second. So an endpoint is read every pass only while it
# is in use (and only the last one to send waits the full 2 ms); quiet ones (e.g. a mouse's
# keyboard interface) are checked one per pass, each at most every QUIET_CHECK_MS (the first
# key press there waits up to about that long, then that endpoint is read every pass).
ACTIVE_MS = 500                # an endpoint that sent something this recently is read every pass
QUIET_CHECK_MS = 20
QUIET_TIMEOUT_MS = 1           # a quiet endpoint's read waits only until the next millisecond tick (a report
                               # it has waiting arrives within that): about half the time of a 2 ms read
MAX_REPORT_DESC = 2048
MAX_CONFIG_DESC = 1024         # a device's own length claim is not trusted beyond this

HID_CLASS = 3
BOOT_SUBCLASS = 1

_TimeoutError = getattr(usb.core, "USBTimeoutError", None)
if _TimeoutError is None:              # very old builds: never matches anything real
    class _TimeoutError(Exception):
        pass


from clock import now_ms          # noqa: F401  (shared clock; other modules import it from here too)


def _safe_attr(dev, name, default=None):
    try:
        value = getattr(dev, name)
    except Exception:          # string descriptors need a control transfer that can fail
        return default
    return default if value is None else value


def clean_name(text):
    """Tidy a USB name string: CircuitPython sometimes appends the language ID
    (0x0409 = US English, shown as "\u0409") or padding to it."""
    if not isinstance(text, str):
        return ""
    text = text.split("\x00")[0].replace("\u0409", "")
    return "".join(ch for ch in text if ord(ch) >= 32 and ord(ch) != 0xFFFD).strip()


def packet_size(iface):
    """Read exactly one packet: the endpoint's max packet size (1-64).

    A USB read only completes when the requested length arrives or a SHORT packet
    (smaller than the max packet size) does. A mouse with a 4-byte max packet sends
    full 4-byte packets, so asking for more (we used to ask for at least 8) never
    completes - every report timed out and was lost (e.g. Dell MS116, 413c:301a)."""
    size = iface.get("max_packet") or 8
    return max(1, min(size, 64))


DESC_TRIES = 4          # a receiver busy with its paired devices sometimes stalls (Pipe error)


def read_report_descriptor(dev, iface):
    """GET_DESCRIPTOR(report), tried a few times: a stall (Pipe error) or a short lack of
    memory is often gone a moment later."""
    for attempt in range(DESC_TRIES):
        try:
            return get_report_descriptor(dev, iface["number"], iface["report_len"] or 256)
        except MemoryError:
            gc.collect()
            if attempt == DESC_TRIES - 1:
                raise
        except Exception:
            if attempt == DESC_TRIES - 1:
                raise
            time.sleep(0.05 * (attempt + 1))
    return None


def device_key(dev):
    ports = _safe_attr(dev, "port_numbers", ()) or ()
    return (dev.idVendor, dev.idProduct, tuple(ports))


def get_config_descriptor(dev):
    """Read the full configuration descriptor (GET_DESCRIPTOR, CONFIGURATION 0)."""
    head = bytearray(9)
    dev.ctrl_transfer(0x80, 6, 0x0200, 0, head)
    total = head[2] | (head[3] << 8)
    full = bytearray(min(max(total, 9), MAX_CONFIG_DESC))
    dev.ctrl_transfer(0x80, 6, 0x0200, 0, full)
    return full


def get_report_descriptor(dev, interface, length):
    buf = bytearray(min(max(length, 1), MAX_REPORT_DESC))
    got = dev.ctrl_transfer(0x81, 6, 0x2200, interface, buf)
    return bytes(buf[:got]) if isinstance(got, int) and 0 < got <= len(buf) else bytes(buf)


def find_hid_interfaces(desc):
    """Return a list of dicts, one per HID interface with an interrupt IN endpoint:
    {number, subclass, protocol, report_len, endpoint, max_packet[, out_endpoint]}"""
    found = []
    current = None
    i = 0
    n = len(desc)
    while i + 1 < n:
        length = desc[i]
        dtype = desc[i + 1]
        if length < 2:
            break
        if dtype == 4 and i + 7 < n:                          # INTERFACE
            current = None
            if desc[i + 5] == HID_CLASS:
                current = {"number": desc[i + 2], "subclass": desc[i + 6],
                           "protocol": desc[i + 7], "report_len": 0}
        if dtype == 0x21 and current is not None and i + 8 < n:   # HID class descriptor
            current["report_len"] = desc[i + 7] | (desc[i + 8] << 8)
        if dtype == 5 and current is not None and i + 5 < n:  # ENDPOINT
            address = desc[i + 2]
            interrupt = (desc[i + 3] & 0x03) == 0x03
            if interrupt and (address & 0x80) and "endpoint" not in current:   # first IN endpoint
                current["endpoint"] = address
                current["max_packet"] = desc[i + 4] | (desc[i + 5] << 8)
                found.append(current)
            if interrupt and not (address & 0x80) and "out_endpoint" not in current:
                current["out_endpoint"] = address           # used to send vendor requests
        i += length
    return found


def report_kinds(reports):
    kinds = []
    for rep in reports.values():
        for flag, name in ((rep.has_mouse(), "mouse"), (rep.has_keys(), "keyboard"),
                           (rep.has_consumer(), "media")):
            if flag and name not in kinds:
                kinds.append(name)
    return kinds


def _looks_like_keyboard(opened_all):
    """A combo receiver / keyboard with a touchpad offers a boot KEYBOARD interface
    first; a mouse with extra keys offers a boot MOUSE interface (or none)."""
    for iface, (_reports, _uses_ids, _mode) in opened_all:
        if iface["subclass"] == BOOT_SUBCLASS and iface["protocol"] in (1, 2):
            return iface["protocol"] == 1
    return False


class Endpoint:
    def __init__(self, dev, iface, reports, uses_ids, mode, sid, key_table):
        self.dev = dev
        self.interface = iface["number"]
        self.address = iface["endpoint"]
        self.buf = bytearray(packet_size(iface))
        self.view = memoryview(self.buf)     # made once: not a new one for every report
        self.views = {}                # report length -> view of that much of buf
        self.data_at = -ACTIVE_MS      # now_ms() of its last report
        self.check_at = 0              # when quiet: next time it is read
        self.reads = 0                 # for "Measure smoothness": reads, reports, time spent
        self.got = 0
        self.ms = 0
        self.reports = reports
        self.uses_ids = uses_ids
        self.mode = mode
        self.sid = sid
        self.key_table = key_table     # "mkey" for keys sent by a mouse, else "key"
        self.kinds = report_kinds(reports)
        self.priority = "mouse" in self.kinds


class HostInput:
    def __init__(self, log, on_change, port_power=None):
        self.log = log                 # log(msg, level)
        self.on_change = on_change     # called after devices are dropped / added
        self.port_power = port_power   # function(on) switching the USB-A port's 5 V, or None
        self.endpoints = []
        self.devices = []              # descriptive dicts for the web page
        self.targets = []              # per attached device: dev, name, ids, every HID interface
                                       # with its report descriptor (for battery.py)
        self._sink = None              # last sink given to poll(), so pump() can keep input flowing
        self.last_input = 0            # now_ms() of the last report from a device (movement, press...)
        self.report_count = 0          # reports read (for "Measure smoothness")
        self.timing = False            # True while measuring: time each endpoint's reads
        self._no_raise = True          # read(..., raise_on_timeout=False) works (no exception per empty read)
        self._hot = None               # endpoint of the last report: always read
        self.on_pump = None            # called after each pump(), e.g. to keep the status LED current
        self._collect = None           # list collecting unclaimed reports during a vendor query
        self._signature = ()
        self._next_scan = 0
        self._next_sid = 1
        self._retries = {}             # device_key -> attempts so far, for failed attaches

    # ------------------------------------------------------------ summary
    def device_type(self):
        kinds = []
        for info in self.devices:
            label = info["type"]
            if label not in kinds:
                kinds.append(label)
        return " + ".join(kinds) if kinds else "none"

    # ------------------------------------------------------------ scanning
    def _bus_devices(self):
        try:
            return list(usb.core.find(find_all=True) or [])
        except Exception as err:
            self.log("USB bus scan failed: %r" % (err,), "error")
            return []

    def service(self):
        """Call often. Handles plug / unplug / swap detection and retries."""
        t = now_ms()
        if t < self._next_scan:
            return
        self._next_scan = t + (SCAN_ATTACHED_MS if self.endpoints else SCAN_IDLE_MS)
        devices = self._bus_devices()
        signature = tuple(sorted(device_key(d) for d in devices))
        if signature == self._signature:
            self._retry_failed(devices)
            return
        if self.endpoints or self.devices:
            self.detach_all("device list changed")
        self._signature = signature
        self._retries = {}
        self.log(("Nothing on the USB-A port",
                  "USB-A port: %d device(s): %s" % (len(devices), ", ".join(
                      "%04x:%04x" % (d.idVendor, d.idProduct) for d in devices)))[bool(devices)], "info")
        for dev in devices:
            self._try_attach(dev, 0)
        self.on_change()

    # ------------------------------------------------------------ port power
    # Some devices (e.g. certain wireless receivers) only intermittently pass the
    # USB handshake on this software-driven port.  Switching the port's power off
    # and on (Restart USB-A port on the page) makes the device try again from scratch.
    def restart_port(self):
        """Switch the USB-A port's 5 V off and on so attached devices re-connect."""
        if not self.port_power:
            self.log("USB-A port power can't be controlled on this board/firmware", "warn")
            return False
        if self.endpoints or self.devices:
            self.detach_all("USB-A port restart")
        self._signature = ()
        self._retries = {}
        try:
            self.port_power(False)
            time.sleep(PORT_OFF_S)
            self.port_power(True)
        except Exception as err:
            self.log("USB-A port restart failed: %r" % (err,), "error")
            return False
        self._next_scan = now_ms() + 1000                 # look for the device soon after power-up
        self.log("USB-A port restarted", "ok")
        return True

    def _try_attach(self, dev, attempt):
        """Attach one device; schedule a retry if it failed. Returns True if attached."""
        result = self._attach(dev)
        if result is False and attempt < MAX_RETRIES:
            self._retries[device_key(dev)] = attempt + 1
            self._next_scan = min(self._next_scan, now_ms() + RETRY_MS)
            self.log("  will retry %04x:%04x (attempt %d of %d)"
                     % (dev.idVendor, dev.idProduct, attempt + 2, MAX_RETRIES + 1), "info")
        if result is False and attempt >= MAX_RETRIES:
            self.log("Gave up on %04x:%04x - click USB scan on the page for details"
                     % (dev.idVendor, dev.idProduct), "warn")
        return bool(result)

    def _retry_failed(self, devices):
        if not self._retries:
            return
        pending, self._retries = self._retries, {}
        attached = False
        for dev in devices:
            attempt = pending.get(device_key(dev))
            if attempt is not None:
                attached = self._try_attach(dev, attempt) or attached
        if attached:
            self.endpoints.sort(key=lambda e: not e.priority)
            self.on_change()

    def detach_all(self, reason):
        if not (self.endpoints or self.devices):
            return
        gone = self.devices
        self.endpoints = []
        self._hot = None
        self.devices = []
        self.targets = []
        self._signature = ()
        self.on_change()               # releases every held key/button, updates the page
        for info in gone:
            self.log("Detached %s %s (%s)" % (info["type"], info["name"], reason), "warn")

    # ------------------------------------------------------------ attaching
    def _open_interface(self, dev, iface, vid_pid):
        """Return (reports, uses_ids, mode) or None."""
        number = iface["number"]
        reports, uses_ids, mode = {}, False, "report"
        try:
            desc = read_report_descriptor(dev, iface)
            iface["desc"] = desc               # kept for battery.py (vendor / battery fields)
            reports, uses_ids, iface["scan"] = hidparse.parse_full(desc)   # "scan": battery_base.scan
        except Exception as err:
            self.log("%s if%d: report descriptor unreadable after %d tries (%r)"
                     % (vid_pid, number, DESC_TRIES, err), "info")
        boot = hidparse.BOOT_DECODERS.get(iface["protocol"]) if iface["subclass"] == BOOT_SUBCLASS else None
        if iface["subclass"] == BOOT_SUBCLASS and reports:
            # make sure the device is in report protocol (CircuitPython may have set boot).
            # Devices start in report protocol, so if one refuses the request the parsed
            # layout is still the right one -- falling back to boot would misread devices
            # whose reports don't start like a boot report (e.g. Kensington SlimBlade).
            if not self._control(dev, 0x0B, 1, number):
                self.log("%s if%d: SET_PROTOCOL(report) refused - using report layout anyway"
                         % (vid_pid, number), "info")
        if not reports and boot:
            reports, uses_ids = boot()
            mode = "boot"
            self._control(dev, 0x0B, 0, number)
        if not reports:
            return None
        self._control(dev, 0x0A, 0, number)      # SET_IDLE(0): report on change only
        return reports, uses_ids, mode

    def _control(self, dev, request, value, interface):
        try:
            dev.ctrl_transfer(0x21, request, value, interface)
            return True
        except Exception:
            return False

    def _attach(self, dev):
        vid_pid = "%04x:%04x" % (dev.idVendor, dev.idProduct)
        try:
            interfaces = find_hid_interfaces(get_config_descriptor(dev))
        except Exception as err:
            self.log("Could not read descriptors of %s: %r" % (vid_pid, err), "error")
            return False
        if not interfaces:
            self.log("Ignoring %s: no HID input interface (hub or other device)" % vid_pid, "info")
            return None
        for iface in interfaces:
            try:
                if dev.is_kernel_driver_active(iface["number"]):
                    dev.detach_kernel_driver(iface["number"])  # stop CircuitPython using it as REPL keyboard
            except Exception:
                pass
        try:
            dev.set_configuration()
        except Exception as err:
            self.log("set_configuration failed for %s: %r" % (vid_pid, err), "error")
            return False

        product = clean_name(_safe_attr(dev, "product", ""))
        maker = clean_name(_safe_attr(dev, "manufacturer", ""))
        name = (product if maker and product.startswith(maker) else
                " ".join(x for x in (maker, product) if x)) or "(unnamed)"
        opened_all = []
        for iface in interfaces:
            opened = self._open_interface(dev, iface, vid_pid)
            if opened is not None:
                opened_all.append((iface, opened))
        # Keys sent by a device that is also a mouse (e.g. a gaming mouse whose side
        # buttons type 1, 2, 3...) are kept apart from a real keyboard's keys, so
        # they can be remapped without changing the keyboard.
        is_mouse = any(rep.has_mouse() for _i, (reports, _u, _m) in opened_all for rep in reports.values())
        key_table = ("key", "mkey")[is_mouse]

        added = []
        kinds = []
        for iface, (reports, uses_ids, mode) in opened_all:
            ep = Endpoint(dev, iface, reports, uses_ids, mode, self._next_sid, key_table)
            self._next_sid += 1
            self.endpoints.append(ep)
            kinds.extend(k for k in ep.kinds if k not in kinds)
            added.append({"interface": iface["number"], "endpoint": "0x%02x" % ep.address,
                          "mode": mode, "kinds": "+".join(ep.kinds),
                          "reports": " ".join(r.describe() for r in reports.values())})
        if not added:
            self.log("%s %s: no usable mouse/keyboard interface yet" % (vid_pid, name), "warn")
            return False
        # a mouse whose extra buttons arrive as keys is still "a mouse"
        dtype = ("mouse" if is_mouse and not _looks_like_keyboard(opened_all) else
                 " + ".join(k for k in ("keyboard", "mouse") if k in kinds) or "media")
        self.endpoints.sort(key=lambda e: not e.priority)
        if self._hot is None:
            self._hot = self.endpoints[0]      # read every pass until another one sends something
        info = {
            "type": dtype,
            "name": name,
            "vid": "0x%04x" % dev.idVendor,
            "pid": "0x%04x" % dev.idProduct,
            "mouse_keys": is_mouse and "keyboard" in kinds,
            "interfaces": added,
        }
        self.devices.append(info)
        self.targets.append({"key": device_key(dev), "dev": dev, "name": name, "vid": dev.idVendor,
                             "pid": dev.idProduct, "interfaces": interfaces})
        gc.collect()
        self.log("Attached %s: %s [%s]%s (%d bytes free)" % (dtype, name, vid_pid,
                                              ("", " - its key presses are listed as 'from mouse'")[info["mouse_keys"]],
                                              gc.mem_free()), "ok")
        for a in added:
            self.log("  if%d ep %s %s mode: %s" % (a["interface"], a["endpoint"], a["mode"], a["reports"]), "info")
        return True

    # ------------------------------------------------------------ capabilities
    def capabilities(self):
        """What the attached device(s) can report, from their HID descriptors.
        The web page lists only these inputs (plus anything pressed or mapped)."""
        caps = {"buttons": 0, "move": False, "wheel": False, "pan": False,
                "keyboard": False, "mouse_keys": False}
        for ep in self.endpoints:
            caps["mouse_keys"] = caps["mouse_keys"] or ep.key_table == "mkey"
            for rep in ep.reports.values():
                for _offset, count, first in rep.buttons:
                    caps["buttons"] = max(caps["buttons"], first + count)
                caps["move"] = caps["move"] or ("x" in rep.axes or "y" in rep.axes)
                caps["wheel"] = caps["wheel"] or "wheel" in rep.axes
                caps["pan"] = caps["pan"] or "pan" in rep.axes
                caps["keyboard"] = caps["keyboard"] or rep.has_keys()
        caps["buttons"] = min(caps["buttons"], 16)
        return caps

    # ------------------------------------------------------------ diagnostics
    def scan_report(self, log):
        """Describe everything on the USB-A port, for the page's USB scan button."""
        devices = self._bus_devices()
        log("USB scan: %d device(s) on the USB-A port" % len(devices), ("warn", "ok")[bool(devices)])
        if not devices:
            log("  Nothing enumerated. The device did not answer the board at all: try another USB-A "
                "port on a powered hub, re-plug it, or check that the receiver is a USB 2.0 device.", "warn")
        attached = {device_key(e.dev) for e in self.endpoints}   # the bus gives new objects each scan
        for dev in devices:
            vid_pid = "%04x:%04x" % (dev.idVendor, dev.idProduct)
            name = " ".join(x for x in (_safe_attr(dev, "manufacturer", ""), _safe_attr(dev, "product", "")) if x)
            log("- %s %s  [%s]" % (vid_pid, name or "(no name)",
                                   ("not attached", "ATTACHED")[device_key(dev) in attached]), "info")
            try:
                desc = get_config_descriptor(dev)
            except Exception as err:
                log("    configuration descriptor unreadable: %r" % (err,), "error")
                continue
            log("    config descriptor %d bytes: %s" % (len(desc), bytes(desc).hex()), "info")
            for iface in find_hid_interfaces(desc):
                line = "    if%d HID subclass %d protocol %d ep 0x%02x mps %d, report descriptor %d bytes" % (
                    iface["number"], iface["subclass"], iface["protocol"], iface.get("endpoint", 0),
                    iface.get("max_packet", 0), iface["report_len"])
                log(line, "info")
                try:
                    rd = read_report_descriptor(dev, iface)
                    reports, uses_ids = hidparse.parse(rd)
                    log("      understood: %s" % (" ".join(r.describe() for r in reports.values()) or "nothing usable"),
                        ("warn", "info")[bool(reports)])
                    for k in range(0, len(rd), 48):
                        log("      desc[%d]: %s" % (k, rd[k:k + 48].hex()), "info")
                except Exception as err:
                    log("      report descriptor unreadable: %r" % (err,), "error")

    # ------------------------------------------------------------ reading
    def poll(self, sink):
        """Read one report from each endpoint (short timeout) and dispatch it.

        sink: object with mouse(sid, buttons, dx, dy, wheel, pan),
              keys(sid, codes_or_None, key_table), consumer(sid, usages)
        """
        self._sink = sink
        now = now_ms()
        every = self._collect is not None      # a vendor query waits for a reply: read everything
        quiet_read = False
        for ep in self.endpoints:
            # the endpoint of the last report (the mouse, while it moves) waits up to 2 ms; the others
            # only until the next millisecond tick, so they hold up the mouse less
            timeout = READ_TIMEOUT_MS if (every or ep is self._hot) else QUIET_TIMEOUT_MS
            if not (every or ep is self._hot or now - ep.data_at < ACTIVE_MS):
                if quiet_read or now < ep.check_at:
                    continue
                quiet_read = True              # one quiet endpoint per pass
                ep.check_at = now + QUIET_CHECK_MS
            t0 = now_ms() if self.timing else 0
            try:
                count = self._read(ep, timeout)
            except Exception as err:          # USBError / OSError: device is gone
                self.detach_all("read error %r" % (err,))
                self._next_scan = now_ms() + RESCAN_AFTER_ERROR_MS
                return
            ep.reads += 1
            if self.timing:
                ep.ms += now_ms() - t0
            if not count:
                continue
            # the report as a view of the buffer: one view per report length, made once and kept
            # (a new slice for every report would use memory up to 1000 times a second)
            data = ep.views.get(count)
            if data is None:
                data = ep.views[count] = ep.view[:count]
            now = now_ms()
            self.last_input = now
            ep.data_at = now
            ep.got += 1
            self._hot = ep
            self.report_count += 1
            if self._collect is not None and ep.uses_ids and data[0] not in ep.reports:
                self._collect.append((ep.dev, ep.interface, bytes(data)))   # e.g. a HID++ reply
                continue
            self.dispatch(ep, data, sink)

    def _read(self, ep, timeout=READ_TIMEOUT_MS):
        """One read; 0 when the endpoint had nothing to say."""
        if self._no_raise:
            try:
                return ep.dev.read(ep.address, ep.buf, timeout=timeout, raise_on_timeout=False)
            except TypeError:                  # CircuitPython without raise_on_timeout
                self._no_raise = False
        try:
            return ep.dev.read(ep.address, ep.buf, timeout=timeout)
        except _TimeoutError:                  # (an exception object per empty read: memory)
            return 0

    def pump(self):
        """Poll the input endpoints once (used while a vendor query waits)."""
        if self._sink is not None and self.endpoints:
            self.poll(self._sink)
        if self.on_pump is not None:
            self.on_pump()

    def await_vendor_report(self, dev, iface, match, timeout_ms):
        """Wait for a report on `iface` (a find_hid_interfaces dict) that match(data)
        accepts; returns what match returned, or None on timeout. Input reports
        keep being dispatched meanwhile, so the mouse keeps working."""
        polled = any(ep.dev is dev and ep.interface == iface["number"] for ep in self.endpoints)
        buf = bytearray(packet_size(iface))
        deadline = now_ms() + timeout_ms
        self._collect = []
        try:
            while now_ms() < deadline:
                if not polled:                # interface the remapper doesn't read itself
                    try:
                        count = dev.read(iface["endpoint"], buf, timeout=5)
                        if count:
                            self._collect.append((dev, iface["number"], bytes(buf[:count])))
                    except _TimeoutError:
                        pass
                    except Exception:
                        return None
                self.pump()
                for got_dev, number, data in self._collect:
                    found = match(data) if got_dev is dev and number == iface["number"] else None
                    if found:
                        return found
                del self._collect[:]
                if not any(tg["dev"] is dev for tg in self.targets):   # device went away meanwhile
                    return None
        finally:
            self._collect = None
        return None

    @staticmethod
    def dispatch(ep, data, sink):
        rid = data[0] if ep.uses_ids else 0
        rep = ep.reports.get(rid)
        if rep is None:
            return
        base = 1 if ep.uses_ids else 0         # the report's data starts after its ID (not sliced off: no copy)
        sid = ep.sid * 256 + rid
        if rep.has_mouse():
            v = rep.decode_mouse(data, base)    # (one list per report type, reused)
            sink.mouse(sid, v[0], v[1], v[2], v[3], v[4])
        if rep.has_keys():
            sink.keys(sid, rep.decode_keys(data, base), ep.key_table)
        if rep.has_consumer():
            sink.consumer(sid, rep.decode_consumer(data, base))
