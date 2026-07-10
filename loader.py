#!/usr/bin/env python3
"""Loader — docker-pull-style progress display for prod mode.

                 ⠸ rtsp-streamer [⣿⣿⣿⣿⣿⣿⣷⠀⠀⠀] 2/5 Preparing      11.5s
                   ✔ ids/first Ready                                  3.5s
                   ✔ ids/seconds Ready                                7.1s
                   ⠸ ids/third Preparing                              9.4s
                   - tfa/dalma_fhd Waiting

Usage:
    handle = loader.start(subtitle="v2.4.0")
    loader.set_items(["ids/first", "ids/seconds", ...])   # once, ordered
    loader.item_start("ids/first")                        # worker began it
    loader.item_done("ids/first")                         # worker finished it
    loader.stop(handle)

Design notes:
  - The display emulates `docker pull`: a header line with a braille bar
    and overall counts, then one line per video with its own state mark
    (`-` waiting, spinner while preparing, `✔` when ready) and a
    right-aligned elapsed time that ticks while running and freezes on
    completion — exactly the layer-list rhythm of a pull. Every video
    keeps its own line for the whole run (the list never collapses), so
    ✔ lines accumulate down the screen the way `Pull complete` lines do.
    Like docker pull, a list taller than the viewer's terminal can smear
    the in-place redraw — accepted trade-off for the full vertical list.
  - The whole block is indented INDENT columns. Under `docker compose up`
    ordinary log lines carry the `rtsp_streamer  | ` service prefix
    (17 columns) while the loader's in-place redraws bypass the prefixer
    and land at the left margin; the indent parks the block just right of
    that prefix column so it reads as part of the log flow instead of
    cutting into it. On a bare terminal it's a harmless offset.
  - The bar fills at *dot* resolution (8 braille dots per cell) and
    *moves* docker-pull style: each frame the displayed fill crawls
    toward the real done/total position, and while a slow build holds
    progress still it creeps forward — capped below the next video's
    boundary, so the bar never claims a video that isn't done. Monotonic.
  - The animation needs a TTY on stdout. In the image that is always
    true: the entrypoint runs streamer.py under its own pseudo-terminal
    (ptyrun.py, fixed at 80 columns — the floor virtually every real
    terminal meets, so frames never wrap on the viewer's side). Without
    any TTY (direct run with piped output, CI) streamer.py skips the
    animation and prints append-only snapshot lines via snapshot().
  - Redraw is in place (\\r + erase-to-EOL per line, cursor-up between
    frames); stop() draws one final frame and leaves the block in the
    scrollback — like the layer list a `docker pull` leaves behind — and
    a blank spacer line is printed first so the block sits visually
    apart from surrounding prefixed lines. If the terminal *narrows*
    mid-run the old block may have wrapped (cursor-up counts would lie),
    so it is abandoned in place and a fresh block starts below —
    resize-safe. Terminals narrower than the block fall back to a
    one-line spinner.
  - The cursor is hidden while running and always restored (finally:).
"""

import shutil
import sys
import threading
import time

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
CYAN = "\033[36m"
GREEN = "\033[32m"
HIDE_CURSOR = "\033[?25l"
SHOW_CURSOR = "\033[?25h"
EL = "\033[K"                 # erase to end of line

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
TICK = "✔"                    # docker compose's completion mark
WAIT_MARK = "-"
# one braille cell filling dot by dot: left column bottom-up, then right
CELL = "⡀⡄⡆⡇⣇⣧⣷⣿"          # 1..8 dots
FULL, TRACK = "⣿", "⠀"   # track = blank braille cell, same width as ⣿
BAR_W = 12                    # header bar cells -> 96 dot-steps
FPS = 12
INDENT = 17                   # parks the block right of compose's
                              # `rtsp_streamer  | ` prefix (17 columns)
MIN_FULL_COLS = INDENT + 44   # narrower than this -> one-line fallback
# animation rates, in dots (eighth-cells) per frame:
CATCHUP_FRAC = 0.25           # fraction of the gap to real progress closed per frame
CREEP = 0.10                  # idle forward creep
CREEP_CAP = 0.9               # creep may cover at most 90% of the current video's span

ST_WAIT, ST_RUN, ST_DONE = 0, 1, 2

_lock = threading.Lock()
_items = []                   # ordered display names
_state = {}                   # name -> [state, t_start, t_end]
_subtitle = ""


def set_items(names):
    """Declare the full ordered list of videos (all start as Waiting)."""
    with _lock:
        _items[:] = [str(n) for n in names]
        _state.clear()
        for n in _items:
            _state[n] = [ST_WAIT, None, None]


def item_start(name):
    with _lock:
        st = _state.get(str(name))
        if st and st[0] == ST_WAIT:
            st[0], st[1] = ST_RUN, time.monotonic()


def item_done(name):
    with _lock:
        st = _state.get(str(name))
        if st:
            now = time.monotonic()
            if st[1] is None:
                st[1] = now
            st[0], st[2] = ST_DONE, now


def _elapsed(dt):
    """docker-compose style: 3.5s, 11.5s, 1m06s."""
    if dt < 60:
        return "{:.1f}s".format(dt)
    return "{}m{:02d}s".format(int(dt) // 60, int(dt) % 60)


def _bar_at(eighths):
    """Braille bar filled to `eighths` dots (of BAR_W * 8 total)."""
    e = max(0, min(BAR_W * 8, int(eighths)))
    full, rem = divmod(e, 8)
    out = FULL * full
    if rem:
        out += CELL[rem - 1]
    return out, TRACK * (BAR_W - len(out))


def _advance(shown, done, total):
    """One animation step for the displayed fill (in dots).

    Crawl quickly toward the real done/total position; once there, creep
    slowly forward through the in-progress video's span (capped at
    CREEP_CAP of it, so the bar never claims a video that isn't done).
    Monotonic: the fill only ever moves forward."""
    if total <= 0:
        return shown
    span = BAR_W * 8 / total                 # dots per video
    target = min(BAR_W * 8, span * done)
    if shown < target:
        return min(target, shown + max(1.0, (target - shown) * CATCHUP_FRAC))
    if done < total:
        return min(target + span * CREEP_CAP, shown + CREEP)
    return shown


def snapshot(done, total):
    """The same bar as one plain append-only line — for non-TTY output
    (piped logs, CI) where in-place redraws would scramble.
    No ANSI codes: safe in log files and captures."""
    fill, track = _bar_at(0 if total <= 0 else BAR_W * 8 * done // total)
    return "[{}{}] {}/{}".format(fill, track, done, total)


def _row(left, elapsed, w, mark=None, mcolor=None, dim=False):
    """One display line: `left` truncated to fit, `elapsed` right-aligned
    at the terminal edge (docker-compose style). Colors are injected after
    the plain text is measured, so padding stays exact."""
    room = w - (len(elapsed) + 2) if elapsed else w
    if len(left) > room:
        left = left[:max(1, room - 1)] + "…"
    pad = " " * (room - len(left))
    if mark and mcolor:
        i = left.find(mark)
        if i >= 0:
            left = left[:i] + mcolor + mark + RESET + (DIM if dim else "") \
                + left[i + len(mark):] + (RESET if dim else "")
    elif dim:
        left = DIM + left + RESET
    tail = "{}  {}{}{}".format(pad, DIM, elapsed, RESET) if elapsed else pad
    return left + tail


def _frame_lines(tick, t0, cols):
    with _lock:
        items = list(_items)
        state = {n: list(_state[n]) for n in items}
        subtitle = _subtitle
    now = time.monotonic()
    spin = SPINNER[tick % len(SPINNER)]
    w = cols - 1
    ind = " " * INDENT

    done = sum(1 for n in items if state[n][0] == ST_DONE)
    total = len(items)

    # header: spinner, name, bar, counts, verb, total elapsed on the right
    finished = total > 0 and done >= total
    fill, track = _bar_at(BAR_W * 8 if finished else _frame_lines.shown)
    head = "{}{} rtsp-streamer {} [{}{}] {}/{} {}".format(
        ind, TICK if finished else spin, subtitle, fill, track,
        done, total, "Ready" if finished else "Preparing")
    lines = [_row(head, _elapsed(now - t0), w,
                  mark=TICK if finished else spin,
                  mcolor=GREEN if finished else CYAN)]

    # item rows, docker-pull style: every video keeps its own line for the
    # whole run — ✔ lines pile up vertically like `Pull complete` lines
    for n in items:
        st, ts, te = state[n]
        if st == ST_DONE:
            lines.append(_row("{}  {} {} Ready".format(ind, TICK, n),
                              _elapsed(te - ts), w, mark=TICK, mcolor=GREEN))
        elif st == ST_RUN:
            lines.append(_row("{}  {} {} Preparing".format(ind, spin, n),
                              _elapsed(now - ts), w, mark=spin, mcolor=CYAN))
        else:
            lines.append(_row("{}  {} {} Waiting".format(ind, WAIT_MARK, n),
                              "", w, dim=True))
    return lines


_frame_lines.shown = 0.0      # displayed bar fill in dots (animated)


def _run(ev):
    out = sys.stdout
    t0 = time.monotonic()
    tick = 0
    drew = 0                              # block lines currently on screen
    last_cols = None
    _frame_lines.shown = 0.0
    # a blank spacer line first, so the animated block sits visually apart
    # from surrounding (compose-prefixed) log lines instead of touching them
    out.write("\n" + HIDE_CURSOR)
    try:
        while not ev.is_set():
            with _lock:
                total = len(_items)
                done = sum(1 for n in _items if _state[n][0] == ST_DONE)
            _frame_lines.shown = _advance(_frame_lines.shown, done, total)
            cols = shutil.get_terminal_size(fallback=(80, 24)).columns
            if drew and last_cols is not None and cols < last_cols:
                # terminal narrowed: the old block's lines may have wrapped,
                # so cursor-up counts no longer match physical lines — never
                # climb over them. Abandon the old block (it stays as one
                # stale frame) and draw a fresh one below.
                out.write("\r\033[J")
                drew = 0
            last_cols = cols
            if cols < MIN_FULL_COLS:      # one-line fallback
                if drew > 1:              # leaving block mode: erase block
                    out.write("\033[{}A\r\033[J".format(drew - 1))
                text = ("preparing {}/{}".format(done, total)
                        if total > 0 else "preparing")
                line = "\r{}{}{} {}".format(
                    CYAN, SPINNER[tick % len(SPINNER)], RESET, text)
                out.write(line[:cols] + EL)
                drew = 1
            else:
                lines = _frame_lines(tick, t0, cols)
                prev = drew - 1 if drew > 1 else 0
                if prev:                  # climb back to the block's top
                    out.write("\033[{}A".format(prev))
                elif drew == 1:           # leaving one-line mode
                    out.write("\r" + EL)
                for l in lines:
                    out.write("\r" + EL + l + "\n")
                if prev > len(lines):     # block shrank (set_items changed)
                    out.write("\033[J")
                drew = len(lines) + 1     # sentinel: >1 means block mode
            out.flush()
            tick += 1
            ev.wait(1.0 / FPS)
    finally:
        # docker-pull style: draw one final frame and leave the block in
        # the scrollback — the ✔ list stays as the record of what was
        # prepared, like the layer list a pull leaves behind. The narrow
        # one-line fallback finalizes as a single plain line; an empty
        # item list just erases (nothing worth keeping).
        try:
            with _lock:
                total = len(_items)
                done = sum(1 for n in _items if _state[n][0] == ST_DONE)
            cols = shutil.get_terminal_size(fallback=(80, 24)).columns
            if drew and last_cols is not None and cols < last_cols:
                # narrowed since the last frame: never climb over possibly
                # wrapped lines — abandon the old block, draw fresh below
                out.write("\r\033[J")
                drew = 0
            if total == 0:
                if drew > 1:
                    out.write("\033[{}A\r\033[J".format(drew - 1))
                elif drew == 1:
                    out.write("\r" + EL)
            elif cols < MIN_FULL_COLS:
                if drew > 1:
                    out.write("\033[{}A\r\033[J".format(drew - 1))
                mark = TICK if done >= total else WAIT_MARK
                out.write("\r" + EL + "{} prepared {}/{}\n".format(
                    mark, done, total))
            else:
                lines = _frame_lines(tick, t0, cols)
                prev = drew - 1 if drew > 1 else 0
                if prev:
                    out.write("\033[{}A".format(prev))
                elif drew == 1:
                    out.write("\r" + EL)
                for l in lines:
                    out.write("\r" + EL + l + "\n")
                if prev > len(lines):
                    out.write("\033[J")
        finally:
            out.write(RESET + SHOW_CURSOR)
            out.flush()


def start(subtitle=""):
    """Start the loader; returns a handle for stop(). Safe to call only when
    stdout is a real terminal (streamer.py checks isatty first)."""
    global _subtitle
    with _lock:
        _subtitle = str(subtitle)
    ev = threading.Event()
    th = threading.Thread(target=_run, args=(ev,), daemon=True)
    th.start()
    return (ev, th)


def stop(handle):
    ev, th = handle
    ev.set()
    th.join(timeout=2.0)
