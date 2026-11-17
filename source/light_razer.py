# light_razer.py - the Razer part of mouselight.py: loaded only when a Razer device is attached
# (Logitech-only setups don't pay for it in memory).
#
# Razer mice take 90-byte reports (community documentation: OpenRazer), "extended matrix"
# lighting commands:
#     0x0F:0x84 get brightness [store, LED, 0]   - used to find which LEDs the mouse has
#     0x0F:0x02 set effect     [store, LED, 1 = static, 0, 0, 1 color, R, G, B]
# LEDs tried: scroll wheel 0x01, logo 0x04, backlight 0x05, right / left side 0x10 / 0x11.
# store 0 = don't keep it (RAM); a mouse that refuses that gets store 1 (kept by the mouse).
# The connection (USB interface, transaction id) is the one the battery reader found.

RAZER_LEDS = ((0x01, "scroll wheel"), (0x04, "logo"), (0x05, "backlight"), (0x10, "right side"), (0x11, "left side"))
RAZER_OK = 0x02
RAZER_REFUSED = (0x03, 0x05)     # failure / not supported: a real "no" (None or 0x04 = no answer in time: asleep)


def send(light, reader, iface, tid, cls, cmd, args):
    """Send one Razer command; the reply's status (2 = ok) or None (no answer)."""
    report = bytearray(90)
    report[1], report[5], report[6], report[7] = tid, len(args), cls, cmd
    report[8:8 + len(args)] = bytes(args)
    crc = 0
    for b in report[2:88]:
        crc ^= b
    report[88] = crc
    mon = reader.mon
    if mon.ctrl(reader.t, 0x21, 0x09, 0x0300, iface, report) is None:
        return None
    for _attempt in range(6):
        mon.wait(15)
        reply = bytearray(90)
        if mon.ctrl(reader.t, 0xA1, 0x01, 0x0300, iface, reply) is None:
            return None
        if reply[0] != 0x01:                 # 1 = busy: ask again
            return reply[0]
    return None


def link(light, reader):
    """(interface, transaction id) the mouse answers on: the battery reader's, or found here."""
    if reader.found is None:
        try:
            reader.read()                    # the battery check finds it
        except Exception:
            pass
    if reader.found is not None:
        return reader.found
    from battery_razer import RAZER_TRANSACTIONS
    for iface in reader.ifaces:
        for tid in RAZER_TRANSACTIONS:
            if send(light, reader, iface, tid, 0x00, 0x81, (0, 0)) == RAZER_OK:     # firmware version
                return (iface, tid)
    return None


def leds(light, reader, found_link):
    """The LEDs that answer (LED id, name). A wireless mouse sometimes doesn't answer in time:
    an LED without an answer is asked once more."""
    out, seen = [], []
    for led, name in RAZER_LEDS:
        status = send(light, reader, found_link[0], found_link[1], 0x0F, 0x84, (1, led, 0))
        if status is None or status == 0x04:
            status = send(light, reader, found_link[0], found_link[1], 0x0F, 0x84, (1, led, 0))
        seen.append("%02X:%s" % (led, status))
        if status == RAZER_OK:
            out.append((led, name))
    light.log("Mouse lighting: Razer if%d tid %02X, LEDs answered %s" % (found_link[0], found_link[1], " ".join(seen)), "info")
    return out

def apply(light, rgb):
    _kind, reader, iface, tid, store, leds = light._found
    for attempt in (store, 1):
        ok, refused = 0, None
        for led, _name in leds:
            status = send(light, reader, iface, tid, 0x0F, 0x02, (attempt, led, 1, 0, 0, 1) + rgb)
            if status == RAZER_OK:
                ok += 1
            else:
                refused = status
        if ok or attempt == 1 or refused not in RAZER_REFUSED:
            break                            # (a mouse that didn't answer is asleep, not refusing)
        light.log("Mouse lighting: the mouse refused a color it doesn't keep (status %r); "
                 "sending colors it keeps instead" % (refused,), "warn")
    if not ok:
        light._lost("didn't take the color (status %r)" % (refused,))
        return
    if attempt != store:
        light._found = ("razer", reader, iface, tid, attempt, leds)
    if refused is not None:
        light.log("Mouse lighting: an LED answered status %r" % (refused,), "warn")
    light._done(rgb, ok)
