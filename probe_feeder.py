#!/usr/bin/env python3
"""
Probe feeder — one continuous RTSP publish session that shows black until a
signal, then plays the content once, then returns to black (re-armed).

Used by streamer.py for `probe = true` streams. The point is a *seamless* switch:
the engine under test connects once and never disconnects when black flips to
content or content flips back to black. That rules out swapping ffmpeg processes
(drops readers) and filter-based input switching (consumes the content in the
background, so it wouldn't start at its phase frame). Instead this process owns a
single encoder and feeds it raw frames:

    [raw yuv420p frames] -> ffmpeg (libx264) -> RTSP   (one session, never ends)

It writes one frame per 1/fps tick, paced by an absolute clock:
  - idle: the same in-memory black frame (cheap to encode, constant memory);
  - on signal: every frame of the content file, decoded on a background
    thread — black keeps flowing until the decoder's first frame is actually
    ready (so the feed never stalls at the switch), then every content frame
    exactly once;
  - then back to black.

Signal = an mtime edge on --signal-file (`touch workspace/start`). No deletion,
so multiple feeders share one file with no race; each fires once per touch and
re-arms automatically. Phase is already baked into the content file (it starts at
its shift frame), so the feeders only need to start it at roughly the same time —
a ~50 ms poll is plenty.
"""

import argparse
import os
import queue
import subprocess
import sys
import threading
import time


def black_frame(w, h):
    """A single yuv420p limited-range black frame: Y=16, U=V=128."""
    ysize = w * h
    csize = ysize // 4
    return bytes([16]) * ysize + bytes([128]) * (csize * 2)


def signal_mtime(path):
    try:
        return os.stat(path).st_mtime
    except OSError:
        return None


def grow_pipe(f):
    """Best-effort: enlarge the decoder pipe. The 64 KiB default holds less
    than one 1080p frame, so the decoder could never run ahead of the paced
    writer; 1 MiB gives it a few frames of headroom."""
    try:
        import fcntl
        fcntl.fcntl(f.fileno(), getattr(fcntl, "F_SETPIPE_SZ", 1031), 1 << 20)
    except Exception:
        pass


def start_sink(args):
    """One continuous libx264 -> RTSP encoder reading raw frames on stdin.

    zerolatency drops the encoder's lookahead/B-frame buffering (~13 frames
    with veryfast), so a fired probe reaches the wire right away instead of a
    constant ~0.4 s (at 30 fps) after the touch — tighter for cold-start
    timing measurements. The x264 params arrive verbatim from streamer.py's
    X264_PARAMS (one definition, not a copy): camera-grade output (fixed IDR
    cadence, no B-frames, SPS/PPS repeated before every keyframe) so a client
    joining mid-stream configures its decoder immediately instead of spraying
    reference errors."""
    # threads=4: x264 defaults to ~1.5x cores *per feeder*, so a handful of
    # probe streams on a big host would spawn dozens of encoder threads and
    # thrash; 4 threads encode 1080p veryfast+zerolatency comfortably in
    # realtime while keeping N feeders predictable.
    params = args.x264_params + ":threads=4"
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
           "-f", "rawvideo", "-pix_fmt", "yuv420p",
           "-s", "{}x{}".format(args.width, args.height),
           "-r", str(args.fps), "-i", "pipe:0",
           "-c:v", "libx264", "-preset", args.preset, "-tune", "zerolatency",
           "-x264-params", params, "-pix_fmt", "yuv420p", "-an",
           "-f", "rtsp", "-rtsp_transport", "tcp", args.url]
    return subprocess.Popen(cmd, stdin=subprocess.PIPE)


def open_content(args):
    """Decode the content file to raw yuv420p frames (scaled to the sink size)."""
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
           "-i", args.content,
           "-vf", "scale={}:{}".format(args.width, args.height),
           "-f", "rawvideo", "-pix_fmt", "yuv420p", "-r", str(args.fps), "pipe:1"]
    return subprocess.Popen(cmd, stdout=subprocess.PIPE)


class ContentStream:
    """Content decoder with a pump thread and a small frame queue.

    Decoder spin-up (process spawn + input probe) takes long enough that a
    blocking read in the tick loop would starve the encoder of frames right
    at the black->content switch. The pump thread decouples that: until the
    first frame is queued, poll_frame() reports "not ready" and the caller
    keeps sending black; from then on it reads blocking, so every content
    frame is delivered exactly once (a slow decode slows delivery rather
    than dropping or duplicating frames — phase stays frame-exact). The
    bounded queue keeps memory constant (~3 frames)."""

    def __init__(self, args, frame_bytes):
        self.frame_bytes = frame_bytes
        self.proc = open_content(args)
        grow_pipe(self.proc.stdout)
        self.queue = queue.Queue(maxsize=3)
        self.first_seen = False
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        try:
            while True:
                b = self.proc.stdout.read(self.frame_bytes)
                if not b or len(b) < self.frame_bytes:
                    break
                self.queue.put(b)
        except Exception:
            pass                       # fd closed under us during shutdown
        finally:
            self.queue.put(None)       # end-of-content sentinel

    def poll_frame(self):
        """One content frame (bytes), b'' while the decoder is still warming
        up (caller keeps black), or None when the content is finished."""
        if not self.first_seen:
            try:
                item = self.queue.get_nowait()
            except queue.Empty:
                return b""
            if item is not None:
                self.first_seen = True
            return item
        return self.queue.get()

    def close(self, kill=False):
        if kill and self.proc.poll() is None:
            self.proc.terminate()
        try:
            self.proc.stdout.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--content", required=True)
    ap.add_argument("--url", required=True)
    ap.add_argument("--width", type=int, required=True)
    ap.add_argument("--height", type=int, required=True)
    ap.add_argument("--fps", type=float, required=True)
    ap.add_argument("--x264-params", required=True,
                    help="x264 param string from streamer.py (X264_PARAMS)")
    ap.add_argument("--preset", default="veryfast")
    ap.add_argument("--signal-file", required=True)
    args = ap.parse_args()

    frame_bytes = args.width * args.height * 3 // 2
    black = black_frame(args.width, args.height)
    period = 1.0 / args.fps if args.fps > 0 else 1.0 / 30
    poll_every = max(1, int(round(0.05 / period)))   # check the signal ~20x/s

    sink = start_sink(args)
    # arm against the signal's current state: only a *newer* touch fires us
    last_fire = signal_mtime(args.signal_file)

    content = None          # ContentStream while playing content once
    next_tick = time.monotonic()
    tick = 0
    try:
        while True:
            if sink.poll() is not None:
                break       # streamer.py will restart us

            # fire on an mtime edge (a touch newer than the last one we saw)
            if content is None and tick % poll_every == 0:
                m = signal_mtime(args.signal_file)
                if m is not None and (last_fire is None or m > last_fire):
                    last_fire = m
                    content = ContentStream(args, frame_bytes)

            frame = None
            if content is not None:
                item = content.poll_frame()
                if item is None:
                    content.close()
                    content = None      # content done -> back to black
                elif item:
                    frame = item
            if frame is None:
                frame = black

            try:
                sink.stdin.write(frame)
                sink.stdin.flush()
            except (BrokenPipeError, ValueError):
                break

            tick += 1
            next_tick += period
            delay = next_tick - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            elif delay < -1.0:
                next_tick = time.monotonic()   # fell far behind: resync clock
    finally:
        if content is not None:
            content.close(kill=True)
        if sink.poll() is None:
            try:
                sink.stdin.close()
            except Exception:
                pass
            sink.terminate()
            try:
                sink.wait(timeout=5)
            except subprocess.TimeoutExpired:
                sink.kill()
    return 0


if __name__ == "__main__":
    sys.exit(main())
