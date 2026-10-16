#!/usr/bin/env python3
"""Loader — docker-pull-style progress display for prod mode.

    rtsp_streamer  | ⠸ rtsp-streamer [⣿⡀⣦⣀⠀] 21/70 Preparing          51.4s
    rtsp_streamer  |   ✔ ids Ready (2 videos) · cached                 0.4s
    rtsp_streamer  |   ⠸ knife Re-encoding · broken chain              9.4s
    rtsp_streamer  |   ⠸ tfa 3/5 Checking dalma_fhd                    6.2s
    rtsp_streamer  |   ⠸ mini 16/61 Preparing                         31.0s
    rtsp_streamer  |   - probe Waiting

Usage:
    handle = loader.start()
    loader.set_items([("ids/first", None), ("mini/1", "mini"), ...])
    loader.item_start("mini/1")                           # worker began it
    loader.item_step("mini/1", "Re-encoding", "B-frames") # what it is doing
    loader.item_progress("mini/1", 0.5)                   # 0..1 of this video
    loader.item_note("mini/1", "reencoded", "B-frames")   # outcome, for Ready
    loader.item_done("mini/1")                            # worker finished it
    loader.stop(handle)

Design notes:
  - The display is structured exactly like `docker compose pull`: the
    summary header is the block's FIRST line and never moves, and below it
    every folder owns one fixed row that changes state in place —
    `- Waiting` → `⠸ k/n Preparing` → `✔ Ready` — just as pull's layer
    rows go Waiting → Downloading → Pull complete. Nothing is ever
    printed above the header, so it cannot sink as folders finish. The
    block is 1 + #folders lines tall (folders are few even when videos
    number in the hundreds), so it fits the terminal and the cursor-up
    redraw can never clamp at the screen top and smear. Should folders
    still outnumber the screen, trailing Waiting rows collapse into one
    `- N folders Waiting` row.
  - The header bar is docker pull's, not a left-to-right meter: one
    braille cell per folder (2 dot-columns), and each folder's own cell
    RISES bottom-up (⠀⡀⣀⣄⣤⣦⣶⣷⣿) with that folder's done/total —
    `[⣿⣶⡀⠀]` reads folder 1 done, folder 2 ~70%, folder 3 started,
    folder 4 waiting. A cell is real work done: finished videos plus the
    encoded fraction of the ones in flight (item_progress, fed from
    ffmpeg's -progress out_time against the output duration) — a 30 min
    video with 15 min encoded fills half its share. A video that reports
    no progress (stream-copies, probes) creeps up a little, capped below
    its share, so a cell never claims work that isn't done. Monotonic per
    folder. Cells are cyan while their folder prepares and turn green once
    it is Ready — the same green as the ✔ marks. Colour sits on glyphs and
    cells only: brackets, words and counts stay plain, timings and
    secondary notes dim.
  - Compose-prefix safety: under `docker compose up` every
    `\\n`-terminated chunk gets the `rtsp_streamer  | ` prefix from
    compose itself. Each block line therefore ends in a real newline
    (so compose prefixes it), and redraws never touch the prefix
    columns: instead of `\\r`, the cursor jumps to column INDENT+1 and
    erases only rightward. Compose's genuine prefix survives every
    frame; nothing fake is painted. On a bare terminal the same
    columns are simply left as a margin.
  - Grouping: set_items entries may carry a group key (the numbered mini
    mounts). A grouped folder row counts clips but never names the
    current one (`⠸ mini 16/61 Preparing`), and finishes as
    `✔ mini Ready (61 clips)` — the per-index map stays in
    workspace/<folder>/manifest.txt. Ungrouped folder rows name the video
    being worked on (`⠸ tfa 2/5 Preparing dalma_fhd`).
  - The animation needs a TTY on stdout. In the image that is always
    true: the entrypoint runs streamer.py under its own pseudo-terminal
    (ptyrun.py, fixed at 80 columns — the floor virtually every real
    terminal meets, so frames never wrap on the viewer's side). Without
    any TTY (direct run with piped output, CI) streamer.py skips the
    animation and prints append-only snapshot lines via snapshot().
  - Redraw is in place (cursor-up between frames, column-addressed
    erase per line). stop() paints one final all-✔ frame that stays put
    in the scrollback — the record of the run, docker-pull style. If
    the terminal *narrows* mid-run the old block may have wrapped
    (cursor-up counts would lie), so it is abandoned in place and a
    fresh block starts below — resize-safe. Terminals narrower than the
    block fall back to a one-line spinner.
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
# one braille cell rising bottom-up, 0..8 dots — docker pull's layer cell
RISE = "⠀⡀⣀⣄⣤⣦⣶⣷⣿"
# left-to-right fill (kept for the non-TTY snapshot line only)
CELL = "⡀⡄⡆⡇⣇⣧⣷⣿"          # 1..8 dots
FULL, TRACK = "⣿", "⠀"   # track = blank braille cell, same width as ⣿
BAR_W = 12                    # snapshot() bar cells
FPS = 12
INDENT = 17                   # compose prints `rtsp_streamer  | ` (17 cols);
                              # the loader never writes left of this column,
                              # so that real prefix survives every redraw
GO_COL = "\033[{}G".format(INDENT + 1)   # jump straight past the prefix
MIN_FULL_COLS = INDENT + 44   # narrower than this -> one-line fallback
# animation rates, in dots (eighth-cells) per frame:
CATCHUP_FRAC = 0.25           # fraction of the gap to real progress closed per frame
CREEP = 0.10                  # idle forward creep
CREEP_CAP = 0.9               # creep may cover at most 90% of the current video's span

ST_WAIT, ST_RUN, ST_DONE = 0, 1, 2

_lock = threading.Lock()
_units = []                   # ordered folder rows: {label, members, group}
_state = {}                   # member name -> [state, t_start, t_end]
_prog = {}                    # member name -> 0..1 encoded fraction (absent = unknown)
_step = {}                    # member name -> (label, detail)
_notes = {}                   # member name -> (kind, detail): cached|reencoded|asis


def set_items(entries):
    """Declare the full ordered list of videos (all start as Waiting).
    Each entry is a name or a (name, group) pair. Videos sharing a folder
    (the part before the first '/', or the group key) share one display
    row; a group marks numbered clips, so the row counts them instead of
    naming the current one."""
    with _lock:
        _units[:] = []
        _state.clear()
        _prog.clear()
        _step.clear()
        _notes.clear()
        _frame.fills = {}
        by_folder = {}
        for e in entries:
            name, group = (e, None) if isinstance(e, str) else (e[0], e[1])
            name = str(name)
            _state[name] = [ST_WAIT, None, None]
            folder = str(group) if group is not None else name.split("/", 1)[0]
            u = by_folder.get(folder)
            if u is None:
                u = {"label": folder, "members": [], "group": group is not None}
                by_folder[folder] = u
                _units.append(u)
            u["members"].append(name)


def item_start(name):
    with _lock:
        st = _state.get(str(name))
        if st and st[0] == ST_WAIT:
            st[0], st[1] = ST_RUN, time.monotonic()


def item_progress(name, frac):
    """Fraction (0..1) of this video's current encode that is done. Kept
    monotonic and short of 1 — only item_done completes a video, since one
    video may run several ffmpeg passes."""
    with _lock:
        name = str(name)
        if name in _state:
            frac = max(0.0, min(0.97, float(frac)))
            _prog[name] = max(_prog.get(name, 0.0), frac)


def item_step(name, label, detail=""):
    """What the video is doing now ("Checking", "Re-encoding", ...), shown
    in its folder row instead of the generic "Preparing"."""
    with _lock:
        if str(name) in _state:
            _step[str(name)] = (label, detail or "")


def item_note(name, kind, detail=""):
    """The video's outcome for its folder's Ready row: "cached",
    "reencoded" (detail = why) or "asis" (served with realism warnings)."""
    with _lock:
        if str(name) in _state:
            _notes[str(name)] = (kind, detail or "")


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
    """Left-to-right braille bar for snapshot(): filled to `eighths` dots
    (of BAR_W * 8 total)."""
    e = max(0, min(BAR_W * 8, int(eighths)))
    full, rem = divmod(e, 8)
    out = FULL * full
    if rem:
        out += CELL[rem - 1]
    return out, TRACK * (BAR_W - len(out))


def _advance(shown, done, total, partial=0.0, unknown=None):
    """One animation step for a displayed fill (in dots of an 8-dot cell).

    target = the folder's real progress: `done` finished videos plus
    `partial` (the summed encoded fractions of videos in flight). The fill
    crawls quickly toward it; once there it may creep slowly through the
    span of `unknown` in-flight videos that report no progress (capped at
    CREEP_CAP of their share), so the fill never claims unfinished work.
    unknown=None = all of the remainder is unknown (one video in flight).
    Monotonic: the fill only ever moves forward."""
    if total <= 0:
        return shown
    span = 8.0 / total                       # dots per video (8-dot cell)
    target = min(8.0, span * (done + partial))
    if shown < target:
        return min(target, shown + max(0.5, (target - shown) * CATCHUP_FRAC))
    if unknown is None:
        unknown = 1 if done < total else 0
    if unknown:
        cap = min(8.0, target + span * CREEP_CAP * unknown)
        return min(cap, shown + CREEP) if shown < cap else shown
    return shown


def snapshot(done, total):
    """The overall progress as one plain append-only line — for non-TTY
    output (piped logs, CI) where in-place redraws would scramble.
    No ANSI codes: safe in log files and captures."""
    fill, track = _bar_at(0 if total <= 0 else BAR_W * 8 * done // total)
    return "[{}{}] {}/{}".format(fill, track, done, total)


def _row(left, elapsed, w, mark=None, mcolor=None, dim=False, dim_from=None):
    """One display line: `left` truncated to fit, `elapsed` right-aligned
    at the terminal edge (docker-compose style). The INDENT prefix columns
    are never part of the text — the painter positions the cursor past
    them. Colors are injected after the plain text is measured, so
    padding stays exact: `mark` in mcolor, everything from `dim_from` on
    (a secondary note) or the whole line (dim=True) dimmed."""
    w -= INDENT
    room = w - (len(elapsed) + 2) if elapsed else w
    if len(left) > room:
        left = left[:max(1, room - 1)] + "…"
    pad = " " * (room - len(left))
    if dim_from is not None and not dim and dim_from < len(left):
        left = left[:dim_from] + DIM + left[dim_from:] + RESET
    if mark and mcolor:
        i = left.find(mark)
        if i >= 0:
            left = left[:i] + mcolor + mark + RESET + (DIM if dim else "") \
                + left[i + len(mark):] + (RESET if dim else "")
    elif dim:
        left = DIM + left + RESET
    tail = "{}  {}{}{}".format(pad, DIM, elapsed, RESET) if elapsed else pad
    return left + tail


def _unit_view(u, state, prog=None, steps=None):
    """(state, done, total, first_start, last_end, current, partial,
    unknown, step) for one folder row; `current` is the most recently
    started unfinished video's stem (what the row is visibly working on),
    `partial` the summed progress of in-flight videos, `unknown` how many
    of them report none, `step` the current video's (label, detail)."""
    prog = prog or {}
    steps = steps or {}
    sts = [(n, state[n]) for n in u["members"]]
    done = sum(1 for _, s in sts if s[0] == ST_DONE)
    started = [s[1] for _, s in sts if s[1] is not None]
    ended = [s[2] for _, s in sts if s[2] is not None]
    st = (ST_DONE if done == len(sts)
          else ST_RUN if started else ST_WAIT)
    running = [(s[1], n) for n, s in sts if s[0] == ST_RUN]
    partial = sum(prog.get(n, 0.0) for _, n in running)
    unknown = sum(1 for _, n in running if n not in prog)
    cur = max(running)[1] if running else None
    current = cur.split("/", 1)[-1] if cur else ""
    step = steps.get(cur, ("Preparing", "")) if cur else ("Preparing", "")
    return (st, done, len(sts),
            min(started) if started else None,
            max(ended) if ended else None, current, partial, unknown, step)


def _ready_note(u, notes):
    """The dim tail of a folder's Ready row, from its videos' outcomes:
    "cached" when nothing was built, else re-encodes / as-is counts."""
    got = [notes.get(n) for n in u["members"]]
    kinds = [g[0] for g in got if g]
    if kinds and len(kinds) == len(got) and set(kinds) == {"cached"}:
        return "cached"
    parts = []
    re_ = [g[1] for g in got if g and g[0] == "reencoded"]
    if re_:
        parts.append("re-encoded: {}".format(re_[0])
                     if len(got) == 1 and re_[0] else
                     "{} re-encoded".format(len(re_)))
    asis = sum(1 for k in kinds if k == "asis")
    if asis:
        parts.append("{} as-is".format(asis) if len(got) > 1 else "as-is")
    return ", ".join(parts)


def _what(u, total):
    if u["group"]:
        return "{} clips".format(total)
    if total == 1 and u["members"][0].endswith("/chain"):
        return "chain"
    return "{} video{}".format(total, "" if total == 1 else "s")


def _folder_row(u, view, spin, now, w, notes=None):
    """One folder's fixed row in its current state."""
    st, done, total, ts, te, current, _p, _u, step = view
    if st == ST_DONE:
        left = "  {} {} Ready ({})".format(TICK, u["label"], _what(u, total))
        note = _ready_note(u, notes or {})
        cut = None
        if note:
            cut = len(left) + 1
            left += " · " + note
        return _row(left, _elapsed(te - ts), w, mark=TICK, mcolor=GREEN,
                    dim_from=cut)
    if st == ST_RUN:
        label, detail = step
        cut = None
        if total > 1:
            left = "  {} {} {}/{} {}".format(spin, u["label"], done, total, label)
            if current and not u["group"]:
                left += " " + current
        else:
            left = "  {} {} {}".format(spin, u["label"], label)
            if detail:
                cut = len(left) + 1
                left += " · " + detail
        return _row(left, _elapsed(now - ts), w, mark=spin, mcolor=CYAN,
                    dim_from=cut)
    return _row("  {} {} Waiting".format(WAIT_MARK, u["label"]),
                "", w, dim=True)


def _cells(units, views):
    """The header bar: one braille cell per folder, rising bottom-up with
    that folder's own progress — docker pull's per-layer cells. Returns
    (plain, colored): plain is measured for layout, colored is painted."""
    out, painted = [], []
    for u, v in zip(units, views):
        st, done, total, partial, unknown = v[0], v[1], v[2], v[6], v[7]
        if st == ST_DONE:
            fill = 8.0
        else:
            shown = _frame.fills.get(u["label"], 0.0)
            fill = (_advance(shown, done, total, partial, unknown)
                    if st == ST_RUN else 0.0)
        _frame.fills[u["label"]] = fill
        out.append(RISE[int(fill)])
        if st != ST_WAIT:
            painted.append((GREEN if st == ST_DONE else CYAN)
                           + RISE[int(fill)] + RESET)
        else:
            painted.append(RISE[int(fill)])
    return "".join(out), "".join(painted)


def _frame(tick, t0, cols, rows=24):
    """One frame -> the block's lines, drawn in place every time:
    summary header first (always the top line), then one fixed row per
    folder — docker pull's layout. If the terminal is too short for every
    folder, trailing Waiting rows collapse into one count row."""
    with _lock:
        units = [dict(u, members=list(u["members"])) for u in _units]
        state = {n: list(_state[n]) for n in _state}
        prog, steps, notes = dict(_prog), dict(_step), dict(_notes)
    now = time.monotonic()
    spin = SPINNER[tick % len(SPINNER)]
    w = cols - 1

    views = [_unit_view(u, state, prog, steps) for u in units]
    done = sum(v[1] for v in views)
    total = sum(v[2] for v in views)

    # header: spinner, name, per-folder cells, counts, verb, total elapsed
    finished = total > 0 and done >= total
    cells, painted = _cells(units, views)
    head = "{} rtsp-streamer [{}] {}/{} {}".format(
        TICK if finished else spin, cells,
        done, total, "Ready" if finished else "Preparing")
    head = _row(head, _elapsed(now - t0), w,
                mark=TICK if finished else spin,
                mcolor=GREEN if finished else CYAN)
    # paint after layout (_row measures plain text): bold name, plain-dim
    # brackets around coloured cells; a header truncated mid-bar simply
    # stays uncoloured
    head = head.replace(" rtsp-streamer [{}]".format(cells),
                        " {}rtsp-streamer{} {}[{}{}{}]{}".format(
                            BOLD, RESET, DIM, RESET, painted, DIM, RESET), 1)
    block = [head]

    # fold surplus Waiting rows so the block always fits the screen
    fit = max(2, rows - 2)                     # lines available to the block
    hidden = 0
    if len(units) + 1 > fit:
        keep = max(0, fit - 2)                 # header + collapse row
        shown, kept = set(), 0
        for i, v in enumerate(views):          # active rows have priority
            if v[0] != ST_WAIT and kept < keep:
                shown.add(i)
                kept += 1
        for i, v in enumerate(views):
            if v[0] == ST_WAIT and kept < keep:
                shown.add(i)
                kept += 1
        hidden = len(units) - len(shown)
    for i, (u, v) in enumerate(zip(units, views)):
        if not hidden or i in shown:
            block.append(_folder_row(u, v, spin, now, w, notes))
    if hidden:
        block.append(_row("  {} {} folders Waiting".format(WAIT_MARK, hidden),
                          "", w, dim=True))
    return block


_frame.fills = {}             # folder label -> displayed cell fill in dots


def _paint(out, tick, t0, drew, last_cols, final=False):
    """Draw one frame in place; returns the new (drew, last_cols).
    `drew` > 1 means a block of drew-1 lines is on screen; 1 means the
    narrow one-line spinner; 0 means nothing to climb over.

    Every full-block line is written as `GO_COL + erase + text + \\n`:
    the newline makes compose prefix the line, and the column jump keeps
    every redraw to the right of that prefix (see docstring)."""
    with _lock:
        total = len(_state)
        done = sum(1 for n in _state if _state[n][0] == ST_DONE)
    size = shutil.get_terminal_size(fallback=(80, 24))
    cols = size.columns
    buf = []
    if drew and last_cols is not None and cols < last_cols:
        # terminal narrowed: the old block's lines may have wrapped, so
        # cursor-up counts no longer match physical lines — never climb
        # over them. Abandon the old block (it stays as one stale frame)
        # and draw a fresh one below.
        buf.append(GO_COL + "\033[J")
        drew = 0
    last_cols = cols

    if total == 0:
        if final:                 # nothing worth keeping: just erase
            if drew > 1:
                buf.append("\033[{}A".format(drew - 1) + GO_COL + "\033[J")
            elif drew == 1:
                buf.append(GO_COL + EL)
            drew = 0
        else:
            text = "{}{}{} preparing".format(
                CYAN, SPINNER[tick % len(SPINNER)], RESET)
            buf.append(GO_COL + EL + text)
            drew = 1
    elif cols < MIN_FULL_COLS:    # one-line fallback
        if drew > 1:              # leaving block mode: erase block
            buf.append("\033[{}A".format(drew - 1) + GO_COL + "\033[J")
        if final:
            mark = TICK if done >= total else WAIT_MARK
            buf.append(GO_COL + EL + "{}{}{} rtsp-streamer {}/{} {}\n".format(
                GREEN if done >= total else DIM, mark, RESET, done, total,
                "Ready" if done >= total else "stopped"))
            drew = 0
        else:
            text = "{}{}{} rtsp-streamer {}/{} Preparing".format(
                CYAN, SPINNER[tick % len(SPINNER)], RESET, done, total)
            buf.append(GO_COL + EL + text)
            drew = 1
    else:
        block = _frame(tick, t0, cols, size.lines)
        prev = drew - 1 if drew > 1 else 0
        if prev:                  # climb back to the block's top
            buf.append("\033[{}A".format(prev))
        elif drew == 1:           # leaving one-line mode
            buf.append(GO_COL + EL + "\n")
        for l in block:
            buf.append(GO_COL + EL + l + "\n")
        if prev > len(block):     # block shrank: erase the leftovers
            buf.append("\033[J")
        # the final frame stays put in the scrollback (docker-pull style):
        # the all-✔ block is the record of the run
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
    _frame.fills = {}
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


def start():
    """Start the loader; returns a handle for stop(). Safe to call only when
    stdout is a real terminal (streamer.py checks isatty first)."""
    ev = threading.Event()
    th = threading.Thread(target=_run, args=(ev,), daemon=True)
    th.start()
    return (ev, th)


def stop(handle):
    ev, th = handle
    ev.set()
    th.join(timeout=2.0)
