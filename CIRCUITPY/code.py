# code.py - starts the USB remapper.
#
# The firmware itself is in remapper_main.mpy and the other .mpy files: precompiled for
# CircuitPython 11.0.0-alpha.2, so the board doesn't compile ~200 KB of Python in RAM at
# start-up (that leaves memory in pieces too small for the status display and others).
# The .py sources are in the "source" folder of the download, for reading or editing.
#
# If the firmware stops with an error, the error is also sent on the web page's data port
# (every few seconds, so a page that connects later sees it too): normally the board has no
# console port, where CircuitPython would print it. See boot.py.
try:
    import remapper_main  # noqa: F401  (runs the remapper)
except Exception as err:      # (Ctrl-C / KeyboardInterrupt is not caught)
    import time
    import usb_cdc
    try:
        import traceback
        text = "".join(traceback.format_exception(err))
    except Exception:
        text = repr(err)
    print(text)                                       # console / boot_out-style output, if any
    try:
        import json
        line = (json.dumps({"type": "debug", "level": "error",
                            "msg": "Firmware stopped with an error (fix it, then press RESET): " + text}) + "\n").encode()
    except Exception:
        line = b'{"type":"debug","level":"error","msg":"Firmware stopped with an error. Press RESET."}\n'
    port = usb_cdc.data
    while True:
        if port is not None:
            try:
                port.write(line)
            except Exception:
                pass
        time.sleep(3)
