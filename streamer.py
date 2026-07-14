#!/usr/bin/env python3
"""
RTSP streamer 2.4.0

Serves every video found under the videos folder as a looping live RTSP
stream. For each stream this is the equivalent of:

    ffmpeg -re -stream_loop -1 -i file.mp4 -c copy -f rtsp \
        rtsp://localhost:41000/<folder>/<name>
    ./mediamtx

mediamtx is the RTSP server, one ffmpeg process publishes each stream,
and this script prepares the files, generates the mediamtx config,
supervises all processes and restarts them if they die.

Modes (MODE environment variable):
    prod (default)  loader progress ticker + stream URLs only; details go to
                    workspace/streamer.log
    dev             everything on stdout, no animation

Features on top of the bare ffmpeg command:
  - every video is prepared into the workspace as MPEG-TS (lossless remux
    when possible) so -stream_loop loops seamlessly without the MP4
    non-monotonic-DTS crash that disconnects clients at the loop point
  - transcode only when needed (fps / size / bitrate change, unsupported
    codec, or force_reencode=true); results are cached and reused. Each
    artifact carries a .meta build stamp, so cached runs skip the packet
    index rescans and a `keyint` change rebuilds automatically
  - copies:        N parallel streams from one file
  - shift_frames: phase offset between copies (copy i starts at frame i*shift;
                  forward-rotated, no loop drift — simulates out-of-phase cameras)
  - probe mode:    streams black until `touch workspace/start`, then plays the
                   (phase-shifted) content once and returns to black, re-armed
  - mini mode:     short-clip 1-minute loop = first frame frozen 10s + clip +
                   black; composes with probe (fire -> black -> frozen -> clip
                   -> black). Mini streams mount as numbers (/mini/1, /mini/2,
                   ...) — with many clips the URL is the index; the number ->
                   source map is written to workspace/<folder>/manifest.txt
  - concat mode:   every video in a `concat = true` folder becomes one chained
                   stream: [first frame frozen 3s][clip][last frame frozen 3s]
                   [2s black] per video, normalised to one size/fps, each
                   video's 1-based index burned into its top-left corner.
                   manifest.txt maps index -> source file + start offset
  - per-camera probe fire: each probe stream also has its own signal file
                   (workspace/start_<folder>_<name>), so one camera can be
                   fired without firing all of them
  - port fallback: first free port from the configured list is used, so
                   several instances can share one host
  - parallel prepare: independent videos are prepared by a small worker pool,
                   cutting first-run startup on multi-video sets
  - delivery watchdog: the supervisor polls the mediamtx API (localhost only)
                   and restarts any stream whose publisher is alive but has
                   stopped delivering bytes — hung ffmpeg, not just dead ffmpeg
  - readiness:     the URL banner prints only after every stream is confirmed
                   publishing, so "ready" means ready
  - camera-grade H.264: every stream leaves with no B-frames, a fixed 2 s IDR
                   cadence and repeated SPS/PPS — like a real IP camera.
                   Sources that don't match (B-frames, long GOP) are
                   re-encoded instead of remuxed; old cached artifacts are
                   detected and rebuilt automatically
"""

import argparse
import configparser
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
from concurrent.futures import ThreadPoolExecutor

VERSION = "2.5.0"
SIGNAL_FILE = "start"       # touch workspace/start to fire armed probe streams
PORT_FILE = ".port"         # bound RTSP port, written for the Docker healthcheck
X264_PRESET = "veryfast"
# Camera-grade H.264 for every encode. Real IP cameras send no B-frames and a
# fixed 1-2 s IDR cadence; a reader joining mid-GOP on a B-frame stream sprays
# "reference picture missing during reorder" / "mmco: unref short failure"
# until the next keyframe. bframes=0 removes the reorder errors entirely,
# scenecut=0 + min-keyint pin the IDR cadence exactly like a camera, and
# repeat-headers=1 puts SPS/PPS before every IDR so a mid-stream join can
# configure the decoder without relying on the SDP.
KEYINT = None               # set by _set_keyint below; config key `keyint`
X264_PARAMS = None


def _set_keyint(n):
    """KEYINT is the join-latency lever: a client can only start decoding at
    an IDR, so worst-case wait for a picture after connect = keyint/fps (plus
    the client's own probing). 60 @ 30 fps = camera-typical 2 s; 30 halves
    the join wait at a slightly higher bitrate on re-encoded streams."""
    global KEYINT, X264_PARAMS
    KEYINT = n
    X264_PARAMS = ("keyint={0}:min-keyint={0}:scenecut=0:bframes=0:"
                   "repeat-headers=1").format(n)


_set_keyint(60)
MINI_FREEZE_S = 10          # mini: hold the first frame this many seconds
MINI_LOOP_S = 60            # mini: total regular-mode loop (freeze + clip + black)
CONCAT_FREEZE_S = 3         # concat: freeze first/last frame this many seconds
CONCAT_GAP_S = 2            # concat: black gap between videos
CONCAT_DEFAULT_SIZE = "1920x1080"   # concat normalisation targets used when
CONCAT_DEFAULT_FPS = "30"           # the folder's size/fps are left at "keep"
MANIFEST_NAME = "manifest.txt"      # index -> source map (mini & concat folders)
ALLOWED_CODECS = {"h264", "hevc"}
VIDEO_EXTS = (".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v")
RESTART_BACKOFF_START = 2.0
RESTART_BACKOFF_MAX = 30.0
STABLE_RESET_SECONDS = 60.0
WATCHDOG_POLL_SECONDS = 5.0   # how often the supervisor samples the mediamtx API
STALL_SECONDS = 15.0          # live publisher, no bytes into mediamtx this long -> restart it
READY_WAIT_SECONDS = 15.0     # max wait for all streams to publish before the URL banner
LOG_MAX_BYTES = 5 * 1024 * 1024   # rotate streamer.log past this size (one .1 kept)

DEV = (os.environ.get("MODE") or "prod").strip().lower() == "dev"
LOG_FH = None  # opened in main() once the workspace exists (prod mode)


def log(msg):
    """Detail logging: stdout in dev, workspace/streamer.log in prod."""
    line = "[streamer] {}".format(msg)
    if DEV or LOG_FH is None:
        print(line, flush=True)
    else:
        LOG_FH.write(time.strftime("%Y-%m-%d %H:%M:%S ") + line + "\n")
        LOG_FH.flush()


def say(msg=""):
    """User-facing output: always stdout."""
    print(msg, flush=True)


# ---------------------------------------------------------------- config ----

SETTING_KEYS = ("copies", "shift_frames", "fps", "size", "bitrate_kbps", "probe",
                "force_reencode", "mini", "concat", "camera_grade")

BOOL_KEYS = ("probe", "force_reencode", "mini", "concat", "camera_grade")
BOOL_WORDS = ("1", "true", "yes", "on", "0", "false", "no", "off")


def load_config(path):
    if not os.path.isfile(path):
        sys.exit("Config file not found: {}".format(path))
    cfg = configparser.ConfigParser(
        comment_prefixes=(";", "#"),
        inline_comment_prefixes=(";", "#"),
        interpolation=None,
    )
    cfg.read(path)
    if not cfg.has_section("streamer"):
        sys.exit("Config must have a [streamer] section: {}".format(path))
    validate_config(cfg)
    return cfg


def validate_config(cfg):
    """Fail fast, at startup, with a clear message on any malformed value.
    Previously a bad int surfaced as a mid-prepare traceback inside a worker
    thread (stream silently lost), and a malformed `size` was silently
    ignored by transcode()."""
    for sec in cfg.sections():
        if sec != "streamer" and not sec.startswith(("folder:", "video:")):
            continue
        for key in SETTING_KEYS:
            if not cfg.has_option(sec, key):
                continue
            v = cfg.get(sec, key).strip()
            if not v:
                continue
            err = None
            if key in ("copies", "shift_frames"):
                if not re.fullmatch(r"-?\d+", v):
                    err = "must be an integer"
            elif key in BOOL_KEYS:
                if v.lower() not in BOOL_WORDS:
                    err = "must be a boolean (true/false)"
            elif key == "fps":
                if v.lower() != "keep":
                    try:
                        ok = float(v) > 0
                    except ValueError:
                        ok = False
                    if not ok:
                        err = "must be 'keep' or a positive number"
            elif key == "size":
                if v.lower() != "keep" and not re.fullmatch(r"\d+x\d+", v.lower()):
                    err = "must be 'keep' or WIDTHxHEIGHT (e.g. 1920x1080)"
            elif key == "bitrate_kbps":
                if v.lower() != "keep" and not re.fullmatch(r"\d+", v):
                    err = "must be 'keep' or an integer (kbps)"
            if err:
                sys.exit("Config error: [{}] {} = {} — {}".format(sec, key, v, err))


def parse_ports(raw):
    """[streamer] ports -> list of ints, or a clean startup error. A typo'd
    port previously surfaced as a bare int() traceback."""
    ports = []
    for tok in raw.replace(" ", "").split(","):
        if not tok:
            continue
        if not re.fullmatch(r"\d+", tok) or not 1 <= int(tok) <= 65535:
            sys.exit("Config error: ports must be comma-separated port numbers "
                     "1-65535 (got: {})".format(raw))
        ports.append(int(tok))
    if not ports:
        sys.exit("Config error: ports is empty")
    return ports


def apply_section(cfg, sec, s):
    if not cfg.has_section(sec):
        return
    for key in SETTING_KEYS:
        if not cfg.has_option(sec, key):
            continue
        v = cfg.get(sec, key).strip()
        if not v:          # blank value in config = leave default untouched
            continue
        if key in ("copies", "shift_frames"):
            s[key] = int(v)
        elif key in BOOL_KEYS:
            s[key] = v.lower() in ("1", "true", "yes", "on")
        else:
            s[key] = v.lower()


def settings_for(cfg, folder, stem):
    """Merge [streamer] <- [folder:x] <- [video:x/y] into one dict."""
    s = {
        "copies": 1,
        "shift_frames": 0,
        "fps": "keep",
        "size": "keep",
        "bitrate_kbps": "keep",
        "probe": False,
        "force_reencode": False,
        "mini": False,
        "concat": False,
        # default OFF since 2.5.0: sources with B-frames / long GOP are served
        # as-is (fast first run, (!) marker in the banner); set true to force
        # strict real-camera H.264 everywhere
        "camera_grade": False,
    }
    apply_section(cfg, "streamer", s)
    apply_section(cfg, "folder:{}".format(folder), s)
    apply_section(cfg, "video:{}/{}".format(folder, stem), s)
    return s


# ----------------------------------------------------------------- media ----

def ffprobe_info(path):
    """(codec, width, height, fps, has_b_frames) of the first video stream,
    or None. has_b_frames is the reorder-buffer depth: 0 = no B-frames."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries",
             "stream=codec_name,width,height,avg_frame_rate,has_b_frames",
             "-of", "json", path],
            capture_output=True, text=True, timeout=30,
        )
        st = (json.loads(r.stdout or "{}").get("streams") or [None])[0]
        if not st:
            return None
        num, _, den = (st.get("avg_frame_rate") or "30/1").partition("/")
        fps = float(num) / float(den or 1) if float(den or 1) else 30.0
        return (st.get("codec_name", ""), int(st.get("width", 0)),
                int(st.get("height", 0)), fps, int(st.get("has_b_frames") or 0))
    except Exception:
        return None


def scan_videos(videos_dir):
    out = []
    for d, _, files in os.walk(videos_dir):
        for f in sorted(files):
            if f.lower().endswith(VIDEO_EXTS):
                out.append(os.path.join(d, f))
    return sorted(out)


def sanitize(name):
    """Make a name safe for a mediamtx path segment and the workspace filename.

    mediamtx rejects any RTSP path that isn't [alphanumeric . _ ~ - /], so a
    source like 'Passenger Dwell Time (2).mp4' crashes the server on startup
    (rc=1) — which the port loop then misreads as 'all ports busy'. We replace
    spaces with '_' and every other illegal char with '_', collapse repeats,
    and trim leading/trailing '_'. The prepared workspace file is named from
    the same sanitized stem so the file and the RTSP mount always match.
    """
    name = name.replace(" ", "_")
    name = re.sub(r"[^A-Za-z0-9._~-]", "_", name)
    name = re.sub(r"_+", "_", name).strip("_")
    return name or "stream"


def folder_and_stem(path, videos_dir):
    rel = os.path.relpath(path, videos_dir)
    parent = os.path.dirname(rel)
    folder = os.path.basename(parent) if parent not in ("", ".") else "root"
    stem = os.path.splitext(os.path.basename(path))[0]
    return sanitize(folder), sanitize(stem)


def needs_transcode(path, setts, info):
    """(reasons, warnings) for this source. Reasons force a re-encode
    (compatibility: settings, unreadable, non-h264/hevc codec — no real
    camera or engine speaks e.g. MPEG-4 Part 2 over RTSP). Camera-realism
    findings (B-frames / long GOP — see X264_PARAMS) are reasons only with
    camera_grade=true; by default (false since 2.5.0) they are warnings: the
    source is served as-is and its URL gets a (!) marker in the banner."""
    reasons, warns = [], []
    if setts["force_reencode"]:
        reasons.append("forced")
    if setts["fps"] != "keep":
        reasons.append("fps")
    if setts["size"] != "keep":
        reasons.append("size")
    if setts["bitrate_kbps"] != "keep":
        reasons.append("bitrate")
    if info is None:
        reasons.append("unreadable")
    elif info[0] not in ALLOWED_CODECS:
        reasons.append("codec={}".format(info[0]))
    else:
        # realism checks only when nothing above already forces an encode
        # (the GOP scan reads the whole packet index)
        realism = []
        if info[4]:
            realism.append("bframes")
        if not reasons and not realism and _gop_too_long(path, info[3]):
            realism.append("gop")
        if setts["camera_grade"]:
            reasons += realism
        else:
            warns = realism
    return reasons, warns


def _gop_too_long(path, fps):
    """True when keyframes sit farther apart than real cameras place them.
    A reader joining mid-stream decodes garbage until the next keyframe, so a
    long (or absent) IDR cadence turns every connect into seconds of decode
    errors; 2x KEYINT is the tolerance so camera-like sources still remux."""
    fps = fps or 30.0
    limit = 2.0 * KEYINT / fps            # seconds
    kts = _keyframe_times(path)
    if not kts:
        return False                       # unreadable is caught elsewhere
    gaps = [b - a for a, b in zip(kts, kts[1:])]
    if gaps:
        return max(gaps) > limit
    # single keyframe: fine for a short clip, a problem for a long file
    n = frame_count(path)
    return bool(n) and n / fps > limit


def _grade_warnings(path):
    """Camera-realism findings for a file: [] when it already looks like
    real-camera H.264 (no B-frames, regular keyframe cadence), otherwise the
    list of issues. Used by is_fresh() to verify (once) cached artifacts that
    predate the build stamp — pre-X264_PARAMS artifacts carry B-frames and
    get rebuilt instead of served, so an old workspace migrates itself."""
    info = ffprobe_info(path)
    if info is None:
        return ["unreadable"]
    warns = []
    if info[4]:
        warns.append("bframes")
    if _gop_too_long(path, info[3]):
        warns.append("gop")
    return warns


def transcoded_name(stem, setts):
    tags = []
    if setts["size"] != "keep":
        tags.append("r{}".format(setts["size"]))
    if setts["fps"] != "keep":
        tags.append("f{}".format(setts["fps"]))
    if setts["bitrate_kbps"] != "keep":
        tags.append("b{}k".format(setts["bitrate_kbps"]))
    if setts["force_reencode"]:
        tags.append("enc")
    return "{}__{}.ts".format(stem, "_".join(tags)) if tags else "{}.ts".format(stem)


def _rm(path):
    try:
        os.remove(path)
    except OSError:
        pass


def _pid_alive(pid):
    """True when a process with this pid exists (signal 0 probes without
    touching it). Used by the startup temp sweep to leave another live
    instance's in-progress .part files alone."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return True     # exists but not ours (EPERM) — leave it alone


def _tmp_for(dst):
    """Build outputs land in a temp file that is atomically renamed into place
    on success. A build killed mid-write (e.g. docker stop during a first-run
    transcode) would otherwise leave a truncated .ts whose fresh mtime passes
    the cache check — and get served, corrupt, on every following run. The pid
    suffix keeps two instances sharing one workspace off each other's temp
    (threads within one instance never share a dst)."""
    return "{}.part{}".format(dst, os.getpid())


STAMP_SUFFIX = ".meta"


def _stamp(dst, warn=None, extra=None):
    """Record next to the artifact how it was built (streamer version +
    keyint). The stamp is what lets is_fresh() trust a cached file without
    re-running the packet-index scan on every startup, and what detects a
    `keyint` config change — which the mtime check and the setting-tagged
    filename both miss. `warn` marks a camera_grade=false remux that kept
    realism issues (B-frames / long GOP) in the file — reused for the URL
    banner marker on cached runs, and it makes the artifact stale the moment
    camera_grade is switched back to true."""
    meta = {"version": VERSION, "keyint": KEYINT}
    if warn:
        meta["warn"] = warn
    if extra:
        meta.update(extra)
    try:
        with open(dst + STAMP_SUFFIX, "w", encoding="utf-8") as f:
            json.dump(meta, f)
    except OSError:
        pass    # unstampable artifact is just re-verified next run


def _read_stamp(dst):
    try:
        with open(dst + STAMP_SUFFIX, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _commit(tmp, dst):
    """Atomically publish a finished build and stamp it."""
    os.replace(tmp, dst)
    _stamp(dst)


def is_fresh(dst, src, setts, check_grade=True):
    """The one cache-validity check for every prepared artifact.

    Fresh = not force_reencode, dst exists and is at least as new as src, and
    the build stamp's keyint matches the config. Artifacts without a stamp
    (built by older versions) are verified once with the packet-index scan
    (_camera_grade) and stamped if they pass — an old workspace migrates
    itself and is never rescanned again. A stamp with a different keyint
    means the cached cadence no longer matches the config -> rebuild
    (previously a documented footgun: keyint edits silently kept serving the
    old cadence until the workspace was wiped).

    check_grade=False is for artifacts derived by pure stream-copy from an
    already-verified file (forward rotations): their grade is inherited, so
    an unstamped one is stamped on the mtime check alone.
    """
    if setts["force_reencode"]:
        return False
    try:
        if os.path.getmtime(dst) < os.path.getmtime(src):
            return False
    except OSError:
        return False                        # dst (or src) missing
    meta = _read_stamp(dst)
    if meta is not None:
        if meta.get("keyint") != KEYINT:
            log("keyint changed ({} -> {}), rebuilding: {}".format(
                meta.get("keyint"), KEYINT, dst))
            return False
        if setts["camera_grade"] and meta.get("warn"):
            log("camera_grade now enforced, rebuilding as-is artifact: {}".format(dst))
            return False
        return True
    if not check_grade:
        _stamp(dst)
        return True
    warns = _grade_warnings(dst)            # one-time scan of unstamped artifacts
    if not warns:
        _stamp(dst)
        return True
    if not setts["camera_grade"] and "unreadable" not in warns:
        _stamp(dst, warn=warns)             # as-is by choice; remember for the banner
        return True
    log("cached file not camera-grade (pre-2.3.2 build), rebuilding: {}".format(dst))
    return False


def transcode(src, dst, setts, info):
    """Re-encode src into dst applying fps/size/bitrate settings."""
    w, h, fps = (info or ("", 1920, 1080, 30.0, 0))[1:4]
    if setts["size"] != "keep" and "x" in setts["size"]:
        w, h = (int(v) for v in setts["size"].split("x", 1))
    if setts["fps"] != "keep":
        fps = float(setts["fps"])
    fps = fps or 30.0

    vf = "fps={},scale={}:{},format=yuv420p".format(fps, w, h)
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
           "-i", src, "-vf", vf,
           "-c:v", "libx264", "-preset", X264_PRESET,
           "-x264-params", X264_PARAMS]
    if setts["bitrate_kbps"] != "keep":
        kb = int(setts["bitrate_kbps"])
        cmd += ["-b:v", "{}k".format(kb), "-maxrate", "{}k".format(kb),
                "-bufsize", "{}k".format(kb * 2)]
    tmp = _tmp_for(dst)
    cmd += ["-an", "-f", "mpegts", tmp]

    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        log("transcode FAILED for {}: {}".format(src, (r.stderr or "").strip()[-400:]))
        _rm(tmp)
        return False
    _commit(tmp, dst)
    return True


def frame_count(path):
    """Video frame count, or 0 if unknown. Counts packets (reads the container
    index) instead of decoding — cheap even for long files."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_packets",
             "-show_entries", "stream=nb_read_packets", "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=120,
        )
        # csv output can carry more than one line; take the first integer.
        toks = (r.stdout or "").split()
        return int(toks[0]) if toks else 0
    except Exception:
        return 0


def _keyframe_times(path):
    """pts_time of each keyframe, read from the packet index (no decode).
    Empty list on any failure (incl. timeout) — _gop_too_long() then reports
    no issue, so an unscannable file is remuxed as-is instead of crashing
    the whole streamer (its cadence is unknown, not known-bad)."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_packets",
             "-show_entries", "packet=pts_time,flags", "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=120,
        )
    except Exception:
        return []
    times = []
    for line in (r.stdout or "").splitlines():
        parts = line.split(",")
        if len(parts) >= 2 and "K" in parts[1]:
            try:
                times.append(float(parts[0]))
            except ValueError:
                pass
    return sorted(times)


def _full_reencode(src, dst, kf_frames):
    """Re-encode the whole video to uniform libx264 with forced IDRs at the
    cut frames — the rotation body build_forward() splits with stream-copies.

    A time-based `-force_key_frames` rounds off by a frame, so the IDRs are
    placed frame-exactly via the `eq(n,k)` expression. One uniform encode
    means every splice point (segment joins and the -stream_loop wrap) shares
    one SPS/PPS and lands on an IDR, so it decodes clean by construction —
    no join verification needed. Built once per video and cached.

    (Earlier versions spliced a re-encoded head onto a stream-copied tail to
    save encode time; that needed a decode-verify pass plus an open-GOP
    fallback to *this* function. A one-time cached prepare step doesn't earn
    that complexity, so the always-correct path is now the only path.)
    """
    tmp = _tmp_for(dst)
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
           "-i", src,
           "-c:v", "libx264", "-preset", X264_PRESET,
           "-x264-params", X264_PARAMS,
           "-pix_fmt", "yuv420p", "-an"]
    if kf_frames:
        cmd += ["-force_key_frames",
                "expr:" + "+".join("eq(n,{})".format(k) for k in kf_frames)]
    cmd += ["-f", "mpegts", tmp]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        log("body re-encode FAILED for {}: {}".format(dst, (r.stderr or "").strip()[-400:]))
        _rm(tmp)
        return False
    _commit(tmp, dst)
    return True


def build_forward(body, dst, k):
    """Forward-rotated copy = frames [k:N] then [0:k], so it starts at frame k.

    `body` has a forced IDR at frame k, so the segment muxer splits it there
    frame-exactly into seg0=[0:k] and seg1=[k:N] with a pure stream-copy (no
    per-copy re-encode). Concatenating seg1+seg0 gives the rotation; the join
    (seg1-end -> seg0-start, an IDR) and the -stream_loop wrap (seg0-end ->
    seg1-start, an IDR) both land on a keyframe, so they decode clean. Length
    stays N -> copies stay `shift` frames apart, no drift.
    """
    seg0, seg1 = dst + ".seg0.ts", dst + ".seg1.ts"
    for f in (seg0, seg1):
        if os.path.exists(f):
            os.remove(f)
    rseg = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
         "-i", body, "-c", "copy", "-an",
         "-f", "segment", "-segment_frames", str(k), "-reset_timestamps", "1",
         dst + ".seg%d.ts"],
        capture_output=True, text=True,
    )
    ok = rseg.returncode == 0 and os.path.isfile(seg0) and os.path.isfile(seg1)
    if not ok:
        log("forward segment FAILED for {}: {}".format(dst, (rseg.stderr or "").strip()[-400:]))
    else:
        listf = dst + ".concat"
        tmp = _tmp_for(dst)
        with open(listf, "w", encoding="utf-8") as f:
            f.write("file '{}'\n".format(os.path.abspath(seg1)))   # [k:N]
            f.write("file '{}'\n".format(os.path.abspath(seg0)))   # [0:k]
        r = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
             "-f", "concat", "-safe", "0", "-i", listf,
             "-c", "copy", "-an", "-f", "mpegts", tmp],
            capture_output=True, text=True,
        )
        if os.path.exists(listf):
            os.remove(listf)
        ok = r.returncode == 0
        if ok:
            _commit(tmp, dst)
        else:
            log("forward concat FAILED for {}: {}".format(dst, (r.stderr or "").strip()[-400:]))
            _rm(tmp)
    for f in (seg0, seg1):
        if os.path.exists(f):
            os.remove(f)
    return ok


def remux_to_ts(src, dst):
    """Lossless remux into MPEG-TS — required for clean infinite looping.

    MP4 edit lists / B-frame reordering produce a non-monotonic DTS at the
    -stream_loop boundary which kills the ffmpeg publisher (and disconnects
    every client). TS has no such structure and loops seamlessly.
    """
    tmp = _tmp_for(dst)
    r = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
         "-i", src, "-c", "copy", "-an", "-f", "mpegts", tmp],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        log("remux FAILED for {}: {}".format(src, (r.stderr or "").strip()[-400:]))
        _rm(tmp)
        return False
    _commit(tmp, dst)
    return True


# ---------------------------------------------------------------- mini ------

def build_mini(src, dst, info, with_tail_black):
    """Build a 'mini' composite from a short clip: hold its first frame for
    MINI_FREEZE_S seconds, then play the clip once.

    Regular mode (`with_tail_black=True`) appends black so the whole thing is
    MINI_LOOP_S long (a 1-minute loop = freeze + clip + black); if the clip is
    already too long for the loop the black tail is clamped to nothing (the loop
    just runs past a minute). Probe mode omits the tail — the feeder returns to
    black after the clip, which is the trailing black.

    Done in one uniform libx264 encode via ffmpeg `tpad`: start_mode=clone holds
    frame 0, stop_mode=add pads black at the end. libx264 opens on an IDR and the
    `-g KEYINT` cadence keeps the -stream_loop wrap on a keyframe, so it loops
    seamlessly the same way a re-encoded body does.
    """
    w, h, fps = info[1:4]
    fps = fps or 30.0
    nframes = frame_count(src)
    dur = nframes / fps if (nframes and fps) else 0.0

    vf = "tpad=start_duration={}:start_mode=clone".format(MINI_FREEZE_S)
    if with_tail_black:
        black = MINI_LOOP_S - MINI_FREEZE_S - dur
        if black > 0.05 and dur > 0:
            vf += ":stop_duration={:.3f}:stop_mode=add:color=black".format(black)
        else:
            log("mini: {} clip {:.1f}s + {}s freeze >= {}s loop — no black tail"
                .format(src, dur, MINI_FREEZE_S, MINI_LOOP_S))

    tmp = _tmp_for(dst)
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
           "-i", src, "-vf", vf,
           "-c:v", "libx264", "-preset", X264_PRESET,
           "-x264-params", X264_PARAMS,
           "-pix_fmt", "yuv420p", "-an", "-f", "mpegts", tmp]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        log("mini build FAILED for {}: {}".format(dst, (r.stderr or "").strip()[-400:]))
        _rm(tmp)
        return False
    _commit(tmp, dst)
    return True


# --------------------------------------------------------------- concat -----

def find_font():
    """A bold TTF for the concat index overlay, or None (drawtext then falls
    back to fontconfig's default; the Docker image ships font-dejavu)."""
    for p in ("/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",            # alpine
              "/usr/share/fonts/ttf-dejavu/DejaVuSans-Bold.ttf",        # old alpine
              "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",   # debian
              "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf"):              # arch
        if os.path.isfile(p):
            return p
    return None


def build_concat_segment(src, dst, index, w, h, fps):
    """One chain segment: [first frame frozen CONCAT_FREEZE_S] + clip +
    [last frame frozen CONCAT_FREEZE_S] + [CONCAT_GAP_S black], normalised to
    w x h @ fps (letterboxed, aspect kept) with the 1-based index burned into
    the top-left corner. drawtext sits *before* the tpads, so both frozen
    frames carry the number while the appended black gap stays clean. One
    uniform X264_PARAMS encode per segment -> the chain is joined with a pure
    stream-copy and loops like any other prepared file."""
    fs = max(24, h // 15)
    draw = ("drawtext={}text='{}':x=24:y=24:fontsize={}:fontcolor=white:"
            "borderw={}:bordercolor=black").format(
        "fontfile={}:".format(find_font()) if find_font() else "",
        index, fs, max(2, fs // 24))
    vf = ("scale={w}:{h}:force_original_aspect_ratio=decrease,"
          "pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,fps={fps},format=yuv420p,{draw},"
          "tpad=start_duration={fz}:start_mode=clone"
          ":stop_duration={fz}:stop_mode=clone,"
          "tpad=stop_duration={gap}:stop_mode=add:color=black").format(
              w=w, h=h, fps=fps, draw=draw,
              fz=CONCAT_FREEZE_S, gap=CONCAT_GAP_S)
    tmp = _tmp_for(dst)
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
           "-i", src, "-vf", vf,
           "-c:v", "libx264", "-preset", X264_PRESET,
           "-x264-params", X264_PARAMS,
           "-pix_fmt", "yuv420p", "-an", "-f", "mpegts", tmp]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        log("concat segment FAILED for {}: {}".format(src, (r.stderr or "").strip()[-400:]))
        _rm(tmp)
        return False
    _commit(tmp, dst)
    return True


def prepare_concat(cfg, workspace, folder, items):
    """Concat folder (`concat = true`): all its videos chain into ONE stream,
    mounted at /<folder>/chain. Per video: first frame frozen CONCAT_FREEZE_S
    s -> the clip -> last frame frozen CONCAT_FREEZE_S s -> CONCAT_GAP_S s of
    black -> next. Segments are normalised to one size/fps (the folder's
    size/fps settings; 1920x1080 @ 30 when left at "keep") so mixed sources
    join cleanly; each carries its index in the top-left corner. The index ->
    source map, with every segment's start offset inside the loop, is written
    to workspace/<folder>/manifest.txt — know the number, find the video.

    items = ordered [(src, stem)]. Segments cache individually (index, size
    and fps are in the artifact name, is_fresh covers keyint/mtime); the
    joined chain caches on a stamp listing its segment set. copies /
    shift_frames don't apply (one composite stream); probe does."""
    setts = settings_for(cfg, folder, "")
    size = setts["size"] if setts["size"] != "keep" else CONCAT_DEFAULT_SIZE
    fps_s = setts["fps"] if setts["fps"] != "keep" else CONCAT_DEFAULT_FPS
    w, h = (int(v) for v in size.split("x", 1))
    fps = float(fps_s)
    if setts["copies"] > 1 or setts["shift_frames"]:
        log("concat folder {}: copies/shift_frames ignored (one chained stream)"
            .format(folder))
    out_dir = os.path.join(workspace, folder)
    os.makedirs(out_dir, exist_ok=True)

    segs = []                              # (index, src, seg_path)
    for i, (src, stem) in enumerate(items, 1):
        seg = os.path.join(out_dir, "{}__seg{}_{}x{}_f{}.ts".format(
            stem, i, w, h, fps_s))
        if is_fresh(seg, src, setts, check_grade=False):
            log("cached: {}".format(seg))
        elif not build_concat_segment(src, seg, i, w, h, fps):
            continue                       # bad video skipped; its number stays reserved
        segs.append((i, src, seg))
    if not segs:
        log("concat folder {}: no usable videos".format(folder))
        return [], []

    chain = os.path.join(out_dir, "__chain__.ts")   # "__" can't clash with a
                                                    # sanitized stem
    seg_names = [os.path.basename(s) for _, _, s in segs]
    meta = _read_stamp(chain)
    fresh = (not setts["force_reencode"]
             and os.path.isfile(chain)
             and meta is not None
             and meta.get("keyint") == KEYINT
             and meta.get("segments") == seg_names
             and os.path.getmtime(chain) >= max(
                 os.path.getmtime(s) for _, _, s in segs))
    if fresh:
        log("cached: {}".format(chain))
    else:
        listf = chain + ".concat"
        with open(listf, "w", encoding="utf-8") as f:
            for _, _, s in segs:
                f.write("file '{}'\n".format(os.path.abspath(s)))
        tmp = _tmp_for(chain)
        r = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
             "-f", "concat", "-safe", "0", "-i", listf,
             "-c", "copy", "-an", "-f", "mpegts", tmp],
            capture_output=True, text=True)
        _rm(listf)
        if r.returncode != 0:
            log("concat join FAILED for {}: {}".format(
                chain, (r.stderr or "").strip()[-400:]))
            _rm(tmp)
            return [], []
        os.replace(tmp, chain)
        _stamp(chain, extra={"segments": seg_names})

    # manifest: number -> start offset in the loop, duration, source file.
    # Rebuilt only with the chain (frame_count scans the segment indexes).
    man = os.path.join(out_dir, MANIFEST_NAME)
    if not fresh or not os.path.isfile(man):
        off = 0.0
        lines = ["# index  start(s)  length(s)  source"]
        for i, src, seg in segs:
            n = frame_count(seg)
            dur = n / fps if n else 0.0
            lines.append("{:>5}  {:>8.1f}  {:>9.1f}  {}".format(i, off, dur, src))
            off += dur
        try:
            with open(man, "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
        except OSError:
            pass

    mount = "/{}/chain".format(folder)
    if setts["probe"]:
        return [], [(mount, chain, w, h, fps)]
    return [(mount, chain, [])], []


# --------------------------------------------------------------- probe ------
#
# Probe mode streams a black screen until `touch workspace/start`, then plays the
# (phase-shifted) content once and returns to black, re-armed for the next touch.
# A separate process per stream (probe_feeder.py) owns one continuous publish
# session and feeds it raw frames — black until the signal, then the content —
# so the engine under test never disconnects at the switch. Black is generated
# inside the feeder (no clip files here); see probe_feeder.py.


# -------------------------------------------------------------- mediamtx ----

def find_mediamtx():
    for cand in (os.environ.get("MEDIAMTX_BIN", ""),
                 shutil.which("mediamtx") or "",
                 os.path.join(os.path.dirname(os.path.abspath(__file__)), "mediamtx")):
        if cand and os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    sys.exit("mediamtx binary not found. Install it in PATH, next to streamer.py, "
             "or set MEDIAMTX_BIN=/path/to/mediamtx")


def write_mediamtx_config(path, port, transport, api_port=None):
    transports = {"tcp": "[tcp]", "udp": "[udp]", "both": "[udp, tcp]"}[transport]
    lines = [
        "logLevel: {}".format("warn" if DEV else "error"),
        # per-client send buffer (packets); large value keeps slow clients and
        # 100+ concurrent readers from being dropped during bitrate spikes
        "writeQueueSize: 2048",
    ]
    # the API feeds the delivery watchdog and the readiness wait; localhost
    # only, so nothing is exposed beyond the host
    if api_port:
        lines += ["api: yes", "apiAddress: 127.0.0.1:{}".format(api_port)]
    else:
        lines += ["api: no"]
    lines += [
        "metrics: no",
        "pprof: no",
        "playback: no",
        "rtsp: yes",
        "rtspAddress: :{}".format(port),
        "rtspTransports: {}".format(transports),
        # UDP RTP ports offset by +10000 so parallel instances don't collide
        "rtpAddress: :{}".format(port + 10000),
        "rtcpAddress: :{}".format(port + 10001),
        "rtmp: no",
        "hls: no",
        "webrtc: no",
        "srt: no",
        "paths:",
    ]
    # Every stream — regular publishers and probe feeders alike — pushes to
    # mediamtx, so only the catch-all path is needed.
    lines.append("  all_others:")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


# ------------------------------------------------------------- publisher ----

def publish_cmd(src, url):
    """Loop a prepared TS to mediamtx (-c copy). Phase shift is baked into the
    file at prepare time, so no runtime seek is needed."""
    return ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
            "-fflags", "+genpts", "-re", "-stream_loop", "-1",
            "-i", src, "-c", "copy", "-an",
            "-f", "rtsp", "-rtsp_transport", "tcp", url]


def feeder_cmd(content, url, w, h, fps, signal_paths):
    """Probe feeder: black until any of signal_paths is touched, then content
    once, repeat (one continuous publish session — see probe_feeder.py).
    Two signal files per stream: the shared global one (fires every probe at
    once) and the stream's own (fires just this camera — no cross-talk)."""
    feeder = os.path.join(os.path.dirname(os.path.abspath(__file__)), "probe_feeder.py")
    cmd = [sys.executable, feeder,
           "--content", content, "--url", url,
           "--width", str(w), "--height", str(h),
           "--fps", str(fps), "--x264-params", X264_PARAMS,
           "--preset", X264_PRESET]
    for sp in signal_paths:
        cmd += ["--signal-file", sp]
    return cmd


# -------------------------------------------------------------- prepare -----

def prepare_source(cfg, workspace, src, folder, stem, mount_stem=None):
    """Prepare one source video end-to-end (transcode/remux, mini composite,
    rotation body, forward-rotated copies) and return its streams as
    (publishers [(mount, file, warns)], feeders [(mount, file, w, h, fps)]).
    warns lists realism issues kept in an as-is file (camera_grade=false) —
    surfaced as a (!) marker on the URL banner. Touches only this video's
    workspace files — independent videos can be prepared in parallel.
    mount_stem overrides the RTSP mount name only (mini numbering); workspace
    artifacts stay named by the real stem, so a reordered library never
    serves a cached artifact under the wrong number."""
    pubs, feeders = [], []
    setts = settings_for(cfg, folder, stem)
    if setts["copies"] <= 0:
        log("skip (copies=0): {}".format(src))
        return pubs, feeders

    out_dir = os.path.join(workspace, folder)
    os.makedirs(out_dir, exist_ok=True)
    play_path = os.path.join(out_dir, transcoded_name(stem, setts))

    # Cache check first, source analysis only on a miss: needs_transcode()'s
    # GOP check reads the whole packet index, so probing every source on
    # every startup would make cached runs scale with library size.
    # (is_fresh also handles force_reencode — "always re-encode" bypasses
    # the cache so force doesn't appear to do nothing until the workspace
    # is deleted by hand.)
    warns = []
    if is_fresh(play_path, src, setts):
        log("cached: {}".format(play_path))
        # as-is artifact (camera_grade=false): its realism issues were
        # recorded in the stamp at build time — reuse them for the banner
        warns = (_read_stamp(play_path) or {}).get("warn") or []
    else:
        info = ffprobe_info(src)
        reasons, warns = needs_transcode(src, setts, info)
        if reasons:
            log("transcoding [{}]: {} -> {}".format(",".join(reasons), src, play_path))
            warns = []   # a re-encode normalises everything
            if not transcode(src, play_path, setts, info):
                return pubs, feeders
        else:
            log("remuxing to ts: {} -> {}".format(src, play_path))
            # Stream-copy remux fails on containers with no usable
            # timestamps (e.g. AVI: "first pts and dts value must be set").
            # Fall back to a re-encode, which synthesises fresh PTS/DTS and
            # produces a clean loopable TS — so a quirky source still serves
            # (and a probe stream still gets a valid file to wrap).
            if not remux_to_ts(src, play_path):
                log("remux failed, re-encoding instead: {}".format(src))
                warns = []
                if not transcode(src, play_path, setts, info):
                    return pubs, feeders
            elif warns:
                log("serving as-is [{}] (camera_grade=false): {}".format(
                    ",".join(warns), play_path))
                _stamp(play_path, warn=warns)

    # shift_frames > 0 puts copies out of phase. A runtime -ss seek can't
    # do this: a stream-copy seek snaps to the nearest keyframe, so a
    # sub-GOP shift collapses to frame 0. Instead each copy is
    # forward-rotated at prepare time: copy i = frames [k:N]+[0:k],
    # starting at frame k = i*shift. Length stays N (no loop drift). All
    # slices come from one uniform libx264 `body` so the concat-copy join
    # and the -stream_loop wrap are clean; the publisher just -c copy's it.
    shift = setts["shift_frames"]
    copies = setts["copies"]
    # mini reshapes the whole clip (freeze + clip + black), so phase shift
    # is meaningless for it — bypass the rotation machinery.
    if setts["mini"]:
        if shift:
            log("mini ignores shift_frames: {}".format(src))
        shift = 0
    nframes = frame_count(play_path) if shift > 0 and copies > 1 else 0
    if shift > 0 and copies > 1 and not nframes:
        log("frame count unknown for {} — copies stay unshifted".format(play_path))

    # size/fps of the prepared file — for the probe feeder's encode and the
    # mini composite. If the file we just built/cached can't be probed,
    # something is genuinely broken; guessing dimensions here would feed the
    # probe encoder wrong-size raw frames (silent corruption), so skip the
    # stream instead of masking it.
    cinfo = None
    if nframes or setts["probe"] or setts["mini"]:
        cinfo = ffprobe_info(play_path)
        if cinfo is None and (setts["probe"] or setts["mini"]):
            log("prepared file unreadable, stream skipped: {}".format(play_path))
            return pubs, feeders
        if cinfo is None:
            log("prepared file unreadable ({}) — copies stay unshifted".format(play_path))
            nframes = 0

    # mini: build the frozen-frame + clip (+ black) composite once; every
    # copy streams it. Probe minis omit the black tail (the feeder returns
    # to black after the clip). Cached and rebuilt only when stale/forced.
    mini_content = None
    if setts["mini"]:
        mini_content = os.path.join(
            out_dir, "{}__mini{}.ts".format(stem, "p" if setts["probe"] else ""))
        if is_fresh(mini_content, play_path, setts):
            log("cached: {}".format(mini_content))
        elif not build_mini(play_path, mini_content, cinfo,
                            with_tail_black=not setts["probe"]):
            mini_content = None   # build failed -> fall back to the plain clip

    body = None        # uniform re-encode with forced IDRs at the cut frames
    if nframes:
        # frame offsets each shifted copy starts at (i*shift), de-duped
        cut_frames = sorted({(i * shift) % nframes for i in range(1, copies)} - {0})
        # body depends on the cut set, so key the cache by shift/copies
        body = os.path.join(out_dir, "{}__body_s{}c{}.ts".format(stem, shift, copies))
        if is_fresh(body, play_path, setts):
            log("cached: {}".format(body))
        elif not _full_reencode(play_path, body, cut_frames):
            body = None   # body build failed -> copies fall back to no shift

    mname = mount_stem or stem
    for i in range(copies):
        mount = ("/{}/{}".format(folder, mname) if i == 0
                 else "/{}/{}_{}".format(folder, mname, i + 1))
        # mini composite when built, else the untouched original (copy 0)
        src_file = mini_content or play_path
        k = (i * shift) % nframes if (nframes and body) else 0
        if k > 0:
            fwd = os.path.join(out_dir, "{}__fwd{}.ts".format(stem, k))
            # stream-copy of the verified body -> grade inherited, no rescan
            if is_fresh(fwd, body, setts, check_grade=False):
                log("cached: {}".format(fwd))
                src_file = fwd
            elif build_forward(body, fwd, k):
                src_file = fwd
            # build failed -> fall back to unshifted play_path
        if setts["probe"]:
            # the feeder re-encodes through its own sink, so served output
            # is always camera-grade regardless of the file's warnings
            feeders.append((mount, src_file, cinfo[1], cinfo[2], cinfo[3]))
        else:
            # derived files (mini/body/fwd) are re-encoded and clean; only
            # the untouched as-is play file carries its realism warnings
            pubs.append((mount, src_file,
                         warns if src_file == play_path else []))
    return pubs, feeders


def port_is_free(port):
    """True if mediamtx could bind this RTSP port.

    SO_REUSEADDR mirrors how mediamtx (Go's net listener) binds: without it a
    plain bind fails with EADDRINUSE while TIME_WAIT sockets from a previous
    run's RTSP clients linger (~60 s), so a genuinely free port looks busy and
    the streamer skips past it — eventually finding the whole range "busy" and
    refusing to start. We must test the port the same way the server will use it.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def pick_api_port(rtsp_port):
    """Free localhost port for the mediamtx API, or None (watchdog disabled).
    port+2000 by convention (UDP data already sits at +10000/+10001), with a
    few fallbacks so one busy port doesn't cost the watchdog."""
    for off in range(2000, 2010):
        if port_is_free(rtsp_port + off):
            return rtsp_port + off
    return None


def wait_rtsp_ready(port, proc, timeout=10.0):
    """Wait until mediamtx actually accepts TCP on its RTSP port.

    A fixed post-spawn sleep either wastes time or (on a loaded host) lets the
    publishers race the server and burn their first restart backoff. Polling
    the real accept is both faster and reliable. False = the process died
    (bind failure); a live-but-silent server past the timeout is accepted and
    left to the supervisor.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return False
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.5)
        try:
            s.connect(("127.0.0.1", port))
            return True
        except OSError:
            time.sleep(0.2)
        finally:
            s.close()
    return proc.poll() is None


def api_paths(api_port):
    """mount -> (ready, bytesReceived) for every path mediamtx knows,
    or None when the API is unreachable (caller skips the cycle)."""
    out, page = {}, 0
    while True:
        url = ("http://127.0.0.1:{}/v3/paths/list?itemsPerPage=100&page={}"
               .format(api_port, page))
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                data = json.load(r)
        except Exception:
            return None
        for it in data.get("items") or []:
            out["/" + (it.get("name") or "")] = (
                bool(it.get("ready")), int(it.get("bytesReceived") or 0))
        page += 1
        if page >= int(data.get("pageCount") or 1):
            return out


def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


# ------------------------------------------------------------------ main ----

def raise_nofile():
    """Raise the open-file soft limit to the hard cap so the image needs no
    compose `ulimits` for 100+ concurrent RTSP clients (each client is a
    socket/fd). Best-effort; harmless where already high or not permitted."""
    target = 65536
    try:
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        # as root (container default) we can also raise the hard cap; if that's
        # not permitted, fall back to lifting soft up to the existing hard.
        for want_hard in (max(hard, target), hard):
            try:
                resource.setrlimit(resource.RLIMIT_NOFILE,
                                   (max(soft, min(target, want_hard)), want_hard))
                break
            except (ValueError, OSError):
                continue
    except Exception:
        pass


def main():
    global LOG_FH

    raise_nofile()
    ap = argparse.ArgumentParser(
        description="RTSP streamer {} (mediamtx + ffmpeg)".format(VERSION))
    ap.add_argument("--config", default="config.ini", help="Path to config file")
    args = ap.parse_args()

    cfg = load_config(args.config)
    base = os.path.dirname(os.path.abspath(args.config))
    get = lambda k, fb: cfg.get("streamer", k, fallback=fb).strip()
    # VIDEOS_DIR / WORKSPACE_DIR pin these to fixed paths regardless of where the
    # config file lives. The container sets them to /app/video_samples and
    # /app/workspace so the config can sit in a mounted /app/config dir without
    # the relative `videos`/`workspace` keys resolving under it. Unset (the
    # no-Docker case) = resolve relative to the config file's directory, as before.
    videos_dir = (os.environ.get("VIDEOS_DIR", "").strip()
                  or os.path.join(base, get("videos", "video_samples")))
    workspace = (os.environ.get("WORKSPACE_DIR", "").strip()
                 or os.path.join(base, get("workspace", "workspace")))
    ports = parse_ports(get("ports", "41000,41001,41002"))
    transport = get("transport", "tcp").lower()
    advertise = (os.environ.get("ADVERTISE_IP", "").strip()
                 or get("advertise_ip", "")
                 or get_local_ip())

    if transport not in ("tcp", "udp", "both"):
        sys.exit("transport must be tcp, udp or both (got: {})".format(transport))
    try:
        ki = int(get("keyint", "60") or 60)
    except ValueError:
        sys.exit("keyint must be an integer (got: {})".format(get("keyint", "")))
    if not 10 <= ki <= 600:
        sys.exit("keyint must be between 10 and 600 frames (got: {})".format(ki))
    _set_keyint(ki)
    if not os.path.isdir(videos_dir):
        sys.exit("Videos folder not found: {}".format(videos_dir))

    sources = scan_videos(videos_dir)
    if not sources:
        sys.exit("No videos found under {}".format(videos_dir))

    mediamtx_bin = find_mediamtx()
    os.makedirs(workspace, exist_ok=True)
    if not DEV:
        log_path = os.path.join(workspace, "streamer.log")
        # cap growth across restarts: the log is append-only and also receives
        # every stream process's stderr, so a long-lived container would
        # otherwise fill the workspace volume. One rotated generation
        # (streamer.log.1) keeps the tail of the previous history.
        try:
            if os.path.getsize(log_path) > LOG_MAX_BYTES:
                os.replace(log_path, log_path + ".1")
        except OSError:
            pass
        LOG_FH = open(log_path, "a", encoding="utf-8")

    # sweep temp build files a killed previous run may have left behind, and
    # the previous run's port file so the healthcheck can't probe a stale port.
    # Temps whose pid suffix belongs to a live process are another instance's
    # in-progress builds (shared workspace) — deleting those would corrupt
    # its prepare mid-write, so they are skipped.
    for d, _, files in os.walk(workspace):
        for f in files:
            m = re.search(r"\.part(\d+)$", f)
            if m and not _pid_alive(int(m.group(1))):
                _rm(os.path.join(d, f))
    _rm(os.path.join(workspace, PORT_FILE))

    say("══════════════════════════════════════")
    say("  RTSP Streamer v{}".format(VERSION))
    say("══════════════════════════════════════")

    # -- prepare: decide per video whether to stream the source directly or a
    #    cached transcoded copy from the workspace (loader animation in prod).
    #    The animated block needs a TTY on stdout — in the image that is
    #    always true (the entrypoint runs streamer.py under its own pty via
    #    ptyrun.py), so it animates under `docker compose up` with no `tty:`
    #    in the compose file. Without any TTY (direct run with piped output)
    #    prod falls back to append-only snapshot lines, one per video.
    anim = None
    loader = None
    if not DEV:
        say("[prepare] processing {} videos... (details: workspace/streamer.log)".format(len(sources)))
        try:
            import loader
        except Exception:
            loader = None
        if loader is not None and sys.stdout.isatty():
            try:
                anim = loader.start(subtitle="v{}".format(VERSION))
            except Exception:
                anim = None

    # de-dup sanitized names first: sources like `clip.mp4` + `clip.mkv` (or
    # `a b.mp4` + `a_b.mp4`) sanitize to the same stem, which would silently
    # share one workspace file and one RTSP mount — second stream lost and the
    # cache cross-contaminated. A "__N" suffix keeps every source served; it
    # cannot collide with a sanitized stem (sanitize collapses repeated "_").
    jobs = []
    taken = set()
    for src in sources:
        folder, stem = folder_and_stem(src, videos_dir)
        base, n = stem, 2
        while (folder, stem) in taken:
            stem = "{}__{}".format(base, n)
            n += 1
        if stem != base:
            log("name collision: {} will serve as /{}/{}".format(src, folder, stem))
        taken.add((folder, stem))
        jobs.append((src, folder, stem))

    # partition: concat folders chain all their videos into one stream (one
    # job per folder); mini videos mount as numbers (/mini/1, /mini/2, ... in
    # scan order — with many clips the URL is the index, not a long filename).
    # Each numbered/concat folder gets a workspace/<folder>/manifest.txt
    # mapping index -> source, so a number always leads back to its video.
    concat_folders = {}          # folder -> ordered [(src, stem)]
    mini_index = {}              # folder -> ordered [(idx, src)]
    counters = {}
    plain_jobs = []              # (src, folder, stem, mount_stem)
    for src, folder, stem in jobs:
        setts = settings_for(cfg, folder, stem)
        if setts["concat"]:
            if setts["mini"]:
                log("concat wins over mini for {}".format(src))
            concat_folders.setdefault(folder, []).append((src, stem))
            continue
        mount_stem = None
        if setts["mini"]:
            idx = counters.get(folder, 0) + 1
            counters[folder] = idx
            mount_stem = str(idx)
            mini_index.setdefault(folder, []).append((idx, src))
        plain_jobs.append((src, folder, stem, mount_stem))

    for folder, entries in mini_index.items():
        mdir = os.path.join(workspace, folder)
        os.makedirs(mdir, exist_ok=True)
        try:
            with open(os.path.join(mdir, MANIFEST_NAME), "w", encoding="utf-8") as f:
                f.write("# index  mount  source\n")
                for idx, src in entries:
                    f.write("{:>5}  /{}/{}  {}\n".format(idx, folder, idx, src))
        except OSError:
            pass

    # (display_name, loader_group, zero-arg prepare callable) — numbered mini
    # videos share their folder as loader group, so 65 of them show as one
    # `mini 12/65` row instead of 65 lines (the manifest keeps the full map)
    tasks = []
    for src, folder, stem, mount_stem in plain_jobs:
        tasks.append(("{}/{}".format(folder, mount_stem or stem),
                      folder if mount_stem else None,
                      (lambda s=src, f=folder, t=stem, m=mount_stem:
                       prepare_source(cfg, workspace, s, f, t, mount_stem=m))))
    for folder in sorted(concat_folders):
        tasks.append(("{}/chain".format(folder), None,
                      (lambda f=folder, it=concat_folders[folder]:
                       prepare_concat(cfg, workspace, f, it))))

    # prepare in parallel: each video's work is independent and lives in
    # ffmpeg subprocesses, so a small thread pool cuts first-run startup on
    # multi-video sets (cached runs are quick either way). ex.map keeps the
    # results in source order, so mounts come out deterministic.
    publishers = []     # (mount, file, warns)
    probe_feeders = []  # (mount, content_file, w, h, fps)
    done = [0]
    done_lock = threading.Lock()

    if anim is not None:
        loader.set_items([(name, grp) for name, grp, _ in tasks])

    def run_job(task):
        name, _grp, fn = task
        if anim is not None:
            loader.item_start(name)
        try:
            res = fn()
        except Exception:
            log("prepare FAILED for {}: {}".format(name, traceback.format_exc().strip()))
            res = ([], [])
        with done_lock:
            done[0] += 1
            if anim is not None:
                loader.item_done(name)
            elif not DEV:
                bar = (loader.snapshot(done[0], len(tasks)) if loader is not None
                       else "prepared {}/{}".format(done[0], len(tasks)))
                say(" {}  {}".format(bar, name))
        return res

    workers = max(1, min(4, os.cpu_count() or 1, len(tasks)))
    try:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            results = list(ex.map(run_job, tasks))
    finally:
        if anim is not None:
            loader.stop(anim)
    for pubs, feeders in results:
        publishers.extend(pubs)
        probe_feeders.extend(feeders)

    if not publishers and not probe_feeders:
        sys.exit("Nothing to stream.")

    proc_out = None if DEV else LOG_FH

    # clear stale signals (global + per-stream) from a previous run so probe
    # streams start armed
    signal_path = os.path.join(workspace, SIGNAL_FILE)
    try:
        for f in os.listdir(workspace):
            if f == SIGNAL_FILE or f.startswith(SIGNAL_FILE + "_"):
                _rm(os.path.join(workspace, f))
    except OSError:
        pass

    # -- start mediamtx on the first free port from the configured list.
    # One config file per port: mediamtx hot-reloads its config when the file
    # changes, so two instances sharing a workspace through one mediamtx.yml
    # would rebind each other the moment the second one writes it.
    mtx = None
    port = None
    api_port = None
    for cand in ports:
        if not port_is_free(cand):
            log("port {} busy, trying next".format(cand))
            continue
        api_port = pick_api_port(cand)
        if api_port is None:
            log("no free API port near {} — delivery watchdog disabled".format(cand))
        mtx_cfg = os.path.join(workspace, "mediamtx_{}.yml".format(cand))
        write_mediamtx_config(mtx_cfg, cand, transport, api_port)
        proc = subprocess.Popen([mediamtx_bin, mtx_cfg], stdout=proc_out, stderr=proc_out)
        # wait for a real TCP accept, not a fixed sleep — publishers must not
        # race a slow-starting server into their first restart backoff
        if wait_rtsp_ready(cand, proc):
            mtx, port = proc, cand
            break
        log("mediamtx failed on port {} (rc={}), trying next".format(cand, proc.returncode))
    if mtx is None:
        sys.exit("No usable port among {} — all busy or mediamtx failed. "
                 "Check mediamtx_*.yml in {}".format(ports, workspace))

    # record the bound port for the Docker healthcheck (nc -z against it)
    try:
        with open(os.path.join(workspace, PORT_FILE), "w", encoding="utf-8") as pf:
            pf.write(str(port))
    except OSError:
        pass

    # -- start one process per stream (ffmpeg publisher or probe feeder), supervised
    stopping = []

    def on_signal(sig, _frame):
        stopping.append(sig)

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    # copy suffixes can still collide with a real file (`clip.mp4` with
    # copies=2 emits /x/clip_2 while `clip_2.mp4` also exists) — rename the
    # late arrival instead of silently overwriting the dict entry, which
    # dropped a stream.
    used_mounts = set()

    def unique_mount(mount):
        u, n = mount, 2
        while u in used_mounts:
            u = "{}_x{}".format(mount, n)
            n += 1
        if u != mount:
            log("duplicate mount {} — serving as {}".format(mount, u))
        used_mounts.add(u)
        return u

    # mount -> argv; "publish" loops a TS, "feeder" runs probe_feeder.py
    stream_cmds = {}
    pub_mounts, feeder_mounts = [], []   # (mount, warns) / (mount, own signal)
    for mount, fpath, warns in publishers:
        mount = unique_mount(mount)
        url = "rtsp://127.0.0.1:{}{}".format(port, mount)
        stream_cmds[mount] = publish_cmd(fpath, url)
        pub_mounts.append((mount, warns))
    for mount, fpath, w, h, fps in probe_feeders:
        mount = unique_mount(mount)
        url = "rtsp://127.0.0.1:{}{}".format(port, mount)
        # per-stream signal alongside the global one: touch workspace/start
        # fires every probe (as before), touch workspace/start_<f>_<name>
        # fires just this camera
        own_sig = "{}_{}".format(SIGNAL_FILE, mount[1:].replace("/", "_"))
        stream_cmds[mount] = feeder_cmd(
            fpath, url, w, h, fps,
            [signal_path, os.path.join(workspace, own_sig)])
        feeder_mounts.append((mount, own_sig))

    procs = {}  # mount -> {proc, backoff, started, retry_at, bytes, bytes_ts}
    for mount, cmd in stream_cmds.items():
        p = subprocess.Popen(cmd, stdout=proc_out, stderr=proc_out)
        procs[mount] = {"proc": p, "backoff": RESTART_BACKOFF_START,
                        "started": time.time(), "retry_at": 0.0,
                        "bytes": None, "bytes_ts": time.time()}

    # hold the banner until every stream is confirmed publishing, so "ready"
    # means an engine pointed at the URLs connects on the first try
    if api_port:
        deadline = time.time() + READY_WAIT_SECONDS
        pending = set(stream_cmds)
        while pending and time.time() < deadline and not stopping:
            time.sleep(0.5)
            paths = api_paths(api_port)
            if paths is not None:
                pending = {m for m in pending if not paths.get(m, (False, 0))[0]}
        if pending:
            log("not publishing after {:.0f}s (will keep retrying): {}".format(
                READY_WAIT_SECONDS, ", ".join(sorted(pending))))

    # short (!) markers on as-is streams (camera_grade=false), one legend
    # line at the bottom — visible where the URL gets copied, no log flood
    warn_text = {"bframes": "B-frames", "gop": "long GOP"}

    # numbered mini mounts collapse in the banner: dozens of URLs that
    # differ only by index would flood the screen, so the first and last
    # are printed with a count between them (a (!) middle still gets its
    # own line so no warning is hidden) — manifest.txt keeps the full map
    display = []                 # ("url", mount, warns) | ("run", folder)
    runs = {}                    # folder -> [(mount, warns)] in index order
    for mount, warns in pub_mounts:
        m = re.match(r"^/(.+)/\d+$", mount)
        if m and m.group(1) in mini_index:
            if m.group(1) not in runs:
                display.append(("run", m.group(1)))
            runs.setdefault(m.group(1), []).append((mount, warns))
        else:
            display.append(("url", mount, warns))

    any_warn = any(w for _, w in pub_mounts)

    def url_row(mount, warns):
        mark = ("  (! {})".format(", ".join(warn_text.get(w, w) for w in warns))
                if warns else "")
        say("  rtsp://{}:{}{}{}".format(advertise, port, mount, mark))

    say("")
    say("──────────── RTSP streams ready ────────────")
    say("  port {}  transport {}".format(port, transport))
    for entry in display:
        if entry[0] == "url":
            url_row(entry[1], entry[2])
            continue
        entries = runs[entry[1]]
        if len(entries) <= 3:
            for mount, warns in entries:
                url_row(mount, warns)
            continue
        mids = entries[1:-1]
        url_row(*entries[0])
        say("   ⋮  {} … {}  ({} more)".format(
            mids[0][0], mids[-1][0], len(mids)))
        for mount, warns in mids:
            if warns:
                url_row(mount, warns)
        url_row(*entries[-1])
    for mount, own_sig in feeder_mounts:
        say("  rtsp://{}:{}{}  (probe — fire: touch workspace/{} | all: touch workspace/{})".format(
            advertise, port, mount, own_sig, SIGNAL_FILE))
    if any_warn:
        say("  (!) served as-is — clients joining mid-stream may glitch briefly")
    man_folders = sorted(set(mini_index) | set(concat_folders))
    if man_folders:
        say("  index maps: " + ", ".join(
            "workspace/{}/{}".format(f, MANIFEST_NAME) for f in man_folders))
    say("─────────────────────────────────────────────")

    last_watch = 0.0
    try:
        while not stopping:
            time.sleep(1.0)
            if mtx.poll() is not None:
                say("[streamer] mediamtx died (rc={}) — shutting down".format(mtx.returncode))
                break
            now = time.time()

            # delivery watchdog: a publisher can hang without dying (blocked
            # RTSP write, wedged ffmpeg) — process-alive checks never catch
            # that. mediamtx's per-path bytesReceived is ground truth for
            # "data is actually arriving"; a live process whose counter has
            # not moved in STALL_SECONDS is killed so the normal restart
            # path brings the stream back. Best-effort: an unreachable API
            # skips the cycle, it never falses a restart.
            if api_port and now - last_watch >= WATCHDOG_POLL_SECONDS:
                last_watch = now
                paths = api_paths(api_port)
                if paths is not None:
                    for mount, st in procs.items():
                        p = st["proc"]
                        if p is None or p.poll() is not None:
                            continue
                        b = paths.get(mount, (False, -1))[1]
                        if b != st["bytes"]:
                            st["bytes"], st["bytes_ts"] = b, now
                        elif now - st["bytes_ts"] > STALL_SECONDS:
                            log("stream {} stalled (no data {:.0f}s) — killing for restart".format(
                                mount, now - st["bytes_ts"]))
                            p.kill()   # kill, not terminate: a wedged process may ignore TERM

            for mount, st in procs.items():
                if st["proc"] is not None and st["proc"].poll() is None:
                    if now - st["started"] > STABLE_RESET_SECONDS:
                        st["backoff"] = RESTART_BACKOFF_START
                    continue
                # stream process died
                if st["proc"] is not None:
                    log("stream {} exited (rc={}), restarting in {:.0f}s".format(
                        mount, st["proc"].returncode, st["backoff"]))
                    st["retry_at"] = now + st["backoff"]
                    st["backoff"] = min(st["backoff"] * 2, RESTART_BACKOFF_MAX)
                    st["proc"] = None
                elif now >= st["retry_at"]:
                    st["proc"] = subprocess.Popen(stream_cmds[mount],
                                                  stdout=proc_out, stderr=proc_out)
                    st["started"] = now
                    st["bytes"], st["bytes_ts"] = None, now
    finally:
        say("[streamer] stopping...")
        for st in procs.values():
            if st["proc"] is not None and st["proc"].poll() is None:
                st["proc"].terminate()
        if mtx.poll() is None:
            mtx.terminate()
        deadline = time.time() + 5
        for st in procs.values():
            p = st["proc"]
            if p is not None:
                try:
                    p.wait(timeout=max(0.1, deadline - time.time()))
                except subprocess.TimeoutExpired:
                    p.kill()
        try:
            mtx.wait(timeout=5)
        except subprocess.TimeoutExpired:
            mtx.kill()
    return 0


if __name__ == "__main__":
    sys.exit(main())
