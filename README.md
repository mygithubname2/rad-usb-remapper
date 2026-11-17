# Rad USB Remapper

Remap any USB mouse or keyboard in hardware. The Rad USB Remapper runs on an
Adafruit Feather RP2040 with USB Type A Host: plug your mouse or
keyboard into the board, plug the board into your computer, and set everything up from a web page. Your
mappings are stored on the board, so they work on any computer with no software installed.

> **Built with AI:** this project was created entirely with Claude Opus 5.5, Anthropic's AI, from start to
> finish. Claude wrote the firmware, the configuration page and the documentation; the project's owner described
> what was wanted, tested it on real hardware and reported back what needed fixing.

## Features

- Remap any mouse button, wheel direction, wheel tilt, keyboard key or media key, and buttons wired to the board.
- Turn an input into a key or shortcut, a mouse button, a media key, scrolling, pointer movement, typed text,
  a cycle or macro of steps, a web page or app launcher, a foot pedal press or a MIDI message.
- Change pointer movement: speed, inverted or swapped axes, scrolling instead of moving, or off; or change it
  only while a button is held or latched.
- Up to 8 profiles per device, each with up to 8 layers, switched by button or held temporarily.
- Separate profiles for each mouse or keyboard you plug in.
- Emulates a three-pedal USB transcription foot pedal.
- Extras: battery level for Razer and Logitech wireless devices, haptic pulses on the Logitech MX Master 4,
  RGB lighting that follows the profile on Logitech and Razer mice, and an optional OLED status display.

## What you need

- An Adafruit Feather RP2040 with USB Type A Host.
- [CircuitPython 11](https://circuitpython.org/board/adafruit_feather_rp2040_usb_host/) on the board.
- Desktop Chrome, Edge or Opera for the configuration page (it uses Web Serial).
- Optional: an Adafruit 0.91" SSD1306 128x32 OLED on the STEMMA QT connector.

## Installing

1. Install CircuitPython 11 on the board: hold BOOT, tap RESET, and drag the `.uf2` file onto the `RPI-RP2` drive.
2. Copy everything in the [`CIRCUITPY`](CIRCUITPY) folder to the root of the `CIRCUITPY` drive.
3. Press RESET.
4. Open [`web/remapper.html`](web/remapper.html) in Chrome, Edge or Opera and click **Connect**.

The full user guide is [`web/help.html`](web/help.html), also linked from the page.

## Repository layout

| Folder | Contents |
|---|---|
| `CIRCUITPY/` | What goes on the board: `boot.py`, `code.py` and the compiled firmware (`.mpy`). |
| `source/` | The firmware source (`.py`). |
| `web/` | The configuration page and its help page. |
| `tools/` | A script to compile the firmware. |

## Building the firmware

The `.mpy` files in `CIRCUITPY/` are compiled from `source/` with CircuitPython's `mpy-cross`, built for the
same CircuitPython version as the board runs. After changing a source file:

```sh
tools/build_mpy.sh /path/to/mpy-cross
```

`boot.py` and `code.py` are copied as they are.

## License

[MIT](LICENSE).

Device protocols for battery, haptics and lighting follow community documentation from the
[Solaar](https://github.com/pwr-Solaar/Solaar), OpenLogi and
[OpenRazer](https://github.com/openrazer/openrazer) projects. Logitech, Razer, Keychron and other names are
trademarks of their owners; this project isn't affiliated with them.
