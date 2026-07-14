#!/usr/bin/env python3
"""Loader — docker-pull-style progress display for prod mode.

                   ✔ ids/first Ready                                  3.5s
                   ✔ mini/1 … mini/65 Ready (65 clips)               41.2s
                 ⠸ rtsp-streamer v2.5.0 [⣿⣿⣿⣿⣿⣷⠀⠀⠀] 66/68 Preparing 51.4s
                   ⠸ tfa/dalma_fhd Preparing                          9.4s
                   - 1 waiting

Usage:
    handle = loader.start(subtitle="v2.5.0")
    loader.set_items([("ids/first", None), ("mini/1", "mini"), ...])
    loader.item_start("mini/1")                           # worker began it
    loader.item_done("mini/1")                            # worker finished it
    loader.stop(handle)

Design notes:
  - The display emulates `docker pull`: finished videos print ONCE as plain
    scrolling ✔ lines (they land in the scrollback exactly like `Pull
    complete` lines), while the small animated block below holds only the
    header bar, the rows currently in flight (at most the worker-pool
    width) and a dim `- N waiting` count. The block is therefore a handful
    of lines tall no matter how many videos there are — it always fits the
    terminal, the cursor-up redraw can never clamp at the screen top and
    smear, and it never climbs over earlier (compose-prefixed) log lines.
  - Grouping: set_items entries may carry a group key. All members of a
    group share ONE row (`⠸ mini 12/65 Preparing`) and ONE final line
    (`✔ mini/1 … mini/65 Ready (65 clips)`) — numbered mini mounts differ
    only by index, so first…last says everything and the per-index map
    stays in workspace/<folder>/manifest.txt.
  - The whole block is indented INDENT columns. Under `docker compose up`
    ordinary log lines carry the `rtsp_streamer  | ` service prefix
    (17 columns) while the loader's writes bypass the prefixer and land at
    the left margin; the indent parks the block just right of that prefix
    column so it reads as part of the log flow instead of cutting into it.
    On a bare terminal it's a harmless offset.
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
    frames). Newly finished units are written first — they overwrite the
    top of the old block and push the fresh block down, which is exactly
    how they end up scrolled "above" it. stop() paints one final frame,
    leaving the ✔ list and the closing header line in the scrollback. If
    the terminal *narrows* mid-run the old block may have wrapped
    (cursor-up counts would lie), so it is abandoned in place and a fresh
    block starts below — resize-safe. Terminals narrower than the block
    fall back to a one-line spinner.
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
_units = []                   # ordered display units: {label, members, group}
_state = {}                   # member name -> [state, t_start, t_end]
_emitted = set()              # unit indexes already printed as final ✔ lines
_subtitle = ""


def set_items(entries):
    """Declare the full ordered list of videos (all start as Waiting).
    Each entry is a name or a (name, group) pair; entries sharing a group
    collapse into one display row and one final ✔ line (first … last)."""
    with _lock:
        _units[:] = []
        _state.clear()
        _emitted.clear()
        by_group = {}
        for e in entries:
            name, group = (e, None) if isinstance(e, str) else (e[0], e[1])
            name = str(name)
            _state[name] = [ST_WAIT, None, None]
            if group is None:
                _units.append({"label": name, "members": [name], "group": False})
            else:
                u = by_group.get(group)
                if u is None:
                    u = {"label": str(group), "members": [], "group": True}
                    by_group[group] = u
                    _units.append(u)
                u["members"].append(name)


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


def _unit_view(u, state):
    """(state, done, total, first_start, last_end) for one display unit."""
    sts = [state[n] for n in u["members"]]
    done = sum(1 for s in sts if s[0] == ST_DONE)
    started = [s[1] for s in sts if s[1] is not None]
    ended = [s[2] for s in sts if s[2] is not None]
    st = (ST_DONE if done == len(sts)
          else ST_RUN if started else ST_WAIT)
    return (st, done, len(sts),
            min(started) if started else None,
            max(ended) if ended else None)


def _done_row(u, view, w, ind):
    """The permanent ✔ line a finished unit leaves in the scrollback."""
    _, _, total, ts, te = view
    if u["group"] and total > 1:
        left = "{}  {} {} … {} Ready ({} clips)".format(
            ind, TICK, u["members"][0], u["members"][-1], total)
    else:
        left = "{}  {} {} Ready".format(ind, TICK, u["members"][0])
    return _row(left, _elapsed(te - ts), w, mark=TICK, mcolor=GREEN)


def _frame(tick, t0, cols):
    """One frame -> (perm_lines, block_lines).

    perm  = units that finished since the last frame, printed once and
            left to scroll away (docker pull's `Pull complete` lines).
    block = the small in-place animated part: header bar, one row per
            in-flight unit (bounded by the worker-pool width, so a
            handful at most) and a dim waiting count."""
    with _lock:
        units = [dict(u, members=list(u["members"])) for u in _units]
        state = {n: list(_state[n]) for n in _state}
        subtitle = _subtitle
        emitted = set(_emitted)
    now = time.monotonic()
    spin = SPINNER[tick % len(SPINNER)]
    w = cols - 1
    ind = " " * INDENT

    views = [_unit_view(u, state) for u in units]
    done = sum(v[1] for v in views)
    total = sum(v[2] for v in views)

    perm, newly = [], []
    for i, (u, v) in enumerate(zip(units, views)):
        if v[0] == ST_DONE and i not in emitted:
            perm.append(_done_row(u, v, w, ind))
            newly.append(i)
    if newly:
        with _lock:
            _emitted.update(newly)

    # header: spinner, name, bar, counts, verb, total elapsed on the right
    finished = total > 0 and done >= total
    fill, track = _bar_at(BAR_W * 8 if finished else _frame.shown)
    head = "{}{} rtsp-streamer {} [{}{}] {}/{} {}".format(
        ind, TICK if finished else spin, subtitle, fill, track,
        done, total, "Ready" if finished else "Preparing")
    block = [_row(head, _elapsed(now - t0), w,
                  mark=TICK if finished else spin,
                  mcolor=GREEN if finished else CYAN)]

    waiting = 0
    for u, v in zip(units, views):
        st, d, t, ts, _ = v
        if st == ST_RUN:
            label = ("{} {}/{}".format(u["label"], d, t) if u["group"]
                     else u["label"])
            block.append(_row("{}  {} {} Preparing".format(ind, spin, label),
                              _elapsed(now - ts), w, mark=spin, mcolor=CYAN))
        elif st == ST_WAIT:
            waiting += t
    if waiting:
        block.append(_row("{}  {} {} waiting".format(ind, WAIT_MARK, waiting),
                          "", w, dim=True))
    return perm, block


_frame.shown = 0.0            # displayed bar fill in dots (animated)


def _paint(out, tick, t0, drew, last_cols, final=False):
    """Draw one frame in place; returns the new (drew, last_cols).
    `drew` > 1 means a block of drew-1 lines is on screen; 1 means the
    narrow one-line spinner; 0 means nothing to climb over."""
    with _lock:
        total = len(_state)
        done = sum(1 for n in _state if _state[n][0] == ST_DONE)
    _frame.shown = _advance(_frame.shown, done, total)
    cols = shutil.get_terminal_size(fallback=(80, 24)).columns
    buf = []
    if drew and last_cols is not None and cols < last_cols:
        # terminal narrowed: the old block's lines may have wrapped, so
        # cursor-up counts no longer match physical lines — never climb
        # over them. Abandon the old block (it stays as one stale frame)
        # and draw a fresh one below.
        buf.append("\r\033[J")
        drew = 0
    last_cols = cols

    if total == 0:
        if final:                 # nothing worth keeping: just erase
            if drew > 1:
                buf.append("\033[{}A\r\033[J".format(drew - 1))
            elif drew == 1:
                buf.append("\r" + EL)
            drew = 0
        else:
            text = "\r{}{}{} preparing".format(
                CYAN, SPINNER[tick % len(SPINNER)], RESET)
            buf.append(text[:cols] + EL)
            drew = 1
    elif cols < MIN_FULL_COLS:    # one-line fallback
        perm, _ = _frame(tick, t0, cols)   # still flush finished units
        if drew > 1:              # leaving block mode: erase block
            buf.append("\033[{}A\r\033[J".format(drew - 1))
        elif drew == 1:
            buf.append("\r" + EL)
        for l in perm:
            buf.append("\r" + EL + l + "\n")
        if final:
            mark = TICK if done >= total else WAIT_MARK
            buf.append("\r" + EL + "{} prepared {}/{}\n".format(
                mark, done, total))
            drew = 0
        else:
            line = "\r{}{}{} preparing {}/{}".format(
                CYAN, SPINNER[tick % len(SPINNER)], RESET, done, total)
            buf.append(line[:cols] + EL)
            drew = 1
    else:
        perm, block = _frame(tick, t0, cols)
        prev = drew - 1 if drew > 1 else 0
        if prev:                  # climb back to the block's top
            buf.append("\033[{}A".format(prev))
        elif drew == 1:           # leaving one-line mode
            buf.append("\r" + EL)
        # finished lines first: they overwrite the old block's top and the
        # fresh block reprints below — that is how they scroll "above" it
        for l in perm + block:
            buf.append("\r" + EL + l + "\n")
        if prev > len(perm) + len(block):   # block shrank: erase leftovers
            buf.append("\033[J")
        # final frame stays put in the scrollback (docker-pull style): the
        # ✔ list plus the closing header line are the record of the run
        drew = 0 if final else len(block) + 1
    out.write("".join(buf))
    out.flush()
    return drew, last_cols


def _run(ev):
    out = sys.stdout
    t0 = time.monotonic()
    tick = 0
    drew = 0                              # see _paint
    last_cols = None
    _frame.shown = 0.0
    # a blank spacer line first, so the animated block sits visually apart
    # from surrounding (compose-prefixed) log lines instead of touching them
    out.write("\n" + HIDE_CURSOR)
    try:
        while not ev.is_set():
            drew, last_cols = _paint(out, tick, t0, drew, last_cols)
            tick += 1
            ev.wait(1.0 / FPS)
    finally:
        try:
            _paint(out, tick, t0, drew, last_cols, final=True)
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
