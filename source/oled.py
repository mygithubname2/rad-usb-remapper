# oled.py - status display on the STEMMA QT connector: an SSD1306 128x32 OLED at 0x3C
# (Adafruit's 0.91" monochrome OLED).
#
# Shows the device on the USB-A port, its battery, the active profile and layer.
# Needs adafruit_displayio_ssd1306.mpy from the CircuitPython library bundle in CIRCUITPY/lib.
# Text is drawn with CircuitPython's built-in font (terminalio), so no text library is needed.
# Without the display (or the library) the remapper runs as usual and the page shows why
# the display isn't used.

import board
import displayio
import terminalio

from clock import now_ms          # (the small clock module: not hostinput, which would cost the display memory)

ADDRESS = 0x3C
WIDTH, HEIGHT = 128, 32
NAME = "SSD1306 128x32 at 0x3C"
# (x, y, characters) per text field:  top left: device name     | top right: battery icon
#                                     bottom left: profile/layer | bottom right: battery %
ROWS = ((0, 2, 17), (0, 18, 16), (104, 18, 4))
ICON_X, ICON_Y = 106, 4
UPDATE_MS = 2000             # look for changes this often (the page asks for an update at once when the
                             # profile, layer or a latch changes: see poke(); building the text uses memory)
SEARCH_MS = 10000            # look for a newly plugged-in display this often


def _bus(i2c):
    try:                                     # CircuitPython 9 and later
        from i2cdisplaybus import I2CDisplayBus
        return I2CDisplayBus(i2c, device_address=ADDRESS)
    except ImportError:                      # older CircuitPython
        return displayio.I2CDisplay(i2c, device_address=ADDRESS)


class _Line:
    """One line of text drawn with the built-in font: a row of font tiles. Much lighter on
    memory than a text library, and nothing to install."""

    def __init__(self, font, palette, chars, x, y):
        w, h = font.get_bounding_box()[:2]
        self.font = font
        self.chars = chars
        self.x = x
        self.grid = displayio.TileGrid(font.bitmap, pixel_shader=palette, width=chars, height=1,
                                       tile_width=w, tile_height=h)
        self.group = displayio.Group(x=x, y=y)
        self.group.append(self.grid)
        self.text = None
        self.set(" ")

    def set(self, text):
        if text == self.text:
            return
        self.text = text
        for i in range(self.chars):
            glyph = self.font.get_glyph(ord(text[i]) if i < len(text) else 32)
            glyph = glyph or self.font.get_glyph(63)         # characters the font lacks -> "?"
            self.grid[i] = glyph.tile_index if glyph else 0


def _battery_icon(bitmap, level, charging):
    """22 x 10 battery outline, filled to `level` %; a gap in the middle while charging."""
    w, h = bitmap.width, bitmap.height
    for x in range(w):
        for y in range(h):
            bitmap[x, y] = 0
    for x in range(w - 2):                   # outline
        bitmap[x, 0] = bitmap[x, h - 1] = 1
    for y in range(h):
        bitmap[0, y] = bitmap[w - 3, y] = 1
    for y in range(3, h - 3):                # the nub
        bitmap[w - 2, y] = bitmap[w - 1, y] = 1
    if level is None:
        return
    fill = (w - 6) * max(0, min(100, level)) // 100
    for x in range(2, 2 + fill):
        for y in range(2, h - 2):
            bitmap[x, y] = 1
    if charging:                             # a lightning-like notch
        for y in range(2, h - 2):
            bitmap[(w - 3) // 2 + (y % 2), y] = 0


def _lines(s):
    layer = "" if s["layer"].startswith("Base") else "/" + s["layer"]   # skip "Base"
    pct = s["battery_text"].split(" ")[0]
    pct = pct if pct.endswith("%") else ""          # no level: the icon stays empty, no number
    return [s["name"], s["profile"] + layer, " " * (4 - len(pct)) + pct]   # percent right-aligned


class Oled:
    def __init__(self, log):
        self.log = log
        self.display = None
        self.status = "not used yet"
        self.rotation = 0
        self._labels = []
        self._icon = None
        self._last = None
        self._next = 0
        self._search_at = 0

    # ---- set up
    def configure(self, rotation):
        """New rotation. A running display is turned in place: rebuilding it needs blocks of
        free memory in one piece, which a long-running board may not have."""
        self.rotation = rotation
        if self.display is not None:
            try:
                self.display.rotation = rotation
                self._last = None                # redraw at the next update
                self._next = 0
                return
            except Exception:                    # (unexpected) - rebuild it instead
                pass
        self._search_at = 0
        self.stop()

    def stop(self):
        if self.display is not None:
            try:
                displayio.release_displays()
            except Exception:
                pass
        self.display = None
        self._last = None
        self.status = "not used yet"

    def _start(self):
        """Look for the display; returns True when it is running."""
        self._search_at = now_ms() + SEARCH_MS
        try:
            displayio.release_displays()
            i2c = getattr(board, "STEMMA_I2C", None)
            i2c = i2c() if i2c else board.I2C()
        except Exception as err:                 # nothing on the connector: no pull-ups
            self.status = "nothing connected to STEMMA QT"
            return False
        try:
            found = False
            if i2c.try_lock():
                try:
                    found = ADDRESS in i2c.scan()
                finally:
                    i2c.unlock()
            if not found:
                self.status = "no display found at 0x3C"
                i2c.deinit()
                return False
            try:
                from adafruit_displayio_ssd1306 import SSD1306
            except ImportError as err:
                self.status = "library missing: copy lib/adafruit_displayio_ssd1306.mpy from the CircuitPython library bundle (%s)" % (err,)
                i2c.deinit()
                return False
            self.display = SSD1306(_bus(i2c), width=WIDTH, height=HEIGHT, rotation=self.rotation)
            self._build()
            self.status = NAME
            self.log("Display: " + self.status, "ok")
            return True
        except MemoryError as err:
            self.status = "not enough memory for the display (%r)" % (err,)
        except Exception as err:
            self.status = "display error: %r" % (err,)
        failed = self.status
        self.stop()
        self.status = failed
        return False

    def _build(self):
        group = displayio.Group()
        palette = displayio.Palette(2)
        palette[0] = 0x000000
        palette[1] = 0xFFFFFF
        self._icon = displayio.Bitmap(22, 10, 2)
        self._labels = []
        for x, y, chars in ROWS:
            line = _Line(terminalio.FONT, palette, chars, x, y)
            group.append(line.group)
            self._labels.append(line)
        group.append(displayio.TileGrid(self._icon, pixel_shader=palette, x=ICON_X, y=ICON_Y))
        self.display.root_group = group
        try:
            self.display.auto_refresh = False    # redraw only when something changes
        except Exception:
            pass

    def begin(self):
        """Start the display now (at boot, before USB devices take memory in small pieces)."""
        if self.display is None:
            self._start()

    # ---- main loop
    def poke(self):
        """Something shown changed (profile, layer, latch): update at the next service()."""
        self._next = 0

    def service(self, info):
        """info() -> dict(name, level, charging, battery_text, profile, layer, note);
        called only when an update is due."""
        t = now_ms()
        if self.display is None:
            if t >= self._search_at:
                self._start()
            return
        if t < self._next:
            return
        self._next = t + UPDATE_MS
        state = info()
        if state == self._last:                  # (compares the dicts: no copy made)
            return
        self._last = state
        try:
            self._draw(state)
        except Exception as err:                 # display unplugged, I2C error...
            self.stop()
            low = isinstance(err, MemoryError)       # memory short: try again in a minute, not at once
            self.status = ("display stopped: not enough free memory - trying again in a minute" if low
                           else "display stopped: %r - looking for it again" % (err,))
            if low:
                self._search_at = now_ms() + 60000
            self.log("Display: " + self.status, "warn")

    def _draw(self, s):
        _battery_icon(self._icon, s["level"], s["charging"])
        lines = _lines(s)
        for line, text in zip(self._labels, lines):
            line.set((text or " ")[:line.chars])
        self.display.refresh()

    def describe(self):
        return self.status
