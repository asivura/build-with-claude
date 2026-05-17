"""Whack-a-Mole for the Cardputer-Adv.

A 30-second reflex game on a 3x3 grid. A mole pops up at a random
hole; tap the matching key on the QWERTY keyboard to whack it before
it hides again. Each successful whack tightens the next mole's
visibility window, so the round ramps from 'too easy' to 'eep!'.

### Controls

    Q W E   top row
    A S D   middle row
    Z X C   bottom row
    ESC     exit during play
    R       restart (game-over screen)
    Q       exit   (game-over screen — mirrors snake / hello)

### Port notes vs. snake.py

- Same per-poll input model (kb.tick + kb.get_key at 40 ms) and the
  same DejaVu9 chrome so the apps feel like one suite.
- Same finally-reset exit (UIFlow 2.0 has no return-to-launcher API,
  same constraint snake / claude_buddy hit).
- Single self-contained file with no peer-module imports, so a user
  who only wants this game can install it with one upload.
- Q on this build is *both* a gameplay key (top-left mole) and the
  conventional exit key. We resolve by gating: during a round Q is
  a whack, ESC is the only exit; on the game-over screen Q exits
  to match the muscle memory the other apps establish.

### Layout (240x135)

    Header:      y=0..19    DARK band, ORANGE hairline at y=20
    Play area:   y=22..114  3x3 grid of 70x28 cells, 6/4 px gaps
    Hint strip:  y=117..134 DARK band, controls hint

Grid origin: x = 9 + col * 76, y = 23 + row * 32
Cell size:   70 x 28
"""

import random
import time

import M5
import machine
from hardware import MatrixKeyboard


# Palette inlined from ui_theme — matches snake / hello / claude_buddy.
_BLACK = 0x000000
_ORANGE = 0xCC785C
_CREAM = 0xF0EEE6
_DARK = 0x1F1F1F
_GRAY_MID = 0x777777
_GREEN = 0x55C878
_HOLE = 0x3A2A1F        # warm brown for empty holes; reads as 'dirt'

_LCD = M5.Lcd

_W = 240
_H = 135

_CELL_W = 70
_CELL_H = 28
_COL_X = (9, 85, 161)
_ROW_Y = (23, 55, 87)

_ROUND_MS = 30000

# Mole-visibility window shrinks linearly with score, floored so the
# late game stays just-barely-playable rather than impossible. Tuned
# on a 9-year-old: 1800 ms is a comfortable opening, 700 ms is the
# floor where it still feels playable, 20 ms per whack is a gentle
# ramp.
_MOLE_START_MS = 1800
_MOLE_MIN_MS = 700
_MOLE_STEP_MS = 20

# Brief flash on a successful hit. Long enough to register, short
# enough not to make the next mole feel delayed.
_HIT_FLASH_MS = 70


def _cell_xy(idx):
    return _COL_X[idx % 3], _ROW_Y[idx // 3]


def _set_font():
    try:
        _LCD.setFont(_LCD.FONTS.DejaVu9)
    except Exception as e:
        print("whack: setFont fallback:", e)


def _draw_chrome():
    _LCD.fillScreen(_BLACK)
    _LCD.fillRect(0, 0, _W, 20, _DARK)
    _LCD.fillRect(0, 20, _W, 1, _ORANGE)
    _LCD.setTextSize(1)
    _LCD.setTextColor(_ORANGE, _DARK)
    _LCD.drawString("Whack-a-Mole", 6, 5)
    _LCD.fillRect(0, _H - 18, _W, 18, _DARK)
    _LCD.setTextColor(_GRAY_MID, _DARK)
    hint = "QWE/ASD/ZXC  ESC quit"
    _LCD.drawString(hint, (_W - _LCD.textWidth(hint)) // 2, _H - 14)


def _update_header(score, seconds_left):
    # Right portion only — repainting the whole header would flash
    # the title text on every tick.
    _LCD.fillRect(100, 0, _W - 100, 20, _DARK)
    _LCD.setTextSize(1)
    _LCD.setTextColor(_CREAM, _DARK)
    text = "score {}  t {:>2}".format(score, seconds_left)
    x = _W - 6 - _LCD.textWidth(text)
    _LCD.drawString(text, x, 5)


def _draw_hole(idx):
    x, y = _cell_xy(idx)
    _LCD.fillRect(x, y, _CELL_W, _CELL_H, _HOLE)


def _draw_mole(idx):
    """Orange body, two black eyes, a small smile. The face is
    important: an unstyled orange rectangle reads as 'a square',
    not as 'something to whack'. Eyes + smile = obvious target."""
    x, y = _cell_xy(idx)
    _LCD.fillRect(x, y, _CELL_W, _CELL_H, _ORANGE)
    eye_y = y + 9
    _LCD.fillRect(x + 24, eye_y, 4, 4, _BLACK)
    _LCD.fillRect(x + 42, eye_y, 4, 4, _BLACK)
    _LCD.fillRect(x + 28, y + 19, 14, 2, _BLACK)


def _draw_hit_flash(idx):
    x, y = _cell_xy(idx)
    _LCD.fillRect(x, y, _CELL_W, _CELL_H, _GREEN)


def _draw_grid():
    for i in range(9):
        _draw_hole(i)


# Physical key-cap layout maps directly to the 3x3 grid.
_KEY_TO_CELL = {
    "q": 0, "w": 1, "e": 2,
    "a": 3, "s": 4, "d": 5,
    "z": 6, "x": 7, "c": 8,
}


def _key_intent(k):
    """Returns one of:
        ("cell", n)  - the player pressed a grid key (n in 0..8)
        ("exit",)    - ESC / Q-on-game-over
        ("restart",) - Enter or R on game-over
        None         - no recognized input

    During a round the caller treats Q as a cell intent (top-left)
    and only ESC as exit; on the game-over screen Q is exit. The
    intent decoder doesn't need to know which screen it's on — it
    just reports both possibilities and the caller picks."""
    if k is None:
        return None
    if isinstance(k, int):
        if k == 0x1B:
            return ("exit",)
        if k in (0x0A, 0x0D):
            return ("restart",)
        if 0x20 <= k <= 0x7E:
            k = chr(k)
        else:
            return None
    if not isinstance(k, str) or not k:
        return None
    ch = k.lower()
    if ch in _KEY_TO_CELL:
        return ("cell", _KEY_TO_CELL[ch])
    if ch == "r":
        return ("restart",)
    return None


def _mole_window(score):
    win = _MOLE_START_MS - score * _MOLE_STEP_MS
    if win < _MOLE_MIN_MS:
        win = _MOLE_MIN_MS
    return win


def _pick_cell(prev):
    """Avoid repeating the same hole twice in a row so the eye has
    to track movement. With nine holes the reject loop spins at
    most a couple of times."""
    while True:
        c = random.randint(0, 8)
        if c != prev:
            return c


def _game_over(kb, score):
    """Blocking end-of-round screen. Returns 'restart' or 'exit'."""
    # Clear the play area and repaint the hint strip for game-over controls.
    _LCD.fillRect(0, 21, _W, _H - 21 - 18, _BLACK)
    _LCD.fillRect(0, _H - 18, _W, 18, _DARK)
    _set_font()
    _LCD.setTextColor(_GRAY_MID, _DARK)
    go_hint = "R again   Q exit   ESC quit"
    _LCD.drawString(go_hint, (_W - _LCD.textWidth(go_hint)) // 2, _H - 14)
    _LCD.setTextSize(2)
    _LCD.setTextColor(_ORANGE, _BLACK)
    t = "Time up!"
    _LCD.drawString(t, (_W - _LCD.textWidth(t)) // 2, 32)
    _LCD.setTextSize(1)
    _LCD.setTextColor(_CREAM, _BLACK)
    s = "score: {}".format(score)
    _LCD.drawString(s, (_W - _LCD.textWidth(s)) // 2, 62)
    _LCD.setTextColor(_GRAY_MID, _BLACK)
    h = "R again   Q exit"
    _LCD.drawString(h, (_W - _LCD.textWidth(h)) // 2, 88)

    while True:
        kb.tick()
        i = _key_intent(kb.get_key())
        if i is not None:
            if i[0] == "restart":
                return "restart"
            if i[0] == "exit":
                return "exit"
            # Cell presses on the game-over screen are ignored — the
            # round is over, no holes are armed. Falling through to
            # the sleep means accidental mashing doesn't loop here.
        time.sleep_ms(40)


def _play_round(kb):
    """One 30-second round. Returns the final score, or None if the
    player exited early via ESC."""
    score = 0
    _draw_chrome()
    _draw_grid()
    _update_header(score, _ROUND_MS // 1000)

    t_start = time.ticks_ms()
    t_end = time.ticks_add(t_start, _ROUND_MS)
    cur_cell = _pick_cell(-1)
    cur_until = time.ticks_add(t_start, _mole_window(score))
    _draw_mole(cur_cell)

    last_secs = _ROUND_MS // 1000

    while True:
        now = time.ticks_ms()
        if time.ticks_diff(t_end, now) <= 0:
            # Final frame: hide the mole so the game-over overlay
            # doesn't draw over an orange cell.
            _draw_hole(cur_cell)
            return score

        secs_left = (time.ticks_diff(t_end, now) + 999) // 1000
        if secs_left != last_secs:
            last_secs = secs_left
            _update_header(score, secs_left)

        kb.tick()
        intent = _key_intent(kb.get_key())
        if intent is not None:
            if intent[0] == "exit":
                _draw_hole(cur_cell)
                return None
            if intent[0] == "cell":
                pressed = intent[1]
                if pressed == cur_cell:
                    _draw_hit_flash(cur_cell)
                    score += 1
                    _update_header(score, secs_left)
                    time.sleep_ms(_HIT_FLASH_MS)
                    _draw_hole(cur_cell)
                    cur_cell = _pick_cell(cur_cell)
                    cur_until = time.ticks_add(time.ticks_ms(),
                                               _mole_window(score))
                    _draw_mole(cur_cell)
                # Wrong-key presses are a no-op — no point penalizing
                # a 9-year-old for stretching across the keyboard.

        if time.ticks_diff(cur_until, time.ticks_ms()) <= 0:
            _draw_hole(cur_cell)
            cur_cell = _pick_cell(cur_cell)
            cur_until = time.ticks_add(time.ticks_ms(),
                                       _mole_window(score))
            _draw_mole(cur_cell)

        time.sleep_ms(40)


def run():
    _set_font()
    kb = MatrixKeyboard()
    # Debounce the launcher keypress so it doesn't whack the first
    # mole the moment the round begins. Same 400 ms window the other
    # apps use.
    time.sleep_ms(400)
    try:
        while True:
            score = _play_round(kb)
            if score is None:
                return
            if _game_over(kb, score) == "exit":
                return
    finally:
        # Mirror snake / hello / claude_buddy: clear, brief pause,
        # soft-reset back to the launcher.
        try:
            _LCD.fillScreen(_BLACK)
        except Exception as e:
            print("whack: clear warning:", e)
        time.sleep_ms(200)
        machine.reset()


# UIFlow's App List invokes apps both as __main__ and via import;
# the empirical behavior is always-run, so just call run() bare
# (same comment lives at the bottom of snake / hello).
run()
