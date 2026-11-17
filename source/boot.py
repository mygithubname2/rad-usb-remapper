# boot.py -- runs once at power-up / hard reset, before USB is set up.
#
#  * enables the USB serial port ("data") that the web page talks to. The CircuitPython
#    console (REPL) port is only added in Update mode, with the D5 jumper, or if the saved
#    settings can't be read, so normally the browser lists just one port (v3.23)
#  * exposes the USB devices chosen on the web page (see DEVICES below)
#  * hides the CIRCUITPY drive if mass storage was disabled from the web page
#  * lets the board write the drive (per-device profiles) if chosen on the web page
#
# DEVICES (settings "usb", chosen on the web page; any combination)
#   kbd     keyboard
#   mouse   mouse: 16-bit custom report with sideways scroll, or CircuitPython's
#           standard 8-bit mouse (option "mouse_type": "hires" | "standard")
#   media   media keys (Consumer Control)
#   pedal   three-pedal USB transcription foot pedal. The board then identifies itself
#           as 05F3:00FF (the pedal's USB ID) so transcription software finds it. With other HID devices
#           it uses report ID 4; on its own it is byte-for-byte the real pedal (2-byte
#           report, no report ID). Media keys share its HID usage page (Consumer,
#           0x0C): some programs (e.g. Pedable) then pick the wrong one.
#   midi    USB MIDI (notes, controllers, program changes from MIDI actions)
#   mfr / product   names the computer shows ("" = default: product "Rad USB Remapper",
#           CircuitPython's own manufacturer); plain ASCII, up to 126 characters
#   vid / pid       USB vendor / product ID (0 = the board's own, 239A:812A).
#           With the foot pedal the ID is always the pedal's (05F3:00FF), so transcription
#           software finds it.
#
# MOUSE TYPE details
#   "hires"     custom mouse (report ID 2, 7 bytes): 8 buttons | X int16 |
#               Y int16 | wheel int8 | horizontal pan int8 -- keeps high-DPI
#               motion and wheel tilt (default)
#   "standard"  CircuitPython's standard mouse (4 bytes: 5 buttons, X/Y/wheel
#               int8, no horizontal scroll) -- the most widely compatible
#
# THE CIRCUITPY DRIVE belongs to the board (it saves each device's profiles in
# /profiles): the computer sees it read-only, or not at all when it is hidden. To copy
# new firmware files, use Update mode on the web page (Settings > Board): the next
# start-up only, the drive is shown and editable by the computer. Any later start-up
# (RESET, unplug) goes back to normal.
#
# Recovery if the drive is hidden or the board presents only a pedal / MIDI and
# the web page is unavailable:
#  * connect pin D5 to GND while pressing RESET -> this boot uses the normal
#    devices (keyboard, mouse, media keys) and the drive is editable (like Update mode), or
#  * enter CircuitPython safe mode (press RESET, then press it again while the
#    status LED blinks yellow) -- boot.py is skipped and the drive appears.
#
# Changes to this file only take effect after a hard reset (RESET button or
# unplug/replug), not after a soft reload.

import usb_hid

# 1) Saved settings and the D5 recovery jumper (each step guarded: nothing here may stop
#    the serial ports below from being set up)
DEFAULT_USB = {"kbd": True, "mouse": True, "media": True, "pedal": False, "midi": False, "mfr": "", "product": "",
               "vid": 0, "pid": 0}
cfg = {"msc": True}
force = False
try:
    import board
    import digitalio
    override_pin = getattr(board, "D5", None)
    if override_pin is not None:
        with digitalio.DigitalInOut(override_pin) as pin:
            pin.switch_to_input(pull=digitalio.Pull.UP)
            force = not pin.value
except Exception as err:
    print("REMAP_ERROR D5:", repr(err))

update = False                 # Update mode: this start-up only, the computer may write the drive
settings_ok = False
try:
    import settings_store
    cfg = settings_store.load()
    update = settings_store.take_update_boot()
    settings_ok = True
except Exception as err:
    print("REMAP_ERROR settings (is settings_store.py on CIRCUITPY?):", repr(err))

# 2) Serial ports. The web page uses the data port. The console (REPL) is for working on the
#    firmware, so it's only there when the drive is being updated or something needs fixing:
#    Update mode, the D5 jumper, or settings that couldn't be read. (CircuitPython safe mode
#    skips this file and always has the console.)
import usb_cdc

console_on = force or update or not settings_ok
try:
    usb_cdc.enable(console=console_on, data=True)
except Exception as err:
    print("REMAP_ERROR usb_cdc:", repr(err))
    try:
        usb_cdc.enable(console=True, data=True)
    except Exception:
        pass

msc_on = cfg.get("msc", True) or force or update
usb = dict(DEFAULT_USB) if force else dict(DEFAULT_USB, **(cfg.get("usb") or {}))
mouse_type = "hires" if force else cfg.get("mouse_type", "hires")
DEFAULT_PRODUCT = "Rad USB Remapper"
product_name = usb["product"] or DEFAULT_PRODUCT
maker_name = usb["mfr"]

# MIDI: CircuitPython enables it by default; only keep it when chosen
try:
    import usb_midi
    if usb["midi"]:
        usb_midi.enable()
        name = product_name
        try:
            usb_midi.set_names(streaming_interface_name=name, audio_control_interface_name=name,
                               in_jack_name=name, out_jack_name=name)
        except Exception as err:          # older CircuitPython: default names
            print("REMAP_ERROR midi names:", repr(err))
    if not usb["midi"]:
        usb_midi.disable()
except Exception as err:
    usb["midi"] = False
    print("REMAP_ERROR usb_midi:", repr(err))

# 3) HID devices
MOUSE_DESCRIPTOR = bytes((
    0x05, 0x01,              # Usage Page (Generic Desktop)
    0x09, 0x02,              # Usage (Mouse)
    0xA1, 0x01,              # Collection (Application)
    0x85, 0x02,              #   Report ID (2)
    0x09, 0x01,              #   Usage (Pointer)
    0xA1, 0x00,              #   Collection (Physical)
    0x05, 0x09,              #     Usage Page (Button)
    0x19, 0x01, 0x29, 0x08,  #     Usage Minimum (1), Usage Maximum (8)
    0x15, 0x00, 0x25, 0x01,  #     Logical Minimum (0), Logical Maximum (1)
    0x95, 0x08, 0x75, 0x01,  #     Report Count (8), Report Size (1)
    0x81, 0x02,              #     Input (Data, Var, Abs)
    0x05, 0x01,              #     Usage Page (Generic Desktop)
    0x16, 0x01, 0x80,        #     Logical Minimum (-32767)
    0x26, 0xFF, 0x7F,        #     Logical Maximum (32767)
    0x75, 0x10, 0x95, 0x02,  #     Report Size (16), Report Count (2)
    0x09, 0x30, 0x09, 0x31,  #     Usage (X), Usage (Y)
    0x81, 0x06,              #     Input (Data, Var, Rel)
    0x15, 0x81, 0x25, 0x7F,  #     Logical Minimum (-127), Logical Maximum (127)
    0x75, 0x08, 0x95, 0x01,  #     Report Size (8), Report Count (1)
    0x09, 0x38,              #     Usage (Wheel)
    0x81, 0x06,              #     Input (Data, Var, Rel)
    0x05, 0x0C,              #     Usage Page (Consumer)
    0x0A, 0x38, 0x02,        #     Usage (AC Pan)
    0x95, 0x01,              #     Report Count (1)
    0x81, 0x06,              #     Input (Data, Var, Rel)
    0xC0,                    #   End Collection
    0xC0,                    # End Collection
))


def pedal_descriptor(report_id):
    """Foot pedal: 3 buttons in byte 0 (1 left, 2 center, 4 right), byte 1 = 0."""
    rid = (0x85, report_id) if report_id else ()
    return bytes((
        0x05, 0x0C,              # Usage Page (Consumer)
        0x09, 0x03,              # Usage (Programmable Buttons)
        0xA1, 0x01,              # Collection (Application)
    ) + rid + (
        0x05, 0x09,              #   Usage Page (Button)
        0x19, 0x01, 0x29, 0x03,  #   Usage Minimum (1), Usage Maximum (3)
        0x15, 0x00, 0x25, 0x01,  #   Logical Minimum (0), Logical Maximum (1)
        0x75, 0x01, 0x95, 0x03,  #   Report Size (1), Report Count (3)
        0x81, 0x02,              #   Input (Data, Var, Abs)
        0x95, 0x0D,              #   Report Count (13) -> pad to 2 bytes
        0x81, 0x01,              #   Input (Constant)
        0xC0,                    # End Collection
    ))


def pedal_device(report_id):
    return usb_hid.Device(
        report_descriptor=pedal_descriptor(report_id),
        usage_page=0x0C,
        usage=0x03,
        report_ids=(report_id,),
        in_report_lengths=(2,),
        out_report_lengths=(0,),
    )


def hires_mouse():
    return usb_hid.Device(
        report_descriptor=MOUSE_DESCRIPTOR,
        usage_page=0x01,
        usage=0x02,
        report_ids=(2,),
        in_report_lengths=(7,),
        out_report_lengths=(0,),
    )


def the_mouse():
    return {"standard": lambda: usb_hid.Device.MOUSE}.get(mouse_type, hires_mouse)()


def chosen_devices():
    pedal_only = usb["pedal"] and not (usb["kbd"] or usb["mouse"] or usb["media"])
    devices = []
    if usb["kbd"]:
        devices.append(usb_hid.Device.KEYBOARD)
    if usb["mouse"]:
        devices.append(the_mouse())
    if usb["media"]:
        devices.append(usb_hid.Device.CONSUMER_CONTROL)
    if usb["pedal"]:
        devices.append(pedal_device(0 if pedal_only else 4))   # alone: exactly like the real pedal
    return devices


try:
    hid_devices = chosen_devices()
    if hid_devices:
        usb_hid.enable(tuple(hid_devices))
    if not hid_devices:
        usb_hid.disable()
except Exception as err:
    print("REMAP_ERROR usb_hid (%r):" % (usb,), repr(err))
    usb.update(kbd=True, mouse=True, media=True, pedal=False)
    mouse_type = "standard"
    try:
        usb_hid.enable((usb_hid.Device.KEYBOARD, usb_hid.Device.MOUSE, usb_hid.Device.CONSUMER_CONTROL))
    except Exception as err2:
        print("REMAP_ERROR usb_hid fallback:", repr(err2))

# 4) Names and USB ID (the pedal's ID with the pedal, so transcription software finds it)
identity = {}
if maker_name:
    identity["manufacturer"] = maker_name
if product_name:
    identity["product"] = product_name
usb_vid, usb_pid = (0x05F3, 0x00FF) if usb["pedal"] else (usb.get("vid") or 0, usb.get("pid") or 0)
if usb_vid:
    identity["vid"] = usb_vid
if usb_pid:
    identity["pid"] = usb_pid
if identity:
    try:
        import supervisor
        supervisor.set_usb_identification(**identity)
    except Exception as err:
        print("REMAP_ERROR usb identity:", repr(err))

# Name of the HID interface (otherwise "CircuitPython HID"; some programs show it)
try:
    usb_hid.set_interface_name(product_name)
except Exception as err:
    print("REMAP_ERROR interface name:", repr(err))

# 5) Mass storage on/off
if not msc_on:
    try:
        import storage
        storage.disable_usb_drive()
    except Exception as err:
        msc_on = True
        print("REMAP_ERROR storage:", repr(err))

# 6) Who may write to the drive. Only one side can at a time: normally the board (to save
#    each device's profiles in /profiles; the computer sees the drive read-only). In Update
#    mode or with the D5 jumper, the computer (to copy firmware files) for this start-up.
board_writes = not (update or force)
if board_writes:
    try:
        import storage
        storage.remount("/", readonly=False)
    except Exception as err:
        board_writes = False
        print("REMAP_ERROR remount:", repr(err))

# code.py reads these lines back from boot_out.txt to report the real state.
print("REMAP_MSC=%d FORCED=%d" % (int(msc_on), int(force)))
print("REMAP_UPDATE=%d" % int(update or force))
print("REMAP_CONSOLE=%d" % int(console_on))
print("REMAP_BOARD_WRITES=%d" % int(board_writes))
print("REMAP_MOUSE_TYPE=%s" % mouse_type)
# devices | USB ID as saved | checksum of the names (the names don't fit: boot_out.txt holds 512 bytes)
_sum = 5381
for _ch in usb["mfr"] + "\n" + usb["product"]:
    _sum = (_sum * 33 + ord(_ch)) & 0xFFFF
print("REMAP_USB=%s|%04x:%04x|%04x" % ("".join("-x"[int(bool(usb[k]))] for k in ("kbd", "mouse", "media", "pedal", "midi")),
                                       usb.get("vid") or 0, usb.get("pid") or 0, _sum))
