# RTSP Streamer — Technical Documentation

This document explains how the project works internally, mechanism by
mechanism, in the order you would meet them while reading the code. It is
written so that a junior engineer with no video-streaming background can
follow along. The user-facing overview (quick start, config reference) lives
in [`README.md`](README.md); this file is the "why and how" companion.

Version covered: **2.9.0**.

---

## 1. What this project is

A folder of video files goes in; a set of stable, seamlessly-looping **live
RTSP streams** comes out. Video-analytics engines (people counters, object
detectors, …) can then be pointed at those URLs as if they were real IP
cameras — for load testing, benchmarking, and cold-start timing.

```
video_samples/ids/clip.mp4  ──►  rtsp://<host>:41000/ids/clip   (loops forever)
```

Three programs cooperate:

| Program | Role |
|---|---|
| **mediamtx** | An off-the-shelf RTSP server (a single Go binary). It accepts one *publisher* per stream path and fans the stream out to any number of *readers* (clients). It never touches the video itself. |
| **ffmpeg** | One process per stream, publishing the prepared file to mediamtx in a loop (`-stream_loop -1`). |
| **streamer.py** | The orchestrator. Prepares the files, generates the mediamtx config, launches everything, prints the URLs, and supervises/restarts the processes. |

```
                         ┌────────────────────────────────────────────┐
                         │                streamer.py                 │
                         │  scan → prepare → configure → supervise    │
                         └──────┬──────────────┬──────────────┬───────┘
                          spawns│        spawns│        spawns│
                                ▼              ▼              ▼
                          ┌──────────┐   ┌──────────┐   ┌────────────────┐
                          │ mediamtx │   │ ffmpeg   │   │ probe_feeder.py│
                          │ (RTSP    │◄──┤ publisher│   │ (for probe     │
                          │  server) │   │ per      │   │  streams only) │
                          └────┬─────┘   │ stream   │   └───────┬────────┘
                               │         └──────────┘           │
                     RTSP read │              ▲ publishes       │ publishes
                               ▼              │ rtsp://127.0.0.1│ (via its own
                     ┌──────────────┐         └─────────────────┘  ffmpeg)
                     │ your engine /│
                     │ VLC / ffplay │
                     └──────────────┘
```

Key property: **one publisher per stream, regardless of client count**.
mediamtx does the fan-out; adding readers costs no extra encoding.

---

## 2. Repository map

| File | What it is |
|---|---|
| `streamer.py` | The orchestrator; ~95 % of the logic lives here. |
| `probe_feeder.py` | Standalone helper process for `probe = true` streams (black-until-signal). One instance per probe stream. |
| `loader.py` | The terminal loading animation shown in prod mode. Pure cosmetics; zero effect on streaming. |
| `ptyrun.py` | Container-only pty wrapper: gives streamer.py a pseudo-terminal so the animation works under compose without `tty:`; forwards SIGTERM, propagates the exit code. |
| `config.ini` | The shipped default configuration (copied into the image as `/app/defaults/config.ini`). |
| `entrypoint.sh` | Container entrypoint: seeds a default config into the mounted config dir on first run, then execs `streamer.py`. |
| `Dockerfile` | Alpine + python3 + ffmpeg + the mediamtx binary. Bakes default env vars and the healthcheck. |
| `docker-compose.yaml` | Reference compose service (host networking, three volume mounts). |
| `dockerhub.sh` | Maintainer helper: pushes the README to Docker Hub, lists/prunes tags. Not used at runtime. |
| `mediamtx` | The mediamtx server binary (also downloadable in the Docker build). |
| `video_samples/` | Source videos, one subfolder per group. Mounted read-only in Docker. |
| `workspace/` | Everything generated: prepared `.ts` files, logs, the mediamtx config, the probe signal file. Safe to delete — it is a cache. |

---

## 3. Concepts you need (5-minute video-streaming primer)

If you already know RTSP and H.264 internals, skip to §4.

**RTSP** (Real-Time Streaming Protocol) is the control protocol IP cameras
speak. A client connects to `rtsp://host:port/path`, negotiates, and then
receives media packets over TCP (interleaved) or UDP (RTP). *Publishing* is
the same handshake in the other direction: a source pushes media to the
server under a path, and the server relays it to readers.

**Container vs codec.** A codec (H.264/AVC, H.265/HEVC) defines how frames
are compressed. A container (MP4, MKV, MPEG-TS) defines how those compressed
frames are laid out in a file with timestamps. Re-wrapping the same encoded
frames into a different container is called a **remux** and is lossless and
cheap (`ffmpeg -c copy`). Re-encoding (decode + encode again) is expensive
and lossy; this project avoids it unless required.

**GOP, keyframes, IDR.** H.264 frames come in three kinds: I-frames
(self-contained images), P/B frames (deltas that *reference other frames*).
An **IDR** frame is a special I-frame that also says "nothing after me may
reference anything before me" — a hard reset point. A decoder can only start
(or splice) cleanly at an IDR. The distance between keyframes is the **GOP
size**; this project forces a keyframe every `keyint` frames (config key,
default 60) whenever
it encodes, so clients joining a stream get a picture within 2 s at 30 fps.

**PTS/DTS.** Every packet carries a *presentation* timestamp (when to show
it) and a *decode* timestamp (when to decode it — earlier than PTS when
B-frames are used, because B-frames reference future frames). Muxers demand
that DTS never goes backwards. This single rule is why looping MP4s crashes —
see §7.

**`-re` and `-stream_loop -1`.** By default ffmpeg reads a file as fast as
the disk allows. `-re` throttles reading to the file's own timestamps —
exactly what "pretend to be a live camera" needs. `-stream_loop -1` re-reads
the input forever, offsetting timestamps each pass.

**Raw video / yuv420p.** A decoded frame in the common 4:2:0 pixel format is
just `width×height` luma bytes + two quarter-size chroma planes =
`w*h*3/2` bytes. A 1920×1080 frame is ~3.1 MB. `probe_feeder.py` pipes such
raw frames into an encoder — knowing the exact byte size per frame is what
lets it treat a pipe as a frame stream (§10).

---

## 4. Process architecture

At steady state, for a config with two normal streams and one probe stream,
the process tree looks like:

```
python3 streamer.py                    (supervisor, 1 s poll loop)
├── mediamtx workspace/mediamtx_41000.yml
├── ffmpeg -re -stream_loop -1 -i ws/ids/a.ts   -c copy -f rtsp rtsp://127.0.0.1:41000/ids/a
├── ffmpeg -re -stream_loop -1 -i ws/ids/b.ts   -c copy -f rtsp rtsp://127.0.0.1:41000/ids/b
└── python3 probe_feeder.py --content ws/ids/c.ts --url rtsp://127.0.0.1:41000/ids/c ...
    ├── ffmpeg (sink: rawvideo on stdin → libx264 → RTSP publish)   [always running]
    └── ffmpeg (content decoder → raw frames on stdout)             [only while firing]
```

Everything is plain `subprocess.Popen` — no threads carry media. The only
threads in the system are: the prepare worker pool (§6, exists only during
startup), the loader animation thread (cosmetic), and one pump thread inside
each firing probe feeder (§10).

All publishers connect to mediamtx over `127.0.0.1` with TCP transport —
publishing locally over TCP is lossless and immune to UDP packet reordering;
the *client-facing* transport is configured separately (§11).

---

## 5. Configuration system

`load_config()` / `settings_for()` in `streamer.py`.

Three layers merge, most specific wins, applied in this order:

```
[streamer]              global defaults
[folder:ids]            everything under video_samples/ids/
[video:ids/clip1]       exactly video_samples/ids/clip1.mp4
```

The mergeable keys are `SETTING_KEYS = (copies, shift_frames, fps, size,
bitrate_kbps, probe, force_reencode, mini, concat, camera_grade)`. A **blank value means "don't
override"** — `apply_section()` skips empty strings, so a `[video:x]` section
can override one key without restating the rest.

Two path keys behave specially in Docker: `videos` and `workspace` resolve
relative to the config file's directory *unless* the `VIDEOS_DIR` /
`WORKSPACE_DIR` environment variables are set. The image sets them to
`/app/video_samples` and `/app/workspace`, so the config can live in a
separately-mounted `/app/config` directory without its relative paths
resolving underneath it.

Other environment overrides: `MODE` (`prod`/`dev`, §17) and `ADVERTISE_IP`
(the IP printed in URLs; blank = auto-detect via a UDP socket "connect" to
8.8.8.8, which never sends a packet — it just asks the kernel which local
interface would route there).

---

## 6. Startup lifecycle

`main()` top to bottom:

1. **`raise_nofile()`** — lifts the open-file soft limit to 65536. Every
   RTSP client is a socket, i.e. a file descriptor inside mediamtx and this
   process tree; the default 1024 would cap concurrent readers. Doing it in
   code means compose files need no `ulimits` block.
2. **Load config**, resolve `videos_dir` / `workspace`, parse the port list,
   validate transport.
3. **`scan_videos()`** — recursive walk of the videos dir collecting known
   extensions, sorted for determinism.
4. **Workspace sweep** — deletes leftover `*.part<pid>` temp files from a
   previous run that was killed mid-build (§7, atomicity), and the previous
   `.port` file so the Docker healthcheck can't probe a stale port.
5. **Name dedup pass.** Path names are sanitized for mediamtx
   (`sanitize()`: anything outside `[A-Za-z0-9._~-]` becomes `_`, repeats
   collapsed) — mediamtx hard-rejects other characters at startup. Two
   different sources can sanitize to the same `(folder, stem)` (e.g.
   `clip.mp4` + `clip.mkv`, or `a b.mp4` + `a_b.mp4`). Without dedup they
   would silently share one workspace file *and* one RTSP mount — one stream
   lost, the cache cross-contaminated. Duplicates get a `__2`, `__3` …
   suffix; the double underscore cannot clash with a sanitized name because
   `sanitize()` collapses repeated underscores.
6. **Parallel prepare.** Each `(src, folder, stem)` job runs
   `prepare_source()` (§7) on a `ThreadPoolExecutor` with
   `min(4, cpu_count)` workers. Threads are the right tool here even in
   Python: the actual work happens in ffmpeg *subprocesses*, so the GIL is
   idle. `ex.map()` preserves source order, so mounts and printed URLs stay
   deterministic run to run. Each finished job updates the loader's progress
   bar. A worker that throws is caught, logged with a traceback, and
   contributes zero streams — one broken video never kills the run.
7. **Signal-file reset** — a stale `workspace/start` from a previous run is
   removed so probe streams start *armed*, not firing.
8. **mediamtx start with port fallback** (§14) — first free port from the
   configured list; config written as `mediamtx_<port>.yml`; the chosen port
   recorded in `workspace/.port` for the healthcheck. Instead of a fixed
   post-spawn sleep, `wait_rtsp_ready()` polls until the server actually
   accepts a TCP connection — faster on a fast host, and on a loaded host the
   publishers can no longer race a slow-starting server into their first
   restart backoff. An API port (`rtsp_port + 2000`, localhost only) is
   allocated alongside for the delivery watchdog (§12).
9. **Stream launch.** Every prepared stream becomes one command line:
   `publish_cmd()` (plain ffmpeg loop) or `feeder_cmd()`
   (`probe_feeder.py`). A final `unique_mount()` guard renames any remaining
   mount collision (a copy suffix like `clip_2` can collide with a real file
   named `clip_2.mp4`) instead of silently overwriting the dict entry.
10. **Readiness wait, then the URL banner.** The banner is held (up to 15 s)
    until the mediamtx API reports every mount `ready` — i.e. its publisher
    has completed the RTSP handshake and is delivering. "Ready" in the output
    therefore means an engine pointed at the URLs connects on the first try.
    Streams still pending at the timeout are logged and left to the
    supervisor; the banner prints regardless.
11. **Supervision loop** entered (§12).

---

## 7. The prepare pipeline

`prepare_source()` — the heart of the project. Per video, in order:

### 7.1 Why MPEG-TS at all

The goal is `-stream_loop -1` looping *without ever disconnecting clients*.
MP4 cannot do this: MP4 files carry edit lists and B-frame reordering
offsets, and at the loop wrap ffmpeg produces a **non-monotonic DTS** — the
muxer errors out, the publisher dies, and every connected client is dropped.
MPEG-TS has no such structure; timestamps restart cleanly and the loop is
invisible. So every source is converted to `.ts` in the workspace once, then
looped forever with a cheap `-c copy` publisher.

### 7.2 Remux or transcode?

`needs_transcode()` collects reasons; if none, a lossless remux suffices:

```
force_reencode = true        → transcode ("forced")
fps / size / bitrate ≠ keep  → transcode (the change requires re-encoding)
ffprobe can't read it        → transcode ("unreadable")
codec not h264/hevc          → transcode (not RTSP-friendly as-is)
source has B-frames          → transcode ("bframes") with camera_grade=true,
                               else remux + (!) warning (camera realism, below)
keyframes > 2×KEYINT apart   → transcode ("gop") with camera_grade=true,
                               else remux + (!) warning (camera realism, below)
otherwise                    → remux (-c copy, lossless, fast)
```

If even the remux fails (some AVIs carry no usable timestamps — ffmpeg says
`first pts and dts value must be set`), it falls back to a transcode, which
synthesizes fresh timestamps. A quirky source still serves.

**Camera realism.** The point of the whole tool is to stand in for real IP
cameras, and real cameras encode very specifically: **no B-frames** and an
IDR keyframe on a fixed 1–2 s clock. That isn't cosmetic — a client that
connects to an RTSP stream mid-GOP starts receiving delta frames whose
reference frames it never saw. With B-frames in the stream the decoder's
reorder machinery fails loudly (`reference picture missing during reorder`,
`mmco: unref short failure`, `number of reference frames … exceeds max`);
with a long GOP the garbage lasts until the next keyframe, potentially many
seconds. So every encode in the project uses one shared parameter set
(`X264_PARAMS` in `streamer.py`):

```
keyint=60 : min-keyint=60   IDR exactly every `keyint` frames (config key,
                            default 60 = 2 s @ 30 fps; the join-latency lever)
scenecut=0                  no surprise keyframes off the cadence
bframes=0                   no B-frames → no reorder, no reorder errors
repeat-headers=1            SPS/PPS before every IDR → a mid-stream join
                            can configure its decoder immediately
```

and with `camera_grade = true`, `needs_transcode()` refuses to remux any
source that itself carries B-frames (`has_b_frames` from ffprobe) or a
keyframe cadence looser than 2×KEYINT (`_gop_too_long()`, read from the
packet index). The observable result matches a real camera exactly: joining
mid-stream costs at most one keyframe interval (≤2 s) of waiting, then a
clean picture — no decode-error spam.

**The default: `camera_grade = false` (since 2.5.0).** The realism checks
(B-frames, long GOP) are *warnings*, not re-encode reasons: the source is
remuxed as-is (no CPU burn on first run), the warning is recorded in the
artifact's build stamp, and the URL banner marks the stream with a short
`(!)` suffix plus one legend line — visible exactly where the URL gets
copied, no log noise. Setting `camera_grade = true` rebuilds the marked
artifacts automatically (the stamp remembers). Codec conversion is *not*
affected by the setting: a non-h264/hevc source (e.g. MPEG-4 Part 2) always
re-encodes — that's compatibility, not realism; no engine speaks Xvid over
RTSP.

Transcodes are libx264 `-preset veryfast` with the params above, yuv420p,
audio stripped (`-an` — analytics engines don't consume audio, and dropping
it removes a whole class of A/V-sync loop bugs).

### 7.3 Caching

One function, `is_fresh()`, decides cache validity for every artifact.
Fresh means: `force_reencode` is off (otherwise "force" would appear to do
nothing until you deleted the workspace by hand), the artifact is at least
as new as its source (**mtime comparison**), and its **build stamp**
matches the config.

The stamp is a small `<artifact>.meta` JSON written next to every
successful build, recording the streamer version and the `keyint` it was
encoded with. It does two jobs:

- **No startup rescans.** A stamped artifact is trusted outright; source
  analysis (`needs_transcode()` and its full packet-index GOP scan) runs
  only on a cache miss. Cached startups no longer scale with library size.
- **`keyint` invalidation.** A stamp whose keyint differs from the config
  triggers a rebuild — previously a documented footgun (keyint edits
  silently kept serving the old cadence until the workspace was wiped).

Artifacts without a stamp (built by versions before 2.4.0) are verified
once with `_camera_grade()` (no B-frames, sane keyframe cadence — §7.2) and
stamped if they pass, so an old workspace **migrates itself**: pre-2.3.2
B-frame files fail and rebuild, good files get stamped and are never
rescanned again.

Artifact names encode what produced them, so config changes naturally miss
the old cache entry:

| Workspace file | Meaning |
|---|---|
| `stem.ts` | plain remux |
| `stem__r1280x720_f25_b2000k_enc.ts` | transcode; tags = the settings used |
| `stem__body_s30c4.ts` | rotation body for `shift_frames=30, copies=4` (§8) |
| `stem__fwd90.ts` | forward-rotated copy starting at frame 90 (§8) |
| `stem__mini.ts` / `stem__minip.ts` | mini composite; `p` = probe variant (§9) |
| `*.ts.meta` | build stamp (version + keyint) for the cache check |
| `*.part<pid>` | in-progress build (temp; swept at startup) |

### 7.4 Atomic writes

Every build writes to `<dst>.part<pid>` and `os.replace()`s it into place
only on success (`_tmp_for()`). Rationale: a container stopped mid-transcode
would otherwise leave a truncated `.ts` whose *fresh mtime passes the cache
check* — it would be served, corrupt, on every subsequent run with no error
anywhere. `os.replace` is atomic on POSIX: any observer sees either the old
complete file or the new complete file, never a half-written one. The pid
suffix keeps two containers sharing a workspace off each other's temp files.

---

## 8. Phase-shifted copies (`copies` + `shift_frames`)

**Goal:** N streams from one file that look like N out-of-phase cameras —
copy *i* starts at frame `i·shift` — while still looping seamlessly and never
drifting apart.

**Why the obvious approach fails.** A runtime seek (`ffmpeg -ss ... -c
copy`) can only start at a keyframe: a stream-copy seek snaps to the nearest
GOP boundary, so a sub-GOP shift silently collapses to frame 0. Decoding-and-
re-encoding each copy at runtime would cost a full encoder per copy, forever.

**The trick: rotate the file, not the playback.** Copy *i* is a new file
containing frames `[k:N] + [0:k]` where `k = i·shift mod N` — it *begins* at
frame k, loops back through the start, and has the same total length N as
every other copy, so the copies stay exactly `shift` frames apart forever (no
drift). Built once at prepare time, then published with the same cheap
`-c copy` loop as any other stream.

To cut a file at arbitrary frame k with a stream-copy, frame k must be an
IDR. Two build steps make that true:

### 8.1 `_full_reencode()` — force IDRs at the cut points

The body is the whole video re-encoded once to uniform libx264 with IDRs
forced *frame-exactly* at every cut via
`-force_key_frames expr:eq(n,k₁)+…` (a time-based force rounds to the
nearest frame, which would put the cut one frame off). One uniform encode
means every splice point — the segment joins in §8.2 and the
`-stream_loop` wrap — shares one SPS/PPS and lands on an IDR, so it decodes
cleanly *by construction*. Built once per video, cached and stamped like
every other artifact.

(Earlier versions spliced a re-encoded head onto a stream-copied tail to
save encode time, which required a decode-verification pass and an
open-GOP fallback to the full re-encode anyway. A one-time cached prepare
step doesn't earn that complexity; the always-correct path is now the only
path.)

### 8.2 `build_forward()` — rotate with pure stream-copies

With an IDR guaranteed at frame k, the ffmpeg **segment muxer** splits the
body frame-exactly into `seg0 = [0:k]` and `seg1 = [k:N]`
(`-f segment -segment_frames k -c copy`), and the concat demuxer joins them
as `seg1 + seg0`. Both splice points — seg1-end→seg0-start inside the file,
and seg0-end→seg1-start at the `-stream_loop` wrap — land on IDRs, so both
decode cleanly. No re-encoding per copy; each rotation is two stream-copies.

Copy 0 (k = 0) streams the untouched `play_path` directly. Any failure along
the way logs and falls back to unshifted copies — degraded, never dead.

---

## 9. Mini mode

For short side-clips (a few seconds) that should appear as a calm 1-minute
loop instead of a frantic 3-second one:

```
[ first frame frozen 10 s ][ the clip, once ][ black to fill 60 s ]
```

`build_mini()` does this in **one** libx264 pass using the `tpad` filter:
`start_mode=clone` holds frame 0 for `MINI_FREEZE_S` (10 s);
`stop_mode=add:color=black` pads black to reach `MINI_LOOP_S` (60 s). If the
clip + freeze already exceed the minute, the black tail is simply omitted
(the loop runs a little long). Because the whole composite is one uniform
encode opening on an IDR with the standard `keyint` cadence, it loops as
cleanly as any other prepared file.

Composition rules: `mini` **ignores `shift_frames`** (the composite reshapes
the timeline, so phase offset is meaningless — the code zeroes `shift`), and
combined with `probe = true` the black tail is dropped from the file because
the probe feeder itself returns to black after playing content (§10) — that
*is* the trailing black.

**Numbered mounts.** Mini streams mount as plain numbers in scan order —
`/mini/1`, `/mini/2`, … — instead of file stems (side-clip filenames are
usually long and irrelevant to the engine under test). The number → source
file map is written to `workspace/<folder>/manifest.txt`
(`MANIFEST_NAME`), and the URL banner prints only the first and last mini
URL with a `⋮ (N more)` line between, so 60 clips don't flood the banner.

### 9.1 Concat chains (`concat = true`)

`prepare_concat()` chains **all** of a folder's videos into ONE looping
stream mounted at `/<folder>/chain`. Per video, `build_concat_segment()`
produces one normalised segment:

```
[ first frame frozen CONCAT_FREEZE_S (3 s) ][ the clip ][ last frame frozen 3 s ][ CONCAT_GAP_S (2 s) black ]
```

with the video's index burned into the top-left corner (drawtext — the
reason the image ships `font-dejavu`). Mixed sources are normalised to the
folder's `size`/`fps` (`CONCAT_DEFAULT_SIZE` 1920×1080 @ 30 when `keep`),
then the segments are concatenated into a single uniform artifact. The
`manifest.txt` records index → source file, start offset inside the loop
and duration — know the number on screen, find the video and when it plays.

---

## 10. Probe mode & `probe_feeder.py`

**Purpose:** measure how fast an engine reacts to content appearing on a
stream it is *already connected to* (cold-start timing). The stream shows
black; you fire a signal; it plays the content once; back to black, re-armed.

**The hard requirement is seamlessness** — the client must never disconnect
or even see a hiccup at the black↔content switches. That eliminates:
swapping ffmpeg processes (kills the RTSP session), and ffmpeg filter-graph
input switching (the content input is consumed in the background even while
unselected, so it wouldn't start from its first frame when switched in).

**Architecture:** one feeder process per probe stream, owning one
ever-running publish session:

```
                       probe_feeder.py
   ┌──────────────────────────────────────────────────────┐
   │ tick loop, one frame per 1/fps, absolute-clock paced  │
   │                                                      │
   │  idle:   in-memory black frame ────────────┐         │
   │                                            ▼         │
   │  firing: ffmpeg decoder ─► pump thread ─► queue ─►  write ─► sink ffmpeg ─► RTSP
   │          (content → raw     (bounded,      (≤3       stdin   (rawvideo→libx264,
   │           yuv420p frames)    daemon)       frames)           zerolatency)
   └──────────────────────────────────────────────────────┘
```

Piece by piece:

- **The sink** (`start_sink`) is a single ffmpeg encoding raw yuv420p frames
  from stdin to libx264 and publishing to mediamtx. It runs for the life of
  the stream — this is what makes the switches invisible. `-tune
  zerolatency` disables the encoder's lookahead and B-frames (~13 frames of
  internal buffering with veryfast), so a fired probe reaches the wire
  immediately instead of a constant ~0.4 s late at 30 fps — that constant
  would otherwise pollute every cold-start measurement.
- **The black frame** is built once in memory: `Y=16, U=V=128` (limited-range
  black), `w·h·1.5` bytes. Encoding the identical frame repeatedly is
  extremely cheap.
- **Pacing** uses an absolute deadline (`next_tick += period`) rather than
  `sleep(period)`, so per-tick jitter never accumulates. If the loop falls
  more than a second behind (machine suspend, CPU starvation), it resyncs
  the clock instead of fast-forwarding.
- **The signal** is an *mtime edge* on `workspace/start`: fire when the file's
  modification time is newer than the last one seen. `touch` is the whole
  client API. Nothing deletes the file, so any number of feeders share it
  without racing; each fires once per touch and re-arms itself. Touches
  during a pass coalesce into a single replay afterwards. Polling is every
  ~50 ms — feeders only need to start *roughly* together, since each copy's
  phase offset is baked into its content file (§8).
- **The ContentStream** answers a subtle frame-delivery problem: spawning
  the decoder and probing its input takes 100–300 ms, and a blocking read in
  the tick loop would deliver *nothing* to the sink for that long — a
  visible stall at exactly the switch this design exists to keep seamless.
  So a pump thread reads decoded frames into a bounded queue
  (3 frames ≈ 9 MB at 1080p), and the tick loop:
  - before the first frame arrives: `get_nowait()`, falling back to black —
    the feed never starves;
  - after the first frame: blocking `get()` — every content frame is
    delivered **exactly once**, in order. A decoder slower than realtime
    slows delivery rather than dropping or duplicating frames, keeping
    phase-shifted copies frame-exact relative to each other.
  - A `None` sentinel from the pump marks end-of-content → back to black,
    re-armed.
  The decoder's stdout pipe is also enlarged to 1 MiB (`F_SETPIPE_SZ`; the
  64 KiB default holds less than one 1080p frame), giving the decoder real
  read-ahead room.
- **Exit** is cooperative: if the sink dies, the feeder exits and
  `streamer.py`'s supervisor restarts it (§12); `BrokenPipeError` on write
  means the same thing mid-write.

---

## 11. The generated mediamtx config

`write_mediamtx_config()` writes a minimal YAML per instance —
`workspace/mediamtx_<port>.yml`. Why per-port: mediamtx **hot-reloads** its
config when the file changes, so two instances sharing a workspace through a
single `mediamtx.yml` would rebind each other the moment the second one
writes the file.

The interesting keys:

| Key | Why |
|---|---|
| `writeQueueSize: 2048` | Per-client send buffer, in packets. A slow reader or a bitrate spike fills the queue instead of forcing a disconnect; sized to comfortably hold 100+ readers per stream. |
| `rtspTransports` | From config: `[tcp]`, `[udp]`, or both — what *clients* may use; publishers always use TCP internally. |
| `rtpAddress/rtcpAddress: port+10000/+10001` | UDP mode's data ports, offset so parallel instances never collide. |
| `paths: all_others:` | The catch-all path: accept any publisher on any path. Access control is not this tool's job (test-LAN scope, see README security note). |
| `api: yes` + `apiAddress: 127.0.0.1:<port+2000>` | The control API that feeds the readiness wait (§6) and the delivery watchdog (§12). Bound to loopback only — invisible off-host. If no port near `+2000` is free the API is disabled and the watchdog silently turns off. |
| everything else `no` | Metrics, HLS, WebRTC, SRT, RTMP all disabled — smallest possible surface. |

---

## 12. Supervision, restarts, shutdown

The main loop polls once per second:

- **mediamtx died** → everything is unusable; log and shut down (in Docker,
  `restart: unless-stopped` brings the whole container back).
- **A stream process died** → schedule a restart with exponential backoff:
  2 s doubling to 30 s cap, reset to 2 s after a process survives 60 s
  (`RESTART_BACKOFF_START/MAX`, `STABLE_RESET_SECONDS`). Backoff prevents a
  broken file from hot-looping ffmpeg spawns; the stable-reset makes an
  occasional crash cheap again.
- **A stream process hung** (the delivery watchdog) → a publisher can wedge
  without dying — an ffmpeg blocked forever on a socket write, a stuck
  decode — and a process-alive check will never notice: the stream is up, the
  process is up, and no frames flow. Every `WATCHDOG_POLL_SECONDS` (5 s) the
  supervisor fetches `/v3/paths/list` from the mediamtx API and compares each
  mount's `bytesReceived` counter against the last sample. That counter is
  ground truth for "data is arriving at the server". A *live* process whose
  counter has not moved for `STALL_SECONDS` (15 s — comfortably above any
  legitimate connect/keyframe gap) is `kill()`ed (SIGKILL, not SIGTERM: a
  wedged process may not service signals), which drops it into the normal
  died→backoff→restart path above. The watchdog is best-effort by design:
  if the API is unreachable it skips the cycle — it can *miss* a stall for
  one poll, but it can never falsely kill a healthy stream, because only a
  frozen byte counter triggers it. Probe feeders are covered identically —
  their encoder emits black frames continuously, so their counter always
  moves while healthy.

Shutdown (SIGINT/SIGTERM, noted by flag from the signal handler — no real
work happens inside the handler): `terminate()` every child, wait up to 5 s
collectively, `kill()` stragglers. The prepare phase's atomic writes (§7.4)
make even a hard kill during first-run transcodes safe.

---

## 13. Concurrency model, summarized

| Where | Mechanism | Bounded by |
|---|---|---|
| Prepare | `ThreadPoolExecutor`, ≤4 workers driving ffmpeg subprocesses | `min(4, cpu_count)` — parallel x264 encodes saturate cores fast |
| Streaming | One OS process per stream + mediamtx fan-out | one publisher regardless of reader count |
| Readers | mediamtx internal goroutines | `writeQueueSize` per client, 65536 fds |
| Probe switch | 1 pump thread + bounded queue per *firing* feeder | 3 frames in flight |
| Supervision | single-threaded 1 s poll | — |

Shared mutable state in `streamer.py` is essentially nil: prepare jobs touch
disjoint files (guaranteed by the dedup pass), results are collected in
source order, and log writes are single `write()` calls behind the GIL.

**Multi-instance:** several containers can share one host (host networking +
port fallback) and even one workspace — per-port mediamtx configs and
pid-suffixed temp files keep them from corrupting each other; concurrent
first-run prepares do duplicate work at worst (last atomic rename wins, both
results are valid). A shared `workspace/start` fires *all* instances' probes
at once, which is usually what a benchmark wants anyway.

---

## 14. Port selection

`port_is_free()` binds a test socket **with `SO_REUSEADDR`** — the same way
mediamtx (Go's listener) will bind. Without that flag, TIME_WAIT sockets left
by the previous run's RTSP clients (~60 s lifetime) make a genuinely free
port look busy, the fallback walks past every port, and the streamer refuses
to start for a minute after every restart. Testing with the same socket
options the real server uses eliminates the false positive.

There is still an inherent TOCTOU window (another process could grab the
port between the test and mediamtx's bind); the loop covers it with
`wait_rtsp_ready()` — poll until the spawned mediamtx accepts a real TCP
connection (a bind failure makes it exit immediately, which the poll sees)
and move to the next port if it died.

---

## 15. Docker image anatomy

- **Base:** Alpine + `python3` + `ffmpeg` + the mediamtx release binary.
  No pip dependencies — stdlib only, by design.
- **Config seeding:** the shipped `config.ini` is baked at
  `/app/defaults/config.ini` — a path no volume mounts over. On start,
  `entrypoint.sh` copies it into `/app/config/` if no config exists there.
  This is why the config is mounted as a **directory**: a single-file bind
  mount whose host file is missing makes Docker silently create a
  *directory* at that path, breaking everything; a directory mount lets the
  image self-seed instead.
- **Path pinning:** `VIDEOS_DIR` / `WORKSPACE_DIR` env vars pin the media
  paths to `/app/*` regardless of where the config lives (§5).
- **Healthcheck:** healthy ⇔ the bound RTSP port (read from
  `workspace/.port`, written by `streamer.py` once mediamtx is up) accepts a
  TCP connection — with a fallback that counts a live `streamer.py` process
  as healthy, so a long first-run prepare phase (minutes of transcoding
  before any port opens) never flaps the container unhealthy.
- **Host networking** in the reference compose file: RTSP over bridge
  networking would need explicit port publishing for TCP *and* the UDP
  ranges; host mode sidesteps all of it and lets the first-free-port scheme
  work across instances.

---

## 16. The loader (`loader.py`)

Cosmetic, prod-mode only, and animated whenever stdout is a **TTY**
(`isatty()`). In the image that is *always*: the entrypoint runs streamer.py
under its own pseudo-terminal (`ptyrun.py` — a ~60-line pty wrapper that
copies output verbatim, forwards SIGTERM for graceful `docker stop`, sets a
fixed 24×80 winsize, and execs directly when a real terminal is already
attached). So the block animates in place under `docker compose up` with no
`tty:` line in anyone's compose file. The display is structured exactly like
**`docker compose pull`**: a summary header that is always the block's first
line, then **one fixed row per folder** that changes state in place —
`- Waiting` → `⠸ k/n Preparing` → `✔ Ready` — just as pull's layer rows go
Waiting → Downloading → Pull complete:

```
rtsp_streamer  | [prepare] processing 70 videos... (details: workspace/streamer.log)
rtsp_streamer  |
rtsp_streamer  | ⠸ rtsp-streamer v2.9.0 [⣿⣶⡀] 23/70 Preparing      51.4s
rtsp_streamer  |   ✔ ids Ready (3 videos)                          49.3s
rtsp_streamer  |   ⠸ tfa 2/5 Preparing dalma_fhd                    9.4s
rtsp_streamer  |   ⠸ mini 16/61 Preparing                          31.0s
```

Nothing is ever printed above the header, so it cannot sink as folders
finish; the block is 1 + #folders lines tall, so it fits the pty's 24 rows
even with hundreds of videos (folders stay few). `stop()` paints one final
all-✔ frame that stays put in the scrollback — the record of the run,
docker-pull style — followed by the URL listing. **Without any TTY**
(running streamer.py directly with piped output, CI) the animation never
starts and prod prints a plain braille bar as append-only snapshot lines
(`loader.snapshot()`, no ANSI codes), one per prepared video.

Design notes that matter to a reader:

- Runs on a daemon thread; `set_items()` / `item_start()` / `item_done()`
  are the API (all lock-protected) — streamer declares the ordered video
  list once, then each prepare worker marks its item running/done. Videos
  sharing a folder share one row; a *group* key (the numbered mini mounts)
  makes the row count clips without naming the current one, and its final
  line reads `✔ mini Ready (61 clips)` — the per-index map lives in
  `workspace/<folder>/manifest.txt`. `start()` returns a handle;
  `stop(handle)` joins the thread.
- **Compose-prefix safety.** Compose prefixes every `\n`-terminated chunk
  with `rtsp_streamer  | `; a redraw that starts with `\r` + erase-to-EOL
  wipes that prefix and leaves a bare gutter (the pre-2.9 bug). So every
  block line ends in a real newline (compose prefixes it) and redraws
  never touch the prefix columns: the cursor jumps straight to column
  `INDENT`+1 (`\033[18G`) and erases only rightward. Compose's genuine
  prefix survives every frame; nothing fake is painted. On a bare terminal
  the same columns are simply an empty margin.
- **The header bar is docker pull's**, not a left-to-right meter: one
  braille cell per folder, and each folder's own cell **rises bottom-up**
  (`⠀⡀⣀⣄⣤⣦⣶⣷⣿`, 8 dot-steps) with that folder's done/total —
  `[⣿⣶⡀]` reads folder 1 done, folder 2 ~70 %, folder 3 just started.
  A cell crawls toward its real position (`CATCHUP_FRAC` of the gap per
  frame) and while a slow video holds progress it creeps upward, capped at
  `CREEP_CAP` (90 %) of the in-progress video's share — a cell never
  claims work that isn't done. Monotonic per folder. (The empty track is
  the blank braille cell `⠀`, U+2800 — same width as `⣿`, so geometry
  never shifts.)
- Rows follow the compose-pull rhythm: dim `-` Waiting, spinner + ticking
  elapsed while preparing (ungrouped rows also name the video currently
  being worked on), green `✔` + frozen elapsed when ready. Elapsed is
  right-aligned at the terminal edge; long names truncate with `…` so the
  column never breaks. Should folders ever outnumber the screen, trailing
  Waiting rows collapse into one `- N folders Waiting` row.
- Each redraw rewrites the block in place (cursor-up between frames,
  column-addressed erase per line). Terminal width is re-read every frame;
  if the terminal *narrows* mid-run the old block may have wrapped
  (cursor-up counts would lie), so it is abandoned in place and a fresh
  block starts below — resize-safe. The baked pty is fixed at 80 columns,
  the floor virtually every real terminal meets, so frames never wrap on
  the viewer's side. Terminals narrower than the block fall back to a
  one-line spinner.
- The cursor is hidden while running and always restored in a `finally`.

---

## 17. Logging & modes

| | `MODE=prod` (default) | `MODE=dev` |
|---|---|---|
| stdout | banner, loader progress ticker, URL list | everything |
| details (`log()`) | appended to `workspace/streamer.log`, timestamped | stdout |
| child process output | `workspace/streamer.log` | stdout |
| mediamtx log level | `error` | `warn` |

`log()` is detail logging; `say()` is user-facing and always stdout.

---

## 18. Troubleshooting

| Symptom | Likely cause | Where to look |
|---|---|---|
| "No usable port among […]" | 3 instances already running, or mediamtx crashed on startup | `workspace/mediamtx_*.yml`, `streamer.log`; §14 |
| A stream restarts every ~30 s | its prepared file is bad → publisher crash-loops in backoff | `streamer.log` for the ffmpeg stderr |
| Client sees black on a normal stream | it's a probe stream — check the URL banner for the `(probe — …)` marker | `touch workspace/start` |
| Probe never fires | signal file outside the feeder's `--signal-file` path (custom workspace?), or the touch predates feeder start (it arms against the current mtime) | re-touch; `streamer.log` |
| Stream URL 404s / name looks mangled | source filename had characters mediamtx forbids; it was sanitized, possibly deduped (`__2`) | startup log lines `name collision:` / `duplicate mount` |
| `streamer.log` shows "stalled (no data …s) — killing" | the delivery watchdog caught a hung publisher and restarted it; occasional entries are the system healing itself, repeated ones for the same mount point at a bad file or an overloaded host | §12; the ffmpeg stderr around it |
| First run very slow | transcodes (codec not h264/hevc, or fps/size/bitrate set) — subsequent runs hit the cache | `streamer.log` `transcoding [reasons]` lines |
| Changed config, nothing rebuilt | artifacts are cached by setting-tagged names, mtime and the `.meta` build stamp; `fps/size/bitrate` changes rename the artifact and `keyint` changes invalidate the stamp — both rebuild automatically | `force_reencode = true` once, or delete `workspace/` |
| Corrupt-looking loop on shifted copies | an old pre-2.3.1 truncated cache file | delete `workspace/` |
| Engine logs `reference picture missing during reorder` / `mmco: unref short failure` | B-frames reached the reader: pre-2.3.2 cached files (auto-rebuilt on the next 2.3.2 start) or a pre-2.3.2 image. A short burst (<2 s) right at connect on older versions was the mid-GOP join; 2.3.2's camera-grade encode bounds it to a silent ≤2 s wait | §7.2; rerun so the cache migrates |

---

## 19. Glossary

| Term | Meaning |
|---|---|
| **RTSP** | Control protocol for live streams; what IP cameras speak. |
| **RTP/RTCP** | The data/feedback packets under RTSP when UDP transport is used. |
| **mediamtx** | The RTSP server binary this project drives. |
| **publisher / reader** | The one source pushing a stream to the server / the many clients pulling it. |
| **mount / path** | The `/folder/name` part of an RTSP URL. |
| **remux** | Re-wrapping encoded frames into another container without re-encoding (`-c copy`). Lossless, fast. |
| **transcode** | Decode + re-encode. Lossy, slow, sometimes unavoidable. |
| **MPEG-TS** | The container used for all prepared files; the one that loops cleanly. |
| **GOP** | Group of pictures: one keyframe plus the delta frames until the next. |
| **IDR** | A keyframe that resets all decoder state; the only safe splice point. |
| **open-GOP** | A keyframe after which frames may still reference earlier data; unsafe to splice at. |
| **PTS / DTS** | Presentation / decode timestamps; DTS must never decrease within a stream. |
| **yuv420p** | Raw pixel format; `w·h·1.5` bytes per frame. |
| **body / forward rotation** | The two-step build that lets copies start at arbitrary frames with stream-copies only (§8). |
| **probe / feeder** | Black-until-signal stream / the process implementing it (§10). |
| **mini** | The freeze + clip + black 1-minute composite for short clips (§9). |
| **workspace** | The cache directory; everything in it is regenerable. |
