# statusled.py - the Feather's on-board NeoPixel: red = no input device, a green flash
# when one is attached, then the active profile's color.
# (Split from code.py: smaller files need less memory to compile at start-up.)

import board
import digitalio

import settings_store
from hostinput import now_ms

LED_RED = "#ff0000"
LED_GREEN = "#00ff00"
LED_FLASH_MS = 1500           # green "device connected" flash before the profile color
LED_CACHE = 8                 # converted colors remembered (profiles + red/green fit)
LED_PREVIEW_MS = 3000         # a previewed color stays this long after the last change
LED_GAMMA = 2.2               # screen colors are gamma-encoded; the LED is roughly linear


class StatusLed:
    """Drives the Feather's NeoPixel with the built-in neopixel_write module
    (no extra library). Silently does nothing on boards without one."""

    def __init__(self):
        self.pin = None
        self.color = None
        self.state = "off"
        self._power = None
        self._attached = False
        self._flash_until = 0
        self._cache = {}              # color string -> LED value, for the current settings
        self.brightness = settings_store.LED_BRIGHTNESS
        self.balance = settings_store.LED_BALANCE
        self.gamma = True
        self._shown = LED_RED        # what is shown now, as a color string
        self._preview = None
        self._preview_until = 0       # while a page's color picker is in use
        self._ticked = None           # profile color tick() last showed (unchanged: nothing to do)
        try:
            import neopixel_write
            self._write = neopixel_write.neopixel_write
            power = getattr(board, "NEOPIXEL_POWER", None)
            if power is not None:              # this Feather powers the LED from GPIO20
                self._power = digitalio.DigitalInOut(power)
                self._power.switch_to_output(value=True)
            self.pin = digitalio.DigitalInOut(board.NEOPIXEL)
            self.pin.switch_to_output(value=False)
        except Exception as err:               # no NeoPixel, or pin in use
            self.pin = None
            print("Status LED unavailable: %r" % (err,))

    def show(self, rgb):
        if self.pin is None or rgb == self.color:
            return
        self.color = rgb
        try:
            self._write(self.pin, bytearray((rgb[1], rgb[0], rgb[2])))   # NeoPixels take G, R, B
        except Exception:
            pass

    def configure(self, brightness, balance, gamma):
        """New brightness (full-channel value 1-255), balance (r, g, b in %) and gamma flag."""
        self.brightness = brightness
        self.balance = tuple(balance)
        self.gamma = bool(gamma)
        self._cache = {}
        self._ticked = None
        self.color = None                         # force a rewrite with the new settings
        self.show_color(self._shown)

    def led_value(self, hex_color):
        """Screen color '#rrggbb' -> LED (r, g, b): gamma decode, white balance, brightness."""
        cached = self._cache.get(hex_color)
        if cached is not None:
            return cached
        try:
            value = int(hex_color[1:7], 16)
        except (TypeError, ValueError):
            value = 0x00FF00
        out = []
        for shift, gain in zip((16, 8, 0), self.balance):
            level = ((value >> shift) & 0xFF) / 255
            linear = level ** LED_GAMMA if self.gamma else level
            out.append(int(linear * gain / 100 * self.brightness + 0.5))
        rgb = tuple(out)
        if len(self._cache) >= LED_CACHE:          # keep the cache small: the color picker
            self._cache = {}                       # can send dozens of new colors a second
        self._cache[hex_color] = rgb
        return rgb

    def show_color(self, hex_color):
        self._shown = hex_color
        self.show(self.led_value(hex_color))

    def update(self, attached):
        """Red with nothing attached; a newly attached device flashes green first."""
        attached = bool(attached)
        self._ticked = None                   # tick() shows the profile color again afterwards
        if attached and not self._attached:
            self._flash_until = now_ms() + LED_FLASH_MS
        self._attached = attached
        if self._preview_until:               # a color preview has priority
            return
        self.state = ("red", "green (device connected)")[attached]
        self.show_color((LED_RED, LED_GREEN)[attached])

    def preview(self, hex_color):
        """Show a color picked on the page for a few seconds (renewed while picking)."""
        self._preview = hex_color
        self._ticked = None
        self._preview_until = now_ms() + LED_PREVIEW_MS
        self.state = "previewing %s" % (hex_color,)
        self.show_color(hex_color)

    def tick(self, hex_color):
        """After the green flash, show the active profile's color."""
        if self._preview_until:
            if now_ms() < self._preview_until:
                return
            self._preview_until = 0           # preview over: back to the normal color
            if not self._attached or now_ms() < self._flash_until:
                self.update(self._attached)   # red, or the rest of the green "connected" flash
                return
        if not self._attached or now_ms() < self._flash_until:
            return
        if hex_color == self._ticked:         # called every loop: nothing new, no new text (memory)
            return
        self._ticked = hex_color
        self.state = "profile color %s" % (hex_color,)
        self.show_color(hex_color)
