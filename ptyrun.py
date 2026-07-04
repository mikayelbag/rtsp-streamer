#!/usr/bin/env python3
"""ptyrun — run a command on its own pseudo-terminal, relaying output.

Bakes the TTY into the image: streamer.py always sees an interactive
stdout (isatty() == True), so the prod loader animation runs under
`docker compose up` without any `tty:` line in the user's compose file.

- If stdout already is a terminal (`docker run -it`), the wrapper is
  pointless — exec the command directly so it gets the real terminal,
  real size and resize events.
- Otherwise fork the command onto a pty and copy the pty output verbatim
  to real stdout (compose's log stream, `docker logs`, a pipe — whatever
  it is). ANSI sequences pass through untouched, which is exactly what
  makes the in-place animation render.
- A fresh pty is 0×0; width-aware programs (the loader) would fall back
  to their narrow one-line mode, so give it a sane fixed size first.
  80 columns deliberately: the pty's size can't track the *viewer's*
  terminal (compose doesn't forward resizes without `tty:`), and a
  redraw line longer than the viewer's terminal wraps there, which
  breaks cursor-up in-place rewrites. 80 is the safe floor virtually
  every terminal meets, so the animation never wraps on the far end.
- SIGTERM/SIGINT/SIGHUP are forwarded to the child, so `docker stop`
  still reaches streamer.py's graceful-shutdown handler; the child's
  exit code is propagated so container status stays truthful.
"""

import fcntl
import os
import pty
import signal
import struct
import sys
import termios

PTY_ROWS, PTY_COLS = 24, 80


def main():
    cmd = sys.argv[1:]
    if not cmd:
        sys.exit("usage: ptyrun.py <command> [args...]")

    if sys.stdout.isatty():                  # real terminal already attached
        os.execvp(cmd[0], cmd)

    pid, master = pty.fork()
    if pid == 0:                             # child: on the pty slave
        os.execvp(cmd[0], cmd)

    fcntl.ioctl(master, termios.TIOCSWINSZ,
                struct.pack("HHHH", PTY_ROWS, PTY_COLS, 0, 0))
    for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(s, lambda n, _f: os.kill(pid, n))

    out = sys.stdout.buffer
    while True:
        try:
            data = os.read(master, 4096)
        except OSError:                      # EIO: child closed its side
            break
        if not data:
            break
        out.write(data)
        out.flush()

    _, status = os.waitpid(pid, 0)
    sys.exit(os.waitstatus_to_exitcode(status))


if __name__ == "__main__":
    main()
