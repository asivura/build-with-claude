"""Verbose Joke Fetcher for the Cardputer-Adv.

A teaching app: narrates each phase of an HTTP request on screen
so a 9-year-old sees the *journey* -- DNS lookup, TCP connect,
request, response -- instead of treating the internet as magic.

### Controls

    SPACE / ENTER   fetch a new fact
    ESC / Q         exit to launcher

### Network requirements

This app reads but never writes WiFi state. It assumes
``wifi_event.py`` has already brought up STA. If it hasn't, the
app shows a friendly "no WiFi" message instead of crashing.

### Why plain HTTP via raw socket (not urequests / HTTPS)

The first cut of this app used ``requests`` against
icanhazdadjoke.com over HTTPS. It failed with
``OSError: [Errno 12] ENOMEM`` -- the ESP32-S3 heap is already
populated by the LCD framebuffer, the NimBLE controller, the
keyboard driver, and our own globals by the time the user presses
SPACE, leaving ~65 KB free. MicroPython's TLS context wants
considerably more for the handshake; ``gc.collect()`` doesn't fix
it because the issue is total capacity, not fragmentation.

Plain HTTP via raw socket sidesteps the issue completely. As a
bonus it lets the narration cover more steps (DNS, connect, send,
recv) which is *more* educational than the urequests one-liner
would have been.

### API

    GET /?format=%25C+%25t HTTP/1.0
    Host: wttr.in
    User-Agent: cardputer
    Accept: text/plain
    Connection: close

wttr.in geolocates by source IP and returns a one-line weather
string like ``Sunny +67F`` (the format string is percent-encoded
so wttr.in's server-side custom-placeholders %C %t survive the
URL parser as literal characters). The first HTTP-only API we
tried, numbersapi.com, 301-redirects to HTTPS now and we get back
an HTML "Moved Permanently" page; wttr.in remains plain-HTTP for
the foreseeable future, and "real weather you can verify out the
window" is arguably a better kid demo than random trivia anyway.

### Layout (240x135)

    Header:   y=0..19    DARK band, ORANGE hairline at y=20
    Trace:    y=24..73   five 10 px rows of step narration
    Fact:     y=78..114  body text, word-wrapped, CREAM on BLACK
    Hint:     y=117..134 controls
"""

import gc
import time

import M5
import machine
from hardware import MatrixKeyboard


# Palette matches the rest of the suite.
_BLACK = 0x000000
_ORANGE = 0xCC785C
_CREAM = 0xF0EEE6
_DARK = 0x1F1F1F
_GRAY_MID = 0x777777
_RED = 0xFF6655
_GREEN = 0x55C878

_LCD = M5.Lcd
_W = 240
_H = 135

_TRACE_X = 6
_TRACE_Y0 = 24
_TRACE_LINE_H = 10
# Three trace lines is enough -- the journey is six steps, but the
# scrolling buffer keeps the latest three visible, which is what the
# eye is tracking anyway. The space freed up goes to the fact below.
_TRACE_MAX_LINES = 3

# The fact renders in DejaVu18 (set explicitly in _show_fact) for
# readability -- DejaVu9 size 1 is too small to read across a room
# for a 9-year-old. 19 px line height accommodates the descender.
_FACT_Y0 = 58
_FACT_LINE_H = 19
_FACT_MAX_LINES = 3
# Character-count wrap for DejaVu18: average glyph is ~11 px, screen
# minus margins is ~228 px, so 19 chars/line is safe with margin.
_FACT_MAX_CHARS = 19

# Local joke server running on the dev machine. Modern joke APIs
# are HTTPS-only and the device's TLS context doesn't fit in the
# free heap on this build, so we host the jokes on the same LAN
# instead and fetch over plain HTTP. See ``scripts/joke_server.py``.
# When the device leaves this network this host becomes
# unreachable -- update the IP to your laptop's LAN address (find
# it with ``ipconfig getifaddr en0`` on macOS).
_HOST = "192.168.1.96"
_PATH = "/joke"
_PORT = 8080

# Pause between narrated steps. Long enough that each new line lands
# in the eye; short enough that the fact arrives in under 3 s total.
_STEP_PAUSE_MS = 350

# Auto-scroll timing for fact content that overflows _FACT_MAX_LINES.
# 1500 ms per advance is comfortable to read; the extra pause at the
# wrap point lets the eye reset before the cycle restarts.
_FACT_AUTO_INTERVAL_MS = 1500
_FACT_AUTO_WRAP_PAUSE_MS = 3000

# Module state for the currently-displayed fact. Pulled out of
# ``_show_fact`` so scroll handlers and the auto-advance tick can
# mutate it without re-fetching.
_fact_lines = []
_fact_offset = 0
_fact_auto = False
_fact_next_advance_ms = 0


def _set_font():
    try:
        _LCD.setFont(_LCD.FONTS.DejaVu9)
    except Exception as e:
        print("joke: setFont fallback:", e)


def _draw_chrome():
    _LCD.fillScreen(_BLACK)
    _LCD.fillRect(0, 0, _W, 20, _DARK)
    _LCD.fillRect(0, 20, _W, 1, _ORANGE)
    _LCD.setTextSize(1)
    _LCD.setTextColor(_ORANGE, _DARK)
    _LCD.drawString("Joke Fetcher", 6, 5)
    _LCD.fillRect(0, _H - 18, _W, 18, _DARK)
    _LCD.setTextColor(_GRAY_MID, _DARK)
    hint = "SPACE new   ;. scroll   ESC"
    _LCD.drawString(hint, (_W - _LCD.textWidth(hint)) // 2, _H - 14)


def _clear_body():
    _LCD.fillRect(0, 21, _W, _H - 21 - 18, _BLACK)


def _trace(lines, msg, color=None):
    """Append a narration line to the on-screen trace.

    ``lines`` is the caller's growing list of (msg, color) tuples.
    Self-contained on font: explicitly sets DejaVu9 each call so
    _show_fact's font switch can't leak in here."""
    if color is None:
        color = _CREAM
    lines.append((msg, color))
    try:
        _LCD.setFont(_LCD.FONTS.DejaVu9)
    except Exception:
        pass
    _LCD.setTextSize(1)
    if len(lines) <= _TRACE_MAX_LINES:
        i = len(lines) - 1
        _LCD.setTextColor(color, _BLACK)
        _LCD.drawString(msg, _TRACE_X, _TRACE_Y0 + i * _TRACE_LINE_H)
        return
    # Overflow: scroll by re-rendering the tail.
    _LCD.fillRect(
        0, _TRACE_Y0 - 1, _W, _TRACE_LINE_H * _TRACE_MAX_LINES + 2, _BLACK,
    )
    for i, (m, c) in enumerate(lines[-_TRACE_MAX_LINES:]):
        _LCD.setTextColor(c, _BLACK)
        _LCD.drawString(m, _TRACE_X, _TRACE_Y0 + i * _TRACE_LINE_H)


def _word_wrap(text, max_chars):
    """Split on spaces; pack into lines no longer than ``max_chars``.

    Pixel-exact wrapping would require ``textWidth`` on every
    candidate substring, which is slow and overkill for a demo. A
    few-pixel overhang is invisible at this scale. A word longer
    than ``max_chars`` gets hard-broken (rare for these facts)."""
    if not text:
        return []
    words = text.split()
    out = []
    cur = ""
    for w in words:
        if not cur:
            cur = w
            continue
        if len(cur) + 1 + len(w) <= max_chars:
            cur = cur + " " + w
        else:
            out.append(cur)
            cur = w
    if cur:
        out.append(cur)
    return out


def _show_fact(text):
    """Load a new fact into the scroll view.

    Stores the wrapped lines in module state so the auto-advance
    tick and the manual scroll handlers can keep mutating without
    a re-fetch. Auto-scroll engages only when the content overflows
    ``_FACT_MAX_LINES`` -- short replies just sit centered without
    any moving parts."""
    global _fact_lines, _fact_offset, _fact_auto, _fact_next_advance_ms
    _fact_lines = _word_wrap(text, _FACT_MAX_CHARS)
    _fact_offset = 0
    _fact_auto = len(_fact_lines) > _FACT_MAX_LINES
    _fact_next_advance_ms = time.ticks_add(
        time.ticks_ms(), _FACT_AUTO_INTERVAL_MS,
    )
    _render_fact()


def _render_fact():
    """Repaint the fact area for the current ``_fact_offset``.

    Chevron indicators ('^' / 'v') in the right margin signal that
    more content sits above or below the visible window. They use
    DejaVu9 to stay compact while the body is DejaVu18."""
    _LCD.fillRect(
        0, _FACT_Y0 - 1, _W, _FACT_LINE_H * _FACT_MAX_LINES + 2, _BLACK,
    )
    if not _fact_lines:
        return
    visible = _fact_lines[_fact_offset : _fact_offset + _FACT_MAX_LINES]
    try:
        _LCD.setFont(_LCD.FONTS.DejaVu18)
    except Exception:
        pass
    _LCD.setTextSize(1)
    _LCD.setTextColor(_CREAM, _BLACK)
    for i, ln in enumerate(visible):
        x = (_W - _LCD.textWidth(ln)) // 2
        if x < 4:
            x = 4
        _LCD.drawString(ln, x, _FACT_Y0 + i * _FACT_LINE_H)
    try:
        _LCD.setFont(_LCD.FONTS.DejaVu9)
    except Exception:
        pass
    # Scroll indicators -- only shown when there's actually content
    # off-screen in that direction, so on short replies they stay
    # invisible and don't add clutter.
    _LCD.setTextColor(_GRAY_MID, _BLACK)
    if _fact_offset > 0:
        _LCD.drawString("^", _W - 10, _FACT_Y0)
    if _fact_offset + _FACT_MAX_LINES < len(_fact_lines):
        _LCD.drawString(
            "v",
            _W - 10,
            _FACT_Y0 + _FACT_LINE_H * _FACT_MAX_LINES - 11,
        )


def _scroll_fact(delta):
    """Manual scroll. Cancels auto-scroll -- once the user takes
    over we don't want to fight them for control."""
    global _fact_offset, _fact_auto
    if not _fact_lines:
        return
    max_off = max(0, len(_fact_lines) - _FACT_MAX_LINES)
    new = _fact_offset + delta
    if new < 0:
        new = 0
    if new > max_off:
        new = max_off
    if new != _fact_offset:
        _fact_offset = new
        _fact_auto = False
        _render_fact()


def _fact_tick():
    """Advance the auto-scroll cycle if it's still enabled. Called
    every main-loop pass; no-op when there's nothing to advance."""
    global _fact_offset, _fact_next_advance_ms
    if not _fact_auto:
        return
    if not _fact_lines:
        return
    if time.ticks_diff(_fact_next_advance_ms, time.ticks_ms()) > 0:
        return
    max_off = max(0, len(_fact_lines) - _FACT_MAX_LINES)
    if _fact_offset >= max_off:
        # Reached the bottom: wrap back to the top after a longer
        # pause so the eye has time to recognize the cycle.
        _fact_offset = 0
        _fact_next_advance_ms = time.ticks_add(
            time.ticks_ms(),
            _FACT_AUTO_INTERVAL_MS + _FACT_AUTO_WRAP_PAUSE_MS,
        )
    else:
        _fact_offset += 1
        _fact_next_advance_ms = time.ticks_add(
            time.ticks_ms(), _FACT_AUTO_INTERVAL_MS,
        )
    _render_fact()


def _wifi_check():
    """Return (connected, ip). Read-only -- wifi_event.py owns the
    connect path."""
    try:
        import network
        sta = network.WLAN(network.STA_IF)
        if sta.active() and sta.isconnected():
            return True, sta.ifconfig()[0]
    except Exception as e:
        print("joke: wifi probe failed:", e)
    return False, None


def _no_wifi_screen():
    _clear_body()
    _LCD.setTextSize(1)
    _LCD.setTextColor(_RED, _BLACK)
    a = "No WiFi yet."
    _LCD.drawString(a, (_W - _LCD.textWidth(a)) // 2, 40)
    _LCD.setTextColor(_GRAY_MID, _BLACK)
    b = "Edit wifi_event.py with"
    c = "your network and reboot."
    _LCD.drawString(b, (_W - _LCD.textWidth(b)) // 2, 62)
    _LCD.drawString(c, (_W - _LCD.textWidth(c)) // 2, 76)


def _splash(ip):
    _clear_body()
    _LCD.setTextSize(1)
    _LCD.setTextColor(_GRAY_MID, _BLACK)
    ip_line = "online as {}".format(ip)
    _LCD.drawString(ip_line, (_W - _LCD.textWidth(ip_line)) // 2, 50)
    _LCD.setTextColor(_CREAM, _BLACK)
    prompt = "Press SPACE for a joke."
    _LCD.drawString(prompt, (_W - _LCD.textWidth(prompt)) // 2, 70)


def _do_fetch():
    """Run one HTTP request, narrating each step. Returns the body
    text, or None on failure (failures get narrated in red and the
    function returns)."""
    import socket
    gc.collect()
    lines = []
    _clear_body()

    _trace(lines, "1. Looking up host...")
    time.sleep_ms(_STEP_PAUSE_MS)
    try:
        infos = socket.getaddrinfo(_HOST, _PORT)
        addr = infos[0][-1]
        ip = addr[0]
    except Exception as e:
        _trace(lines, "   DNS failed: {}".format(e), _RED)
        return None
    _trace(lines, "   Found: {}".format(ip), _GREEN)
    time.sleep_ms(_STEP_PAUSE_MS)

    _trace(lines, "2. Connecting on port {}...".format(_PORT))
    time.sleep_ms(_STEP_PAUSE_MS)
    s = socket.socket()
    try:
        try:
            s.connect(addr)
        except Exception as e:
            _trace(lines, "   {}".format(e), _RED)
            return None

        _trace(lines, "3. Sending GET request...")
        time.sleep_ms(_STEP_PAUSE_MS)
        req = (
            "GET " + _PATH + " HTTP/1.0\r\n"
            "Host: " + _HOST + "\r\n"
            "User-Agent: cardputer\r\n"
            "Accept: text/plain\r\n"
            "Connection: close\r\n\r\n"
        )
        try:
            s.send(req.encode())
        except Exception as e:
            _trace(lines, "   send failed: {}".format(e), _RED)
            return None

        _trace(lines, "4. Reading the reply...")
        time.sleep_ms(_STEP_PAUSE_MS)
        buf = b""
        try:
            while True:
                chunk = s.recv(256)
                if not chunk:
                    break
                buf += chunk
        except Exception as e:
            _trace(lines, "   recv failed: {}".format(e), _RED)
            return None
    finally:
        try:
            s.close()
        except Exception:
            pass

    # HTTP/1.x: an empty line (CRLF CRLF) separates headers from body.
    sep = buf.find(b"\r\n\r\n")
    if sep < 0:
        _trace(lines, "   malformed reply", _RED)
        return None
    body = buf[sep + 4:].decode().strip()
    _trace(lines, "5. Got {} bytes!".format(len(body)), _GREEN)
    time.sleep_ms(_STEP_PAUSE_MS)
    return body


def _key_intent(k):
    """('exit' | 'fetch' | 'up' | 'down' | None).

    Space / Enter fetch; ESC / Q exit; ``;`` `.` (the keys with
    physical up / down arrow labels on the Cardputer-Adv) scroll
    the fact area when content overflows. WASD aliases mirror the
    snake-app convention so muscle memory carries across the suite."""
    if k is None:
        return None
    if isinstance(k, int):
        if k == 0x1B:
            return "exit"
        if k in (0x0A, 0x0D, 0x20):
            return "fetch"
        if 0x20 <= k <= 0x7E:
            k = chr(k)
        else:
            return None
    if not isinstance(k, str) or not k:
        return None
    ch = k.lower()
    if ch == "q":
        return "exit"
    if ch == " ":
        return "fetch"
    if ch in ("w", ";"):
        return "up"
    if ch in ("s", "."):
        return "down"
    return None


def run():
    _set_font()
    _draw_chrome()
    kb = MatrixKeyboard()
    # Debounce the launcher keypress so it doesn't trigger an
    # instant fetch the moment the app opens.
    time.sleep_ms(400)

    connected, ip = _wifi_check()
    if not connected:
        _no_wifi_screen()
        try:
            while True:
                kb.tick()
                if _key_intent(kb.get_key()) == "exit":
                    return
                time.sleep_ms(40)
        finally:
            try:
                _LCD.fillScreen(_BLACK)
            except Exception:
                pass
            time.sleep_ms(200)
            machine.reset()

    _splash(ip)
    try:
        while True:
            kb.tick()
            intent = _key_intent(kb.get_key())
            if intent == "exit":
                return
            if intent == "fetch":
                body = _do_fetch()
                if body:
                    _show_fact(body)
                else:
                    _show_fact("(try again)")
            elif intent == "up":
                _scroll_fact(-1)
            elif intent == "down":
                _scroll_fact(1)
            _fact_tick()
            time.sleep_ms(40)
    finally:
        # Mirror snake / hello / claude_buddy: clear, brief pause,
        # soft-reset back to the launcher.
        try:
            _LCD.fillScreen(_BLACK)
        except Exception:
            pass
        time.sleep_ms(200)
        machine.reset()


# UIFlow App List invokes apps both as __main__ and via import; the
# empirical behavior is always-run, so just call run() bare.
run()
