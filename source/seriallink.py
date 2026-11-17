# seriallink.py - the JSON-lines link to the web page over the USB serial ports.
#
# Messages to the page are encoded and written in small pieces through one fixed buffer,
# so a large message (the settings) never needs one big block of free memory. Lines from
# the page longer than MAX_LINE are dropped unread.

import gc
import json

MAX_LINE = 6144            # largest JSON command accepted from the page


class _SerialPort:
    """One USB serial port the page may talk on."""

    def __init__(self, name, port):
        self.name = name
        self.port = port
        self.rx = bytearray()
        try:
            port.timeout = 0
            port.write_timeout = 0.05
        except Exception:
            pass


_TXBUF = bytearray(256)      # one fixed send buffer, reused for every message
_TXVIEW = memoryview(_TXBUF)


class _Writer:
    """Collects encoded JSON in _TXBUF and writes it to the port whenever it fills."""

    def __init__(self, port):
        self.port = port
        self.n = 0
        self.ok = True

    def put(self, text):
        data = text.encode("utf-8")
        size = len(data)
        n = self.n
        if n + size < len(_TXBUF):          # fits: no slices (nearly always)
            _TXBUF[n:n + size] = data
            self.n = n + size
            return
        i = 0
        while i < size and self.ok:
            k = min(size - i, len(_TXBUF) - self.n)
            _TXBUF[self.n:self.n + k] = data[i:i + k]
            self.n += k
            i += k
            if self.n == len(_TXBUF):
                self.flush()

    def flush(self):
        if self.n and self.ok:
            try:
                written = self.port.write(_TXVIEW[:self.n]) or 0
            except Exception:
                written = 0
            self.ok = written == self.n
        self.n = 0


from settings_store import emit_json as _emit     # JSON piece by piece (shared with saving)

FLAT_TEXT = 600              # longest string value a message may hold to be sent in one piece


def _flat(obj):
    """True for a small message: no lists or dicts in it, no long texts."""
    if len(obj) > 12:
        return False
    for key in obj:
        value = obj[key]
        kind = type(value)
        if kind is dict or kind is list or kind is tuple:
            return False
        if kind is str and len(value) > FLAT_TEXT:
            return False
    return True


class Link:
    """Talks to the web page on whichever serial port it connects to.

    Normally that is the second ("data") port that boot.py creates.  If the
    page opened the CircuitPython console (REPL) port instead -- or the data
    port does not exist because boot.py has not run -- the firmware answers
    there too, so the page always gets a reply.
    """

    def __init__(self, ports):
        self.ports = [_SerialPort(n, p) for n, p in ports if p is not None]
        self.active = self.ports[0] if self.ports else None   # data port first
        self.peer = False                 # page said hello and is reading
        self.log = lambda msg, level="info": None    # set by remapper_main once debug() exists

    def on_console(self):
        return self.peer and self.active is not None and self.active.name == "console"

    def available(self):
        if self.active is None:
            return False
        return self.peer or (self.active.name == "data" and self.active.port.connected)

    def send(self, obj):
        """Send one JSON line. It is written in small pieces as it is encoded, so a large
        message (the settings) never needs one big block of free memory."""
        if not self.available():
            return False
        if type(obj) is dict and _flat(obj):
            # a small message (nearly all of them): one json.dumps and one write. Piece by piece
            # used about 1.5 KB of memory for even the shortest message - and the live "mouse is
            # moving" highlight sends one twice a second, so that was most of the memory used
            # while the mouse moved (3.37.1: 2.9 KB/s, a clean-up pause every 5 s).
            return self.send_raw(json.dumps(obj).encode())
        out = _Writer(self.active.port)
        _emit(out, obj)
        out.put("\n")
        out.flush()
        self.peer = self.peer and out.ok      # page stopped reading
        return out.ok

    def send_raw(self, text):
        """Send one line that is already JSON (a str or bytes, without the newline)."""
        if not self.available():
            return False
        port = self.active.port
        try:
            ok = (port.write(text) or 0) == len(text) and (port.write(b"\n") or 0) == 1
        except Exception:
            ok = False
        self.peer = self.peer and ok
        return ok

    def poll(self):
        """Return the complete JSON messages received on any port (an empty tuple, which needs
        no memory, on the passes - nearly all - when nothing arrived)."""
        messages = None
        for sp in self.ports:
            try:
                waiting = sp.port.in_waiting
                if waiting:
                    sp.rx.extend(sp.port.read(waiting))
            except Exception:
                continue
            while True:
                end = sp.rx.find(b"\n")
                if end < 0:
                    break
                if end > MAX_LINE:            # too long to be a command: dropped, never parsed
                    sp.rx = sp.rx[end + 1:]
                    self.log("Ignored a %d-byte line from the page (limit %d)" % (end, MAX_LINE), "warn")
                    continue
                line = bytes(memoryview(sp.rx)[:end]).strip()   # one copy, not two
                sp.rx = sp.rx[end + 1:]
                if not line.startswith(b"{"):
                    continue              # console typing / noise, not for us
                gc.collect()              # commands can be large: start from a tidy heap
                try:
                    msg = json.loads(line)
                    line = None
                except Exception:
                    self.log("Ignored malformed line from page (%d bytes)" % len(line), "warn")
                    continue
                if isinstance(msg, dict) and msg.get("cmd") == "hello":
                    self.active = sp      # answer on the port the page opened
                if messages is None:
                    messages = []
                messages.append(msg)
            if len(sp.rx) > MAX_LINE:
                sp.rx = bytearray()
        return messages or ()

    def port_name(self):
        return self.active.name if self.active else "none"
