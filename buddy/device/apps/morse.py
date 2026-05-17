"""Morse Code Trainer for the Cardputer-Adv.

Press any letter or digit and the device:
  - displays the character large on screen
  - shows its Morse pattern (e.g. ``. -``)
  - beeps the pattern through the speaker at 700 Hz
  - flashes a synchronized indicator bar so the rhythm is visible
    even when the speaker is too quiet to hear across a room

The combo of audio + visual is the educational point: a kid pressing
'A' hears di-DAH *and* sees ``. -`` blinking in sync. Builds Morse
fluency the way phonics tiles build reading.

### Modes

The app has two modes:

* **Play** (default) -- press a letter or digit and hear its Morse.
* **Quiz** -- the device plays a random pattern and the kid types
  her guess. Score climbs on correct, miss count on wrong. Built
  on top of the same playback engine as Play mode.

Press **ENTER** to toggle. Backtick always exits the whole app.

### Controls

In Play mode:
    A-Z, 0-9    play the Morse pattern for that character
    SPACE       open the settings menu (volume adjust)
    ENTER       enter Quiz mode
    `           exit to launcher

In Quiz mode:
    A-Z, 0-9    submit as guess for the current pattern
    SPACE       skip current letter (counts as a miss)
    ENTER       back to Play mode
    `           exit to launcher

Q is *playable* (real Morse letter), not the exit. The dedicated
exit is ``\``` (backtick) which doesn't clash with any Morse symbol.

### Volume

Volume is persisted in NVS under namespace ``morse``, key ``volume``
as an int32 (0-100, percent). Default on first run is 80. The
hardware API is ``M5.Speaker.setVolumePercentage(fraction)`` where
``fraction`` is 0.0-1.0 *despite* the method name -- so we always
divide our percent by 100 before applying.

### Timing

    dit               = 200 ms  (~5 WPM, comfortable for a learner)
    dah               = 3 dits = 600 ms
    inter-element gap = 1 dit  = 200 ms
    inter-letter gap  = 3 dits = 600 ms (after pattern finishes)
    tone              = 700 Hz (the classic CW pitch)

### Why playback blocks the main loop

``_play_morse`` uses ``time.sleep_ms`` between elements rather than
running an async tick. Sleep is the right tool here: the kid can't
do anything useful mid-playback anyway, and a non-blocking design
would just bloat the code for no UX gain. Buffered keypresses
during a long letter (e.g., '0' is five dahs, ~4 s) get drained
afterward by ``_drain_kb`` so the app doesn't auto-replay a queue
of impatient mashes.

### Layout (240x135)

    Header:        y=0..19    DARK band, ORANGE hairline
    Big letter:    y=24..68   DejaVu40, centered, CREAM
    Morse pattern: y=74..94   DejaVu18, centered, ORANGE
    Flash bar:     y=100..114 240x14, ORANGE on tone / BLACK off
    Hint:          y=117..134 controls
"""

import random
import time

import M5
import machine
from hardware import MatrixKeyboard


_BLACK = 0x000000
_ORANGE = 0xCC785C
_CREAM = 0xF0EEE6
_DARK = 0x1F1F1F
_GRAY_MID = 0x777777
_GREEN = 0x55C878
_RED = 0xFF6655

_LCD = M5.Lcd
_SPK = M5.Speaker

_W = 240
_H = 135

_LETTER_Y = 24
_PATTERN_Y = 74
_FLASH_Y = 100
_FLASH_H = 14

_DIT_MS = 200
_DAH_MS = _DIT_MS * 3
_ELEMENT_GAP_MS = _DIT_MS
_LETTER_GAP_MS = _DIT_MS * 3

_TONE_HZ = 700

# Standard international Morse for A-Z and 0-9. The kid can extend
# this table for punctuation (``.`` = ``.-.-.-`` etc.) by dropping
# entries below; ``_key_intent`` already accepts any ASCII printable
# the table has a mapping for.
# Volume change step in the settings UI.
_VOL_STEP = 10
_VOL_DEFAULT = 80


def _load_volume():
    """Read the persisted volume from NVS, defaulting to
    ``_VOL_DEFAULT`` if the key is missing or out of range.
    Returns an int in 0..100."""
    try:
        import esp32
        nvs = esp32.NVS("morse")
        v = nvs.get_i32("volume")
        if 0 <= v <= 100:
            return v
    except Exception as e:
        # Most common path: key doesn't exist yet on first launch,
        # which raises OSError ENOENT. Treat any failure as "use
        # the default" -- we'd rather show a working app at default
        # volume than crash on a missing key.
        print("morse: _load_volume:", e)
    return _VOL_DEFAULT


def _save_volume(v):
    try:
        import esp32
        nvs = esp32.NVS("morse")
        nvs.set_i32("volume", int(v))
        nvs.commit()
    except Exception as e:
        print("morse: _save_volume:", e)


def _apply_volume(v):
    """Push the percent value at the hardware. ``setVolumePercentage``
    takes a 0.0-1.0 fraction despite the method name -- passing an
    integer like 80 clamps to 1.0 (full blast)."""
    try:
        _SPK.setVolumePercentage(v / 100.0)
    except Exception as e:
        print("morse: _apply_volume:", e)


_MORSE = {
    "a": ".-",    "b": "-...",  "c": "-.-.",  "d": "-..",
    "e": ".",     "f": "..-.",  "g": "--.",   "h": "....",
    "i": "..",    "j": ".---",  "k": "-.-",   "l": ".-..",
    "m": "--",    "n": "-.",    "o": "---",   "p": ".--.",
    "q": "--.-",  "r": ".-.",   "s": "...",   "t": "-",
    "u": "..-",   "v": "...-",  "w": ".--",   "x": "-..-",
    "y": "-.--",  "z": "--..",
    "0": "-----", "1": ".----", "2": "..---", "3": "...--",
    "4": "....-", "5": ".....", "6": "-....", "7": "--...",
    "8": "---..", "9": "----.",
}


def _set_chrome_font():
    try:
        _LCD.setFont(_LCD.FONTS.DejaVu9)
    except Exception:
        pass


def _draw_chrome():
    _LCD.fillScreen(_BLACK)
    _LCD.fillRect(0, 0, _W, 20, _DARK)
    _LCD.fillRect(0, 20, _W, 1, _ORANGE)
    _set_chrome_font()
    _LCD.setTextSize(1)
    _LCD.setTextColor(_ORANGE, _DARK)
    _LCD.drawString("Morse Code", 6, 5)
    _LCD.fillRect(0, _H - 18, _W, 18, _DARK)
    _LCD.setTextColor(_GRAY_MID, _DARK)
    hint = "SPC setup  ENT quiz  ` exit"
    _LCD.drawString(hint, (_W - _LCD.textWidth(hint)) // 2, _H - 14)


def _clear_body():
    _LCD.fillRect(0, 21, _W, _H - 21 - 18, _BLACK)


def _show_idle():
    _clear_body()
    _set_chrome_font()
    _LCD.setTextSize(1)
    _LCD.setTextColor(_GRAY_MID, _BLACK)
    msg = "press any letter or digit"
    _LCD.drawString(msg, (_W - _LCD.textWidth(msg)) // 2, 60)


def _show_letter(ch, pattern):
    """Repaint the body with the big letter + its Morse pattern.
    Called once per keypress, before playback begins, so the kid
    can read the pattern while the speaker plays it."""
    _clear_body()
    try:
        _LCD.setFont(_LCD.FONTS.DejaVu40)
    except Exception:
        pass
    _LCD.setTextSize(1)
    _LCD.setTextColor(_CREAM, _BLACK)
    text = ch.upper()
    x = (_W - _LCD.textWidth(text)) // 2
    _LCD.drawString(text, x, _LETTER_Y)
    try:
        _LCD.setFont(_LCD.FONTS.DejaVu18)
    except Exception:
        pass
    _LCD.setTextColor(_ORANGE, _BLACK)
    # Space-separate the symbols so the kid can count them.
    pattern_text = " ".join(pattern)
    x = (_W - _LCD.textWidth(pattern_text)) // 2
    _LCD.drawString(pattern_text, x, _PATTERN_Y)
    _set_chrome_font()


def _flash_on():
    _LCD.fillRect(0, _FLASH_Y, _W, _FLASH_H, _ORANGE)


def _flash_off():
    _LCD.fillRect(0, _FLASH_Y, _W, _FLASH_H, _BLACK)


def _check_play_abort(kb):
    """Default abort-check used during Play-mode playback. Returns
    the intent tuple to propagate (so the caller knows *why* the
    abort happened) or None to keep playing. Letter / digit
    presses during playback are deliberately ignored -- can't
    listen and guess at the same time."""
    kb.tick()
    k = kb.get_key()
    if k is None:
        return None
    intent = _key_intent(k)
    if intent is None:
        return None
    if intent[0] in ("exit", "toggle_mode"):
        return intent
    return None


def _check_quiz_abort(kb):
    """Abort-check used during Quiz-mode playback. Like
    ``_check_play_abort`` but also surfaces SPACE (settings intent,
    repurposed as 'skip' in Quiz)."""
    kb.tick()
    k = kb.get_key()
    if k is None:
        return None
    intent = _key_intent(k)
    if intent is None:
        return None
    if intent[0] in ("exit", "toggle_mode", "settings"):
        return intent
    return None


def _sleep_or_abort(kb, ms, check_fn):
    """Sleep for ``ms`` while polling ``check_fn`` every 20 ms.
    Returns whatever ``check_fn`` returned when truthy, or None if
    the wait completed normally."""
    end = time.ticks_add(time.ticks_ms(), ms)
    while time.ticks_diff(end, time.ticks_ms()) > 0:
        r = check_fn(kb)
        if r:
            return r
        time.sleep_ms(20)
    return None


def _play_element(kb, duration_ms, check_fn):
    """One dit or dah: tone + flash, both for ``duration_ms``.
    On abort, the sound is stopped and the abort value is returned
    so it propagates up through the pattern."""
    _flash_on()
    try:
        _SPK.tone(_TONE_HZ, duration_ms)
    except Exception as e:
        print("morse: tone failed:", e)
    aborted = _sleep_or_abort(kb, duration_ms, check_fn)
    _flash_off()
    if aborted:
        try:
            _SPK.stop()
        except Exception:
            pass
    return aborted


def _play_morse(kb, pattern, check_fn=None):
    """Play one full character. Returns either:

      None                  -- pattern completed normally
      ('exit',)             -- user pressed backtick / ESC
      ('toggle_mode',)      -- user pressed ENTER
      ('settings',)         -- user pressed SPACE  (Quiz mode only,
                               where it means 'skip this letter')

    The return type is polymorphic so the caller can dispatch on
    *why* playback ended. Pass ``check_fn=_check_quiz_abort`` for
    Quiz mode; the default catches the Play-mode intents."""
    if check_fn is None:
        check_fn = _check_play_abort
    for i, sym in enumerate(pattern):
        if i > 0:
            r = _sleep_or_abort(kb, _ELEMENT_GAP_MS, check_fn)
            if r:
                return r
        if sym == ".":
            r = _play_element(kb, _DIT_MS, check_fn)
        elif sym == "-":
            r = _play_element(kb, _DAH_MS, check_fn)
        else:
            r = None
        if r:
            return r
    return _sleep_or_abort(kb, _LETTER_GAP_MS, check_fn)


def _draw_quiz_chrome(score, miss, hint=None):
    """Quiz header: title left, score X/N right (where N is the
    number of rounds played so far). Repaints header + hint strip
    so it can be called between rounds without flashing the body.

    The hint is phase-specific so the screen actually tells the kid
    what to press right now -- a static hint would say 'type guess'
    even on the result screen, which is the bug we just fixed."""
    _LCD.fillRect(0, 0, _W, 20, _DARK)
    _LCD.fillRect(0, 20, _W, 1, _ORANGE)
    _set_chrome_font()
    _LCD.setTextSize(1)
    _LCD.setTextColor(_ORANGE, _DARK)
    _LCD.drawString("Quiz", 6, 5)
    _LCD.setTextColor(_CREAM, _DARK)
    total = score + miss
    sc = "{}/{}".format(score, total)
    x = _W - 6 - _LCD.textWidth(sc)
    _LCD.drawString(sc, x, 5)
    _LCD.fillRect(0, _H - 18, _W, 18, _DARK)
    _LCD.setTextColor(_GRAY_MID, _DARK)
    if hint is None:
        hint = "type guess  SPC skip  ` exit"
    _LCD.drawString(hint, (_W - _LCD.textWidth(hint)) // 2, _H - 14)


def _show_quiz_listen(score, miss):
    """Body during playback: a big '?' so the kid knows it's a
    quiz (not a pattern they can read off the screen)."""
    _draw_quiz_chrome(score, miss, "listening...  SPC skip  ` exit")
    _clear_body()
    try:
        _LCD.setFont(_LCD.FONTS.DejaVu40)
    except Exception:
        pass
    _LCD.setTextSize(1)
    _LCD.setTextColor(_ORANGE, _BLACK)
    q = "?"
    _LCD.drawString(q, (_W - _LCD.textWidth(q)) // 2, _LETTER_Y)
    _set_chrome_font()
    _LCD.setTextColor(_GRAY_MID, _BLACK)
    msg = "listen..."
    _LCD.drawString(msg, (_W - _LCD.textWidth(msg)) // 2, _PATTERN_Y + 4)
    # Flash bar starts blank; _play_morse will animate it.
    _LCD.fillRect(0, _FLASH_Y, _W, _FLASH_H, _BLACK)


def _show_quiz_guess(score, miss):
    """Body after playback: '?' stays, prompt invites a guess."""
    _draw_quiz_chrome(score, miss, ", replay  letter  SPC skip")
    _clear_body()
    try:
        _LCD.setFont(_LCD.FONTS.DejaVu40)
    except Exception:
        pass
    _LCD.setTextSize(1)
    _LCD.setTextColor(_CREAM, _BLACK)
    q = "?"
    _LCD.drawString(q, (_W - _LCD.textWidth(q)) // 2, _LETTER_Y)
    _set_chrome_font()
    _LCD.setTextColor(_GRAY_MID, _BLACK)
    msg = "which letter?"
    _LCD.drawString(msg, (_W - _LCD.textWidth(msg)) // 2, _PATTERN_Y + 4)


def _show_quiz_result(ch, pattern, guess, correct, score, miss):
    """Result body: outcome banner, big correct letter, pattern.

    ``correct`` is True (right answer), False (wrong answer), or
    None (skipped) so the banner can color-code all three states."""
    _draw_quiz_chrome(score, miss, ", replay   any key = next")
    _clear_body()
    # Outcome banner just under the header.
    try:
        _LCD.setFont(_LCD.FONTS.DejaVu18)
    except Exception:
        pass
    _LCD.setTextSize(1)
    if correct is True:
        color = _GREEN
        title = "Correct!"
    elif correct is False:
        color = _RED
        title = "Nope - it was"
    else:
        color = _GRAY_MID
        title = "Skipped"
    _LCD.setTextColor(color, _BLACK)
    _LCD.drawString(title, (_W - _LCD.textWidth(title)) // 2, 24)
    # Big correct letter.
    try:
        _LCD.setFont(_LCD.FONTS.DejaVu40)
    except Exception:
        pass
    _LCD.setTextColor(_CREAM, _BLACK)
    big = ch.upper()
    _LCD.drawString(big, (_W - _LCD.textWidth(big)) // 2, 46)
    # Morse pattern under the letter.
    try:
        _LCD.setFont(_LCD.FONTS.DejaVu18)
    except Exception:
        pass
    _LCD.setTextColor(_ORANGE, _BLACK)
    pat = " ".join(pattern)
    _LCD.drawString(pat, (_W - _LCD.textWidth(pat)) // 2, 92)
    _set_chrome_font()


def _await_quiz_input(kb):
    """Wait for the kid's response after a quiz playback completes.

    Returns:
      ('EXIT',)          -- backtick / ESC: exit the whole app
      ('TOGGLE',)        -- ENTER: switch back to Play mode
      ('SKIP',)          -- SPACE: give up on this letter
      ('REPLAY',)        -- ',': hear the pattern again
      ('GUESS', ch)      -- a Morse-mappable key: their guess
    """
    while True:
        kb.tick()
        intent = _key_intent(kb.get_key())
        if intent is None:
            time.sleep_ms(40)
            continue
        if intent[0] == "exit":
            return ("EXIT",)
        if intent[0] == "toggle_mode":
            return ("TOGGLE",)
        if intent[0] == "settings":
            return ("SKIP",)
        if intent[0] == "replay":
            return ("REPLAY",)
        if intent[0] == "play":
            return ("GUESS", intent[1])


def _wait_for_advance(kb):
    """Sit on the result screen until the kid moves on.

    Returns one of:
      'EXIT'    -- backtick / ESC: exit the whole app
      'TOGGLE'  -- ENTER: back to Play mode
      'REPLAY'  -- ',': hear the (now-revealed) pattern one more time
      'NEXT'    -- any other key: advance to a new letter
    """
    while True:
        kb.tick()
        intent = _key_intent(kb.get_key())
        if intent is None:
            time.sleep_ms(40)
            continue
        if intent[0] == "exit":
            return "EXIT"
        if intent[0] == "toggle_mode":
            return "TOGGLE"
        if intent[0] == "replay":
            return "REPLAY"
        return "NEXT"


def _quiz_loop(kb):
    """Run the quiz until the kid exits or toggles back to Play.

    Returns True if the caller should exit the whole app
    (backtick), False if they should return to Play mode (ENTER).
    Score and miss counts are local -- a fresh quiz starts each
    time you enter the mode.

    Loop structure:

      outer:    one iteration per *letter* (a new random pick)
        inner (replay-before-guess):
          listen -> guess
          REPLAY response sends us back to listen with the same
          letter; this is what the kid asked for so she can hear
          a pattern she missed without losing the current round.
        result-wait:
          show the answer, wait for next-key. REPLAY here re-plays
          the (now-revealed) pattern and returns to the same
          result screen so she can hear it as many times as she
          needs before moving on.
    """
    score = 0
    miss = 0
    letters = list(_MORSE.keys())

    while True:
        ch = random.choice(letters)
        pattern = _MORSE[ch]
        last_guess = None
        last_correct = None

        # --- inner loop: listen + guess, with replay-before-guess.
        while True:
            _show_quiz_listen(score, miss)
            result = _play_morse(kb, pattern, _check_quiz_abort)
            if result is not None:
                kind = result[0]
                if kind == "exit":
                    return True
                if kind == "toggle_mode":
                    return False
                if kind == "settings":
                    # SPACE during playback -- skip.
                    miss += 1
                    _show_quiz_result(
                        ch, pattern, None, None, score, miss,
                    )
                    break

            _show_quiz_guess(score, miss)
            res = _await_quiz_input(kb)
            tag = res[0]
            if tag == "EXIT":
                return True
            if tag == "TOGGLE":
                return False
            if tag == "REPLAY":
                continue  # back to _show_quiz_listen
            if tag == "SKIP":
                miss += 1
                _show_quiz_result(
                    ch, pattern, None, None, score, miss,
                )
            else:
                last_guess = res[1]
                last_correct = (last_guess == ch)
                if last_correct:
                    score += 1
                else:
                    miss += 1
                _show_quiz_result(
                    ch, pattern, last_guess, last_correct,
                    score, miss,
                )
            break

        # --- result-wait loop: allow replay after seeing the answer.
        while True:
            adv = _wait_for_advance(kb)
            if adv == "EXIT":
                return True
            if adv == "TOGGLE":
                return False
            if adv == "REPLAY":
                # Re-play the now-revealed pattern, then re-show
                # the same result screen. Use the lighter
                # play-mode abort check (no skip here -- skip
                # doesn't make sense after the answer is shown).
                _show_quiz_listen(score, miss)
                r = _play_morse(kb, pattern, _check_play_abort)
                if r is not None:
                    if r[0] == "exit":
                        return True
                    if r[0] == "toggle_mode":
                        return False
                _show_quiz_result(
                    ch, pattern, last_guess, last_correct,
                    score, miss,
                )
                continue
            break  # adv == "NEXT" -- advance to a new letter


def _show_settings(volume):
    """Repaint the full screen as the settings page for the given
    volume percent."""
    _LCD.fillScreen(_BLACK)
    _LCD.fillRect(0, 0, _W, 20, _DARK)
    _LCD.fillRect(0, 20, _W, 1, _ORANGE)
    _set_chrome_font()
    _LCD.setTextSize(1)
    _LCD.setTextColor(_ORANGE, _DARK)
    _LCD.drawString("Settings", 6, 5)

    # Big volume value at the top of the body.
    try:
        _LCD.setFont(_LCD.FONTS.DejaVu24)
    except Exception:
        pass
    _LCD.setTextColor(_CREAM, _BLACK)
    label = "Volume {}%".format(volume)
    x = (_W - _LCD.textWidth(label)) // 2
    _LCD.drawString(label, x, 30)

    # Horizontal volume bar -- visually proportional fill on top of
    # a dark trough, with a 1 px gray frame so the trough reads as
    # a container even when the fill is at 0.
    bar_x = 20
    bar_y = 70
    bar_w = _W - 40
    bar_h = 20
    _LCD.fillRect(bar_x, bar_y, bar_w, bar_h, _DARK)
    fill_w = (bar_w * volume) // 100
    if fill_w > 0:
        _LCD.fillRect(bar_x, bar_y, fill_w, bar_h, _ORANGE)
    try:
        _LCD.drawRect(bar_x - 1, bar_y - 1, bar_w + 2, bar_h + 2, _GRAY_MID)
    except Exception:
        # drawRect missing on some firmware variants; the bar still
        # reads fine without the outline.
        pass

    _set_chrome_font()
    _LCD.fillRect(0, _H - 18, _W, 18, _DARK)
    _LCD.setTextColor(_GRAY_MID, _DARK)
    hint = ";(up) louder   .(down) quieter   ` back"
    _LCD.drawString(hint, (_W - _LCD.textWidth(hint)) // 2, _H - 14)


def _settings_loop(kb, current_volume):
    """Modal volume editor. Persists on exit. Returns the new
    volume so the caller can stop tracking the value itself."""
    volume = current_volume
    _show_settings(volume)
    while True:
        kb.tick()
        k = kb.get_key()
        if k is None:
            time.sleep_ms(40)
            continue

        # Decode here rather than reusing ``_key_intent`` -- the
        # settings UI has its own keymap (`;`/`.` are scroll, not
        # play, and we don't want a digit press to leak through).
        if isinstance(k, int):
            if k in (0x1B, 0x08, 0x09, 0x7F, 0x03):
                break
            if 0x20 <= k <= 0x7E:
                ch = chr(k).lower()
            else:
                continue
        elif isinstance(k, str) and k:
            ch = k.lower()
        else:
            continue

        if ch in ("`", "~"):
            break

        delta = 0
        if ch in ("w", ";"):
            delta = _VOL_STEP
        elif ch in ("s", "."):
            delta = -_VOL_STEP

        if delta != 0:
            new_v = volume + delta
            if new_v < 0:
                new_v = 0
            if new_v > 100:
                new_v = 100
            if new_v != volume:
                volume = new_v
                _apply_volume(volume)
                _show_settings(volume)
                # Play a brief tone so the user actually hears the
                # new level rather than just reading a number off
                # the screen.
                try:
                    _SPK.tone(_TONE_HZ, 80)
                except Exception:
                    pass

    _save_volume(volume)
    return volume


def _key_intent(k):
    """Returns ('play', ch) for a Morse-mappable key, ('exit',)
    for a dedicated exit key, or None for anything else.

    Exit policy: ````` (backtick, top-left of the keyboard,
    not a Morse letter so no conflict) is the primary, guaranteed
    exit. A handful of alternates are accepted defensively in case
    this Cardputer-Adv firmware reports ESC as something other than
    POSIX 0x1B: ints 0x1B / 0x08 / 0x09 / 0x7F / 0x03 (ESC, BS,
    TAB, DEL, Ctrl-C) and strings ``esc`` / ``exit`` / ``back`` /
    ``~``. Q is *not* an exit -- it's a Morse letter (``--.-``)
    and a kid should be able to hear it like every other letter."""
    if k is None:
        return None
    if isinstance(k, int):
        if k in (0x1B, 0x08, 0x09, 0x7F, 0x03):
            return ("exit",)
        if k in (0x0A, 0x0D):
            return ("toggle_mode",)
        if k == 0x20:
            return ("settings",)
        if 0x20 < k <= 0x7E:
            k = chr(k)
        else:
            return None
    if not isinstance(k, str) or not k:
        return None
    ch = k.lower()
    if ch in ("\n", "\r"):
        return ("toggle_mode",)
    if ch == " ":
        return ("settings",)
    if ch in ("`", "~", "esc", "exit", "back"):
        return ("exit",)
    if ch == ",":
        # Left-arrow-labeled key on the Cardputer-Adv keyboard.
        # Used by Quiz mode for 'replay the pattern'; no-op in
        # Play mode (the run loop just ignores it).
        return ("replay",)
    if ch in _MORSE:
        return ("play", ch)
    return None


def run():
    _set_chrome_font()
    _draw_chrome()
    _show_idle()
    kb = MatrixKeyboard()
    # Debounce the launcher keypress so it doesn't auto-play the
    # first character the moment the app opens.
    time.sleep_ms(400)
    try:
        volume = _load_volume()
        _apply_volume(volume)
        while True:
            kb.tick()
            intent = _key_intent(kb.get_key())
            if intent is None:
                time.sleep_ms(40)
                continue
            if intent[0] == "exit":
                return
            if intent[0] == "toggle_mode":
                # ENTER from Play mode -> jump into Quiz mode.
                if _quiz_loop(kb):
                    return
                _draw_chrome()
                _show_idle()
                continue
            if intent[0] == "settings":
                volume = _settings_loop(kb, volume)
                _draw_chrome()
                _show_idle()
                continue
            if intent[0] == "play":
                ch = intent[1]
                _show_letter(ch, _MORSE[ch])
                r = _play_morse(kb, _MORSE[ch])
                if r is not None:
                    if r[0] == "exit":
                        return
                    if r[0] == "toggle_mode":
                        # ENTER pressed mid-playback -- into Quiz.
                        if _quiz_loop(kb):
                            return
                        _draw_chrome()
                        _show_idle()
    finally:
        try:
            _SPK.stop()
        except Exception:
            pass
        try:
            _LCD.fillScreen(_BLACK)
        except Exception:
            pass
        time.sleep_ms(200)
        machine.reset()


# UIFlow App List invokes apps both as __main__ and via import; the
# empirical behavior is always-run, so just call run() bare.
run()
