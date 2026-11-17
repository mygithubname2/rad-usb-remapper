# boardbuttons.py - buttons soldered to the board's free pins, plus its BOOT button.
#
# Nothing to set up: every free header pin is watched with the chip's pull-up, so a pin
# with nothing on it never reads "pressed". A button wired from a pin to GND is a new
# input; it appears on the web page the first time it is pressed, and is remapped like
# a mouse button (profile table "board", key = the pin's GPIO number). An unmapped
# board button sends nothing.
#
# Pins left alone: SDA/SCL (STEMMA QT display), D5 (recovery jumper), D13 (red LED),
# GPIO16-18 (USB-A port), the NeoPixel. A pin that already reads low at start-up is
# skipped too (something else is wired to it), so it can't look like a stuck button.
#
# keypad scans the pins in the background (C code, debounced): this costs a few hundred
# bytes. Events are read into one reused object, so polling allocates nothing.

import board
import digitalio
import keypad

# (board pin name, GPIO number) - the number is the input's code in profiles
PINS = (("BUTTON", 7), ("A0", 26), ("A1", 27), ("A2", 28), ("A3", 29), ("D24", 24), ("D25", 25),
        ("SCK", 14), ("MOSI", 15), ("MISO", 8), ("RX", 1), ("TX", 0),
        ("D4", 4), ("D6", 6), ("D9", 9), ("D10", 10), ("D11", 11), ("D12", 12))
MAX_EVENTS = 8


def _idle_high(pin):
    """True if the pin can be claimed and reads high with the pull-up (nothing holding it low)."""
    try:
        with digitalio.DigitalInOut(pin) as io:
            io.switch_to_input(pull=digitalio.Pull.UP)
            return io.value
    except Exception:              # in use by something else, or not on this board
        return False


class BoardButtons:
    def __init__(self, log=None):
        self.log = log or (lambda msg, level="info": None)
        self.keys = None
        self.codes = ()            # GPIO number of each watched pin, in keypad's key order
        self.skipped = ()          # pin names found low at start-up
        self._event = keypad.Event()

    def start(self):
        """Watch the free pins (again). Safe to call when already running."""
        self.stop()
        pins, codes, skipped, seen = [], [], [], []
        for name, gpio in PINS:
            pin = getattr(board, name, None)
            if pin is None or pin in seen:
                continue
            seen.append(pin)
            if _idle_high(pin):
                pins.append(pin)
                codes.append(gpio)
            elif name != "BUTTON":
                skipped.append(name)
        self.codes, self.skipped = tuple(codes), tuple(skipped)
        if pins:
            try:
                self.keys = keypad.Keys(pins, value_when_pressed=False, pull=True, max_events=MAX_EVENTS)
            except Exception as err:
                self.keys, self.codes = None, ()
                self.log("Board buttons: can't watch the pins (%r)" % (err,), "warn")
        if skipped:
            self.log("Board buttons: pins %s read low at start-up and are left alone" % ", ".join(skipped), "info")

    def stop(self):
        if self.keys is not None:
            self.keys.deinit()
            self.keys = None
        self.codes = ()

    def poll(self, remapper):
        """Pass presses and releases to the remapper (call every loop)."""
        keys = self.keys
        if keys is None:
            return
        event = self._event
        while keys.events.get_into(event):
            remapper.board(self.codes[event.key_number], event.pressed)
        if keys.events.overflowed:
            keys.events.overflowed = False
            remapper.board_release_all()
