<!-- Shared README: shown on GitHub and pushed to Docker Hub as the repo
     overview with: ./dockerhub.sh push-readme -->

# RTSP Streamer

[![CI](https://github.com/mikayelbag/rtsp-streamer/actions/workflows/ci.yml/badge.svg)](https://github.com/mikayelbag/rtsp-streamer/actions/workflows/ci.yml)
[![Docker Image Version](https://img.shields.io/docker/v/mikayelbag/rtsp-streamer?sort=semver&label=docker%20hub)](https://hub.docker.com/r/mikayelbag/rtsp-streamer)
[![Image Size](https://img.shields.io/docker/image-size/mikayelbag/rtsp-streamer/latest)](https://hub.docker.com/r/mikayelbag/rtsp-streamer)
[![Docker Pulls](https://img.shields.io/docker/pulls/mikayelbag/rtsp-streamer)](https://hub.docker.com/r/mikayelbag/rtsp-streamer)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

**Image:** [`mikayelbag/rtsp-streamer`](https://hub.docker.com/r/mikayelbag/rtsp-streamer) ·
**Source:** [github.com/mikayelbag/rtsp-streamer](https://github.com/mikayelbag/rtsp-streamer)

**Turn a folder of videos into looping live RTSP streams** — a self-contained test
source for benchmarking video-analytics engines. Point your engine
at the printed `rtsp://` URLs and it sees stable, seamlessly-looping cameras.

For each video this is the equivalent of:

```bash
ffmpeg -re -stream_loop -1 -i file.mp4 -c copy -f rtsp rtsp://localhost:41000/mystream
./mediamtx
```

…but it loops without crashing, simulates phase-shifted cameras, can hold a
stream black until you fire a signal, and loops short clips with a frozen-frame
intro. [MediaMTX](https://github.com/bluenviron/mediamtx)
is the RTSP server; one ffmpeg process publishes each stream; `streamer.py`
prepares the files, generates the config, prints the URLs and supervises
everything (auto-restart with backoff).

```
video_samples/ids/clip.mp4   →   rtsp://<host-ip>:41000/ids/clip
```

---

## Quick start

```bash
mkdir -p video_samples/ids workspace config
# drop your .mp4 / .mkv / … into video_samples/ids/

docker run --rm --network host \
  -v "$PWD/video_samples:/app/video_samples:ro" \
  -v "$PWD/workspace:/app/workspace" \
  -v "$PWD/config:/app/config" \
  mikayelbag/rtsp-streamer:latest
```

First run drops a documented **`config/config.ini`** next to you — edit it and
restart to change anything. The container prints the ready stream URLs.

### docker-compose

```yaml
services:
  streamer:
    image: mikayelbag/rtsp-streamer:latest
    container_name: rtsp_streamer
    restart: unless-stopped
    network_mode: host          # binds first free port of 41000/41001/41002
    environment:
      MODE: prod                # prod = clean output | dev = full logs
    volumes:
      - ./video_samples:/app/video_samples:ro
      - ./workspace:/app/workspace
      - ./config:/app/config    # default config.ini seeded here on first run
```

```bash
docker compose up
```

---

## Configuration

Everything lives in **`config/config.ini`** — a global `[streamer]` block plus
optional per-folder `[folder:name]` and per-video `[video:folder/name]`
overrides. Key settings:

| Key | Default | Meaning |
|---|---|---|
| `ports` | `41000,41001,41002` | first free port is used (run 3 instances side by side) |
| `transport` | `tcp` | `tcp` \| `udp` \| `both` |
| `keyint` | `60` | keyframe interval in frames (global) — worst-case client join wait is `keyint/fps`; lower = faster joins, slightly higher bitrate |
| `advertise_ip` | auto | IP shown in the printed URLs (blank = auto-detect) |
| `copies` | `1` | N parallel streams from one file |
| `shift_frames` | `0` | phase offset between copies — simulate out-of-phase cameras |
| `fps` / `size` / `bitrate_kbps` | `keep` | re-encode targets (`keep` = stream as-is) |
| `force_reencode` | `false` | always normalise to H.264, 2 s keyframes |
| `camera_grade` | `false` | `true` = re-encode sources with B-frames / long GOP so streams behave exactly like real IP cameras; `false` (default) = serve them as-is — faster first run, the URL gets a `(!)` marker, mid-stream joins may glitch |
| `probe` | `false` | black until a signal, then play once (see below) |
| `mini` | `false` | short-clip 1-minute loop: frozen first frame 10 s + clip + black; streams mount as numbers `/mini/1…N` (see below) |
| `concat` | `false` | chain a folder's videos into ONE looping stream `/<folder>/chain` (see below) |

### Volumes

| Mount | Purpose |
|---|---|
| `/app/video_samples` | source videos, one subfolder per group (mount `:ro`) |
| `/app/workspace` | transcode cache + `streamer.log` + the probe signal file |
| `/app/config` | **config directory** — default `config.ini` seeded on first run |

> The config is mounted as a **directory**, not a single file. A single-file
> bind-mount fails the moment the host file is missing (Docker silently creates
> a *directory* in its place); a directory mount lets the image self-seed a
> working default instead.

### Environment

| Var | Default | Meaning |
|---|---|---|
| `MODE` | `prod` | `prod` = progress display + URL list (details → `workspace/streamer.log`) · `dev` = full logs on stdout. Progress animates in place, `docker compose pull` style: a pinned summary header whose bar has one braille cell per folder rising with that folder's own progress, then one `- Waiting` → `⠸ Preparing` → `✔ Ready` row per folder with elapsed times. The image runs the streamer on its own pseudo-terminal, so it works under compose with no `tty:` line needed, and compose's `rtsp_streamer \|` log prefix stays intact on every line. |
| `ADVERTISE_IP` | auto | overrides `advertise_ip` from config |

### Ports

`41000 41001 41002` (RTSP/TCP). With `transport=udp`, RTP/RTCP also use
`port+10000` / `port+10001`. The mediamtx API used by the delivery watchdog
binds `port+2000` on **127.0.0.1 only** — never reachable from outside the
host.

---

## Probe mode — black until you fire it

Set `probe = true` (globally, per folder, or per video) and that stream shows a
**black screen** until you signal it, then plays the content **once** and returns
to black, re-armed. For engine cold-start timing on an already-connected stream.

```bash
# fire every armed probe stream at once:
touch workspace/start
# re-touch to fire again
```

- **Seamless** — a per-stream feeder owns one continuous publish session and
  feeds it raw frames (black → content → black), so the engine **never
  disconnects** at the switch.
- **Decoupled from clients** — only the `touch` fires it. A client (or a
  stranger) connecting to the RTSP URL never triggers it and never disturbs the
  engine's view. One touch fires all probes; touches during a pass coalesce to a
  single replay afterwards.
- **Shift-aware** — honours `copies`/`shift_frames`: at the signal, copy `i`
  starts at frame `i·shift`.

---

## Mini mode — short clips on a 1-minute loop

Set `mini = true` for short side-clips (a few seconds long). Each becomes a
**1-minute loop**:

```
[ first frame frozen 10s ] → [ the clip, once ] → [ black filling the rest of the minute ]
```

The composite is built once (one uniform H.264 encode) and cached, then looped
seamlessly like any other stream. Mini streams mount as **numbers** in scan
order — `rtsp://…/mini/1`, `/mini/2`, … — and `workspace/mini/manifest.txt`
maps each number back to its file. The shipped config enables it for a
`mini/` folder:

```ini
[folder:mini]
mini = true
```

Drop clips into `video_samples/mini/` and they pick it up. The folder is
optional — if it doesn't exist there simply are no mini streams, no error.

- **Composes with probe** — `mini = true` + `probe = true` together: the stream
  shows black until you `touch workspace/start`, then plays
  black → frozen frame → clip → black. (In probe the trailing black is the
  feeder returning to black, so the composite omits the fixed black tail.)
- **Long clips** — if a clip plus the 10 s freeze already exceeds a minute the
  black tail is dropped and the loop simply runs a little past 60 s.
- **Ignores `shift_frames`** — mini reshapes the whole clip, so phase offset
  doesn't apply.

---

## Concat chains — many videos, one stream

Set `concat = true` for a folder and ALL of its videos chain into **one
looping stream** at `rtsp://…/<folder>/chain`. Per video:

```
[ first frame frozen 3s ] → [ the clip ] → [ last frame frozen 3s ] → [ 2s black ] → next video
```

Each video's index is burned into its top-left corner (drawtext), and
`workspace/<folder>/manifest.txt` maps index → file + where it starts inside
the loop. Mixed sources are normalised to `size`/`fps` (1920×1080 @ 30 when
`keep`). The shipped config enables it for a `concat/` folder — optional,
absent folder = no chain, no error:

```ini
[folder:concat]
concat = true
```

---

## Consuming the streams

Verified consumer patterns (measured against v2.3.2 — exact 30.00 fps
delivery, max 41 ms inter-frame jitter, loop wraps invisible):

**HLS recorder** (`-c copy`, 2 s segments) — works as-is; segments come out a
uniform 2 s because the cutter locks onto the stream's fixed IDR cadence:

```bash
ffmpeg -stimeout 5000000 -use_wallclock_as_timestamps 1 -rtsp_transport tcp \
  -i rtsp://<host>:41000/ids/clip -c:v copy -an \
  -f hls -hls_time 2 -hls_list_size 1800 \
  -hls_flags append_list+delete_segments+program_date_time+omit_endlist+discont_start+temp_file \
  -hls_segment_type mpegts -hls_segment_filename 'rec_%05d.ts' rec.m3u8
```

**Fast joins** — time-to-first-picture after connect is `keyint/fps` worst
case (2 s at defaults) plus your client's own probing. To cut the client
side, use:

```bash
ffmpeg -probesize 32k -analyzeduration 0 -flags low_delay -rtsp_transport tcp -i rtsp://...
```

and/or lower `keyint` in the config (e.g. `keyint = 30` → 1 s worst case).

**Reconnect supervision** — exactly like a real camera, a stream restart
(container restart, watchdog recovery) drops the RTSP session; wrap your
consumer in a retry loop. The HLS flags above (`append_list+discont_start`)
already make a restarted recorder resume its playlist correctly.

---

## Behaviour

- **Seamless looping** — every video is prepared as MPEG-TS in `workspace/`
  (lossless `-c copy` remux when the source already looks camera-like,
  re-encode otherwise). TS is what makes `-stream_loop -1` loop without
  the MP4 non-monotonic-DTS crash that disconnects every client at the wrap.
  Prepared files are cached and rebuilt only when the source is newer.
- **Camera-grade H.264 (opt-in)** — with `camera_grade = true` streams mimic
  real IP cameras: **no B-frames**, a fixed 2 s IDR cadence, SPS/PPS repeated
  before every keyframe; sources that don't already look like that are
  re-encoded instead of remuxed, and a client joining mid-stream waits at
  most one keyframe interval (≤2 s) for a clean picture instead of spraying
  `reference picture missing during reorder` decode errors. By default
  (`camera_grade = false`) such sources are served **as-is** — no re-encode
  on first run — and their URLs carry a `(!)` marker warning that mid-stream
  joins may glitch. Flipping the setting rebuilds the affected cache entries
  automatically.
- **Crash-safe cache** — prepared files are written to a temp name and renamed
  into place atomically, so a container killed mid-transcode never leaves a
  truncated file that would be served on the next run.
- **Parallel prepare** — independent videos are prepared concurrently (small
  worker pool), cutting first-run startup on multi-video sets.
- **Phase-shifted cameras** — `copies` + `shift_frames` build N streams from one
  file, each forward-rotated to start `shift` frames later, frame-exact and with
  no loop drift. Built cheaply (only the first GOP is re-encoded) and cached.
  <sub>(Full mechanism documented inline in `streamer.py`.)</sub>
- **Self-healing** — crashed publishers and probe feeders restart automatically
  (2 s backoff, doubling to 30 s, reset after a stable minute).
- **Delivery watchdog** — a publisher that *hangs* without dying (wedged
  ffmpeg, blocked write) is caught too: the supervisor polls mediamtx's
  per-stream byte counters (API on localhost only) and restarts any live
  stream that has delivered nothing for 15 s.
- **Ready means ready** — the URL banner prints only after every stream is
  confirmed publishing on the server, so an engine pointed at the URLs
  connects on the first try.
- **Scales** — one publisher per stream regardless of client count; mediamtx
  fans out with no re-encoding. `writeQueueSize: 2048` + a 65536 fd limit
  (raised at startup) comfortably covers 100+ concurrent readers per stream.
- **Multi-instance** — host networking + first-free-port means several streamer
  containers coexist on one host with no config changes.
- **Healthcheck** — the container reports healthy while preparing and once the
  bound RTSP port accepts connections; a crashed streamer flips it unhealthy.

Deep-dive documentation of every internal mechanism lives in
[`TECHNICAL.md`](https://github.com/mikayelbag/rtsp-streamer/blob/main/TECHNICAL.md).


---

## Tags

| Tag | What |
|---|---|
| `latest` | the current release |
| `X.Y.Z` (e.g. `2.9.0`) | pinned release versions |

## Security note

There is no RTSP authentication — any host that can reach the port can read the
streams (and publish to an *unused* path). Intended for a trusted test LAN. Currently out of scope but to lock it down, front it with `mediamtx` auth or a firewall rule.

