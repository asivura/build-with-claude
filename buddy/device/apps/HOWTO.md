# How to build a Cardputer-Adv app

A practical guide distilled from one evening of building Whack-a-Mole,
a Morse Code trainer, and a Verbose Joke / Weather Fetcher with a
nine-year-old. Everything here was learned by burning batteries and
reading tracebacks on a real device.

The audience is a parent or kid who wants to add a new app to the
buddy bundle. You don't need to read every section — skim, find what
matches your idea, copy a snippet.

---

## 1. Where apps live

```
buddy/device/
├── main.py              # the launcher menu (don't touch unless adding launcher features)
└── apps/
    ├── morse.py         # our apps live here, one file each
    ├── whack_a_mole.py
    ├── joke_fetcher.py
    └── your_app.py      # ← drop yours here
```

`main.py` scans `/flash/apps/` at boot and shows every `.py` it finds
as a menu entry, labeled by the bare filename (`whack_a_mole`,
`morse`, …). Press Enter on a menu line and the launcher does
`exec(open(path).read())` — your file runs as a script.

**Each app should be self-contained in one file.** No peer modules to
import. Keeps `install_apps.py` happy (one upload per app), keeps the
device's flash uncluttered, and means a kid who only wants Snake
doesn't get the BLE-client code along for the ride.

---

## 2. The standard chrome

Every app in this bundle paints the same three-zone layout so they
feel like a suite, not a grab-bag:

```
y=  0..19    DARK band (header)   →  app title left, optional info right
y=  20       1 px ORANGE hairline
y=  22..116  body                  →  the app
y=  117..134 DARK band (hints)    →  control summary
```

Inline palette (matches snake / hello / claude_buddy):

```python
_BLACK    = 0x000000
_ORANGE   = 0xCC785C    # the brand accent
_CREAM    = 0xF0EEE6    # primary text on black
_DARK     = 0x1F1F1F    # header / hint backgrounds
_GRAY_MID = 0x777777    # secondary text
_RED      = 0xFF6655    # error / wrong answer
_GREEN    = 0x55C878    # success / right answer
```

Standard `_draw_chrome`:

```python
def _draw_chrome():
    _LCD.fillScreen(_BLACK)
    _LCD.fillRect(0, 0, _W, 20, _DARK)
    _LCD.fillRect(0, 20, _W, 1, _ORANGE)
    _LCD.setTextSize(1)
    _LCD.setTextColor(_ORANGE, _DARK)
    _LCD.drawString("My App", 6, 5)
    _LCD.fillRect(0, _H - 18, _W, 18, _DARK)
    _LCD.setTextColor(_GRAY_MID, _DARK)
    hint = "SPACE action   ` exit"
    _LCD.drawString(hint, (_W - _LCD.textWidth(hint)) // 2, _H - 14)
```

Screen is **240 × 135 px**. Plan body layout against
`y ∈ [22, 116]` (94 px usable).

---

## 3. Fonts

The firmware exposes a healthy selection:

```
DejaVu9  DejaVu12  DejaVu18  DejaVu24  DejaVu40  DejaVu56  DejaVu72
Montserrat12  Montserrat14  …  Montserrat48
ASCII7  AlibabaPuHuiTiCN24  AlibabaSansJA24  AlibabaSansKR24  EFontCN24 …
```

Switch with `_LCD.setFont(_LCD.FONTS.DejaVu18)`. **Always wrap in
try/except** — some firmware variants are missing fonts, and a
crash here is worse than a default-font fallback.

Rough heights (with descenders, in pixels): 9→10, 12→14, 18→20,
24→26, 40→44.

For *kid-readable* big text, `DejaVu18` or `DejaVu24` is the sweet
spot. `DejaVu9` is fine for the chrome / hints but reads as tiny to
a nine-year-old.

**Always measure centered text with `_LCD.textWidth(text)` rather
than `len(text) * CHAR_W`** — the glyphs are proportional and a
naive multiply drifts by 5-10 px on real strings.

---

## 4. Keyboard input

`MatrixKeyboard` is a polled driver. Tick it on every main-loop pass,
then drain one key per pass with `get_key()`:

```python
from hardware import MatrixKeyboard

kb = MatrixKeyboard()
time.sleep_ms(400)   # debounce the launcher keypress

while True:
    kb.tick()
    k = kb.get_key()
    if k is not None:
        # handle key
        ...
    time.sleep_ms(40)
```

The 40 ms pass cadence is the suite convention. Slower feels laggy;
faster wastes battery without changing perceived responsiveness.

**`get_key()` returns either an `int` (printable ASCII code) or a
`str`, depending on firmware build.** Handle both:

```python
def _key_intent(k):
    if k is None:
        return None
    if isinstance(k, int):
        if k in (0x1B, 0x08, 0x09, 0x7F, 0x03):
            return ("exit",)            # ESC / BS / TAB / DEL / Ctrl-C
        if k == 0x20:
            return ("space",)
        if k in (0x0A, 0x0D):
            return ("enter",)
        if 0x20 < k <= 0x7E:
            k = chr(k)
        else:
            return None
    if not isinstance(k, str) or not k:
        return None
    ch = k.lower()
    # Centralize key→intent here so the rest of the app deals in
    # intents ("up", "fire", "exit") instead of raw key codes.
    if ch == ",": return ("left",)
    if ch == ".": return ("down",)
    if ch == ";": return ("up",)
    if ch == "/": return ("right",)
    # ...
    return None
```

When something doesn't work, **add `print(repr(k), type(k).__name__)`
inside `_key_intent`** and read `tail_serial.py` while pressing keys.
You'll discover, for example, that the ESC key on this firmware does
in fact report `0x1B` — but some characters surprise you.

### Exit key convention

`Q` and `ESC` are the "exit" keys in `snake.py` and `hello_cardputer.py`,
but our Morse Code app needs Q as a *playable letter*. The convention
this bundle has settled on:

| Key | Used for                              |
|-----|---------------------------------------|
| `` ` `` (backtick) | **Universal exit**, no conflicts with any Morse letter or digit |
| `ESC` | Accepted defensively as a backup exit (`0x1B` and `0x08` / `0x09` / `0x7F` / `0x03`) |
| `Q`   | Plays Q's Morse in Morse Code, exits in other apps. Not universal. |
| `,` `.` `;` `/` | Arrow-labeled on the physical keyboard, free for app-specific use |

**Always include backtick in your exit intent.** It's the one key
guaranteed not to clash with any letter, digit, or muscle-memory
expectation.

---

## 5. Speaker (sound)

`M5.Speaker.tone(frequency_hz, duration_ms)` is **non-blocking** —
it returns immediately; the tone plays asynchronously in the
background. To pace yourself, `time.sleep_ms(duration_ms)` after.

```python
_SPK = M5.Speaker
_SPK.tone(700, 200)        # 200 ms beep at 700 Hz
time.sleep_ms(200)         # let it finish before the next thing
```

**Volume gotcha.** `setVolumePercentage` takes a **0.0–1.0
fraction**, not a 0–100 percent, despite the method name. Calling
`setVolumePercentage(80)` silently clamps to 1.0 (full blast).
Always convert:

```python
_SPK.setVolumePercentage(volume_percent / 100.0)
```

There's also `setVolume(0..255)` if you want raw control.

`_SPK.stop()` cancels an in-flight tone — useful when you abort
playback mid-pattern.

---

## 6. WiFi + HTTP

The boot-time `wifi_event.py` (in `buddy/device/`) brings the STA
interface up using hard-coded credentials. Your app can read the
connection state without touching it:

```python
import network
sta = network.WLAN(network.STA_IF)
if sta.active() and sta.isconnected():
    ip = sta.ifconfig()[0]
```

### HTTPS does not fit in the heap

By the time the LCD framebuffer, the NimBLE controller, the
keyboard driver, and your own globals have loaded, **only ~65 KB
of MicroPython heap is free**. MicroPython's TLS context wants
substantially more for the handshake.

Every `requests.get('https://…')` we tried failed with
`OSError: [Errno 12] ENOMEM`. `gc.collect()` doesn't help — it's
total capacity, not fragmentation.

**Use plain HTTP.** Either find an HTTP-only endpoint (rare in
2026 — most APIs 301 to HTTPS) or run a server on your laptop and
fetch from it. The `joke_fetcher.py` app does the latter; see
`scripts/joke_server.py` for the laptop side.

### Plain HTTP via raw socket

For a teaching demo, narrating each HTTP step is more educational
than `urequests.get` anyway. A working pattern:

```python
import socket

infos = socket.getaddrinfo("wttr.in", 80)
ip = infos[0][-1][0]            # the magic name→number moment

s = socket.socket()
s.connect(infos[0][-1])
s.send((
    "GET /?format=%25C+%25t HTTP/1.0\r\n"
    "Host: wttr.in\r\n"
    "User-Agent: cardputer\r\n"
    "Connection: close\r\n\r\n"
).encode())

buf = b""
while True:
    chunk = s.recv(256)
    if not chunk:
        break
    buf += chunk
s.close()

sep = buf.find(b"\r\n\r\n")    # split headers / body
body = buf[sep + 4:].decode().strip()
```

Percent-encode any `%` you want to send literally (e.g. wttr.in's
`%C` placeholder becomes `%25C` in the URL) so the server's URL
parser doesn't try to decode it as a percent-escape.

---

## 7. Persistent settings via NVS

`esp32.NVS` gives you a key/value store that survives reboots.
The Morse app uses it to remember the volume setting:

```python
import esp32

def _load_volume():
    try:
        nvs = esp32.NVS("morse")
        v = nvs.get_i32("volume")
        if 0 <= v <= 100:
            return v
    except Exception:
        pass            # OSError ENOENT on first run is normal
    return 80           # default

def _save_volume(v):
    try:
        nvs = esp32.NVS("morse")
        nvs.set_i32("volume", int(v))
        nvs.commit()    # without commit() the value is lost on reboot
    except Exception as e:
        print("nvs save:", e)
```

**Pick a namespace per app** (`"morse"`, `"snake"`, …) so different
apps don't stomp on each other.

UIFlow 2.0 itself uses NVS for boot-time settings; its parser
distinguishes string and blob types and will boot-loop on a
type-mismatched read. As long as you stay in your own namespace,
this isn't a concern. Don't write to the `nvs.cfg` namespace.

---

## 8. Exiting an app

UIFlow 2.0 has no "return to launcher" API. Every app in this
bundle exits the same way: print-clean the screen, brief pause,
soft-reset the chip. The launcher comes up on the next boot.

```python
def run():
    ...
    try:
        while True:
            # main loop
            ...
    finally:
        try:
            _LCD.fillScreen(_BLACK)
        except Exception:
            pass
        time.sleep_ms(200)
        machine.reset()
```

`machine.reset()` is fast (<1 second back to the launcher) and
guarantees a clean heap for the next app. The brief screen-clear
before the reset hides the "last frame of the prior app" flicker
that otherwise shows up behind the launcher's first paint.

---

## 9. The dev loop

Three scripts in `buddy/scripts/` make iteration fast:

```bash
# Push edited files to the device (auto-reboots into the new code)
python3 scripts/push.py --port /dev/cu.usbmodem11301 \
    --files apps/your_app.py

# Tail the device's stdout for print() output / tracebacks
python3 scripts/tail_serial.py --port /dev/cu.usbmodem11301

# Run one-off MicroPython commands on the device via REPL
python3 scripts/repl_run.py --port /dev/cu.usbmodem11301 \
    --script "import M5; print(dir(M5))"
```

**Always test in this order**: write the file, `push.py`, then
launch from the device menu. If the app fails, immediately
`tail_serial.py` while pressing keys / repeating the action —
your `print()` calls and any tracebacks land there.

The `push.py` script unconditionally reboots the device when it
finishes (unless you pass `--no-reset`). If you have an ad-hoc
WiFi connect that doesn't survive reboot, your app may show
"No WiFi" after the push. Make the WiFi persistent in
`wifi_event.py` before iterating heavily, or you'll fight this
every push.

---

## 10. Hard-won gotchas (the short list)

| Surprise | Where you'll hit it | Fix |
|---|---|---|
| `OSError: [Errno 12] ENOMEM` on `requests.get('https://...')` | First HTTPS fetch from an app | Use plain HTTP via raw socket; run a server on your laptop if needed |
| `setVolumePercentage(80)` is silently 100 % | First time you tune speaker volume | Pass `pct / 100.0` (0.0-1.0 fraction) |
| Modern HTTP APIs return `<html>` redirect | First time you point at a "free joke API" | They all 301 to HTTPS now — use `wttr.in` or self-host |
| `ESC` doesn't exit your app | When the user can't get out | Use ``\``` (backtick) as the universal exit. Catch `0x1B` / `0x08` / `0x09` / `0x7F` / `0x03` defensively. |
| Q is in the Morse alphabet | Morse Code app | Don't use Q as an exit key in any app — pick a non-letter, non-digit key (backtick) |
| `urequests` isn't installed on this firmware | First time you `import urequests` | `import requests` — UIFlow 2.0 ships the `requests` shim, not `urequests` |
| `kb.get_key()` returns `int` sometimes, `str` other times | First weird key handling | Always branch on `isinstance(k, int)` first, then `str` |
| Mid-playback key presses queue and replay after | Speaker apps with long patterns | Poll keys *during* the sleep, not just between |
| Native USB Cardputer-Adv has no DTR/RTS reset | When you try to bootloader it via esptool | Manual BtnG0 + BtnRST dance; see `m5-onboard/SKILL.md` |
| WiFi ad-hoc connect dies on every reboot | Iterating with `push.py` | Bake your SSID + password into `wifi_event.py` |

---

## 11. Minimal app skeleton

Drop this in `buddy/device/apps/<name>.py`, edit `_TITLE` and the
key handlers, and push:

```python
"""<name> -- a starter app for the Cardputer-Adv."""

import time

import M5
import machine
from hardware import MatrixKeyboard

_BLACK = 0x000000
_ORANGE = 0xCC785C
_CREAM = 0xF0EEE6
_DARK = 0x1F1F1F
_GRAY_MID = 0x777777

_LCD = M5.Lcd
_W = 240
_H = 135

_TITLE = "<your title>"
_HINT = "SPACE action   ` exit"


def _set_font():
    try:
        _LCD.setFont(_LCD.FONTS.DejaVu9)
    except Exception:
        pass


def _draw_chrome():
    _LCD.fillScreen(_BLACK)
    _LCD.fillRect(0, 0, _W, 20, _DARK)
    _LCD.fillRect(0, 20, _W, 1, _ORANGE)
    _LCD.setTextSize(1)
    _LCD.setTextColor(_ORANGE, _DARK)
    _LCD.drawString(_TITLE, 6, 5)
    _LCD.fillRect(0, _H - 18, _W, 18, _DARK)
    _LCD.setTextColor(_GRAY_MID, _DARK)
    _LCD.drawString(_HINT, (_W - _LCD.textWidth(_HINT)) // 2, _H - 14)


def _key_intent(k):
    if k is None:
        return None
    if isinstance(k, int):
        if k in (0x1B, 0x08, 0x09, 0x7F, 0x03):
            return ("exit",)
        if k == 0x20:
            return ("action",)
        if 0x20 < k <= 0x7E:
            k = chr(k)
        else:
            return None
    if not isinstance(k, str) or not k:
        return None
    ch = k.lower()
    if ch == " ":
        return ("action",)
    if ch in ("`", "~"):
        return ("exit",)
    return None


def run():
    _set_font()
    _draw_chrome()
    kb = MatrixKeyboard()
    time.sleep_ms(400)
    try:
        while True:
            kb.tick()
            intent = _key_intent(kb.get_key())
            if intent is None:
                time.sleep_ms(40)
                continue
            if intent[0] == "exit":
                return
            if intent[0] == "action":
                # do the thing
                _LCD.fillRect(0, 22, _W, _H - 22 - 18, _BLACK)
                _LCD.setTextColor(_CREAM, _BLACK)
                _LCD.drawString("you pressed SPACE", 6, 60)
            time.sleep_ms(40)
    finally:
        try:
            _LCD.fillScreen(_BLACK)
        except Exception:
            pass
        time.sleep_ms(200)
        machine.reset()


run()
```

That's a working app. ~80 lines. Adds an entry to the launcher
the next time you reboot, runs on press, exits cleanly.

Build outward from here.

---

## 12. Reference: the three apps in this folder

| File | Purpose | Notable patterns |
|---|---|---|
| `whack_a_mole.py` | 30-second reflex game on a 3×3 grid | Per-cell rendering, time-based gameplay, tunable difficulty constants near top |
| `morse.py` | Plays Morse for any letter + quiz mode + settings menu | Multi-mode app, NVS-persisted volume, audio + visual sync, modal sub-screens |
| `joke_fetcher.py` | Verbose HTTP demo — narrates each request step | Raw-socket HTTP, DNS-as-its-own-step, scrollable response area, plain-text API |
| `claude_buddy.py` | BLE companion for Claude Desktop's Hardware Buddy | NimBLE peripheral, line-delimited JSON protocol, NVS-backed state |
| `hello_cardputer.py` | Smoke-test that became an app | Minimal pattern, keyboard echo |

When in doubt, **read the existing files**. Every pattern here is
demonstrated in at least one of them.
