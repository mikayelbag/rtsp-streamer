#!/usr/bin/env python3
"""
RTSP streamer 2.12.1

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
  - a video that needs no change is published straight from the videos
    folder (no workspace copy); should its publisher die, the supervisor
    switches it to a loop-safe MPEG-TS copy built on the spot
  - transcode only when needed (fps / size / bitrate change, unsupported
    codec, or force_reencode=true); results are cached and reused. Each
    artifact carries a .meta build stamp, so cached runs skip the packet
    index rescans and a `keyint` change rebuilds automatically
  - copies:        N parallel streams from one file
  - shift_frames: phase offset between copies (copy i starts at frame i*shift;
                  forward-rotated, no loop drift — simulates out-of-phase cameras)
  - probe mode:    streams black until `touch workspace/start`, then plays the
                   (phase-shifted) content once and returns to black, re-armed;
                   probe_idle = frame idles on a frozen frame instead (first
                   frame before the first fire, last frame after each pass)
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
import textwrap
import threading
import time
import traceback
import urllib.request
from concurrent.futures import ThreadPoolExecutor

VERSION = "2.12.1"
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
#
# ref=X264_REF sizes the decoder's surface pool. x264 always writes a VUI
# bitstream_restriction, and with bframes=0 that says max_num_reorder_frames=0;
# FFmpeg's NVDEC then allocates ref_frame_count + num_reorder_frames + 2
# surfaces — 3 at veryfast's default ref=1. An engine that holds a few
# decoded frames in flight runs the pool dry: "No decoder surfaces left",
# then "Missing reference picture" / "decode_slice_header error" as the
# dropped frame's dependants fail. Real cameras usually omit the restriction,
# so FFmpeg derives ~4 reorder frames from the level and allocates ~7. ref=4
# is the most a 1080p stream may use within level 4.x and lifts the pool to 6.
X264_REF = 4
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
                   "ref={1}:repeat-headers=1").format(n, X264_REF)


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


# ------------------------------------------------------- progress reports ----
# Prepare work runs in pool threads, one video per thread. run_job() names
# the thread's video in _TASK; the build steps below report what they are
# doing (_report_step), their outcome (_report_note) and, through _ffmpeg,
# how much of an encode is done — to REPORTER (the loader module while its
# animation runs, else None and every report is a no-op).
REPORTER = None
_TASK = threading.local()


def _report(fn, *args):
    name = getattr(_TASK, "name", None)
    if REPORTER is None or name is None:
        return
    try:
        getattr(REPORTER, fn)(name, *args)
    except Exception:
        pass        # a display hiccup must never fail a prepare


def _report_step(label, detail=""):
    _report("item_step", label, detail)


def _report_note(kind, detail=""):
    _report("item_note", kind, detail)


REASON_TEXT = {"forced": "forced", "fps": "settings", "size": "settings",
               "bitrate": "settings", "unreadable": "unreadable",
               "broken": "broken chain", "bframes": "B-frames",
               "gop": "long GOP"}


def _reason_text(reasons):
    """needs_transcode() reasons as a short human phrase for the loader."""
    out = []
    for r in reasons:
        t = ("codec " + r.split("=", 1)[1] if r.startswith("codec=")
             else REASON_TEXT.get(r, r))
        if t not in out:
            out.append(t)
    return ", ".join(out)


def media_duration(path):
    """Container duration in seconds, or 0.0 when unknown."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=30)
        return max(0.0, float((r.stdout or "").split()[0]))
    except Exception:
        return 0.0


def _ffmpeg(cmd, out_dur=0.0):
    """subprocess.run(cmd) for an ffmpeg build step. When a progress
    REPORTER is listening and the output's duration is known, ffmpeg also
    writes `-progress` key=value lines to stdout; out_time against out_dur
    is the encoded fraction of this video — what the loader's cells show
    (a 30 min video with 15 min encoded fills half its share)."""
    if REPORTER is None or getattr(_TASK, "name", None) is None or out_dur <= 0:
        return subprocess.run(cmd, capture_output=True, text=True)
    cmd = [cmd[0], "-progress", "pipe:1", "-nostats"] + cmd[1:]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True, errors="replace")
    err = []
    t = threading.Thread(target=lambda: err.append(p.stderr.read()), daemon=True)
    t.start()
    for line in p.stdout:
        key, _, val = line.strip().partition("=")
        if key in ("out_time_us", "out_time_ms"):    # both are microseconds
            try:
                base, width = getattr(_TASK, "span", (0.0, 1.0))
                frac = min(1.0, max(0.0, int(val) / 1e6 / out_dur))
                _report("item_progress", base + width * frac)
            except ValueError:
                pass
    p.wait()
    t.join(timeout=5)
    return subprocess.CompletedProcess(cmd, p.returncode, "", "".join(err))


# ---------------------------------------------------------------- config ----

SETTING_KEYS = ("copies", "shift_frames", "fps", "size", "bitrate_kbps", "probe",
                "force_reencode", "mini", "concat", "camera_grade", "probe_idle")
PROBE_IDLE = ("black", "frame")

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
            elif key == "probe_idle":
                if v.lower() not in PROBE_IDLE:
                    err = "must be 'black' or 'frame'"
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
        # probe streams before a fire: "black", or "frame" = the clip's first
        # frame frozen, and after each pass its last frame frozen
        "probe_idle": "black",
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
        # a broken reference chain re-encodes regardless of camera_grade —
        # it isn't a realism nuance, it derails hardware decoders (see
        # _broken_chain); only checked when nothing else forces an encode
        if not reasons and info[0] == "h264":
            broken = _broken_chain(path)
            if broken:
                log("broken source ({}), re-encoding: {}".format(broken, path))
                reasons.append("broken")
                return reasons, warns
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


FRAME_NUM_GAP_OK = 16    # gaps_in_frame_num_allowed: max legit forward skip


def _broken_chain(path):
    """Why an H.264 source can't be stream-copied safely, or None.

    Reads slice headers only (trace_headers, no decode — ~0.5 s per minute
    of 1080p). Two defects, both found in real camera recordings:

    - a lost IDR: the following P-frames' frame_num continues from the
      vanished IDR (e.g. 15 -> 1) and their POC drops without the reset an
      IDR brings. FFmpeg reads that as reordering, grows its reorder buffer
      ("Increasing reorder buffer to 13") and from then on holds that many
      frames — NVDEC's level-sized surface pool runs dry: "No decoder
      surfaces left", then "Missing reference picture". The software
      decoder conceals it silently, so decoder logs can't catch it.
    - the file doesn't open on an IDR, so every -stream_loop wrap lands
      mid-chain the same way.

    Rule (H.264 7.4.3): a non-IDR picture's frame_num equals the previous
    reference picture's, or that plus one. With gaps_in_frame_num_allowed
    a small forward skip is legal (dropped frames); anything else is not.
    Re-encoding conceals the hole once and emits a clean chain. Any
    failure to scan reports None — unknown is not known-bad."""
    try:
        p = subprocess.Popen(
            ["ffmpeg", "-hide_banner", "-nostdin", "-i", path, "-map", "0:v:0",
             "-c", "copy", "-bsf:v", "trace_headers", "-f", "null", "-"],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            text=True, errors="replace")
    except OSError:
        return None
    max_fn, gaps_ok = 1 << 16, False
    ref_idc = nal = None
    prev_ref, first, why = None, True, None
    try:
        for line in p.stderr:
            m = re.search(r"\]\s+\d+\s+(\w+)\s+\S+ = (\d+)$", line)
            if not m:
                continue
            key, val = m.group(1), int(m.group(2))
            if key == "log2_max_frame_num_minus4":
                max_fn = 1 << (val + 4)
            elif key == "gaps_in_frame_num_allowed_flag":
                gaps_ok = bool(val)
            elif key == "nal_ref_idc":
                ref_idc = val
            elif key == "nal_unit_type":
                nal = val
            elif key == "first_mb_in_slice":
                nal = nal if val == 0 else None    # one check per picture
            elif key == "frame_num" and nal in (1, 5):
                if first:
                    first = False
                    if nal != 5:
                        why = "does not start on an IDR"
                        break
                if nal == 1 and prev_ref is not None and val not in (
                        prev_ref, (prev_ref + 1) % max_fn):
                    gap = (val - prev_ref) % max_fn
                    if not (gaps_ok and gap <= FRAME_NUM_GAP_OK):
                        why = "lost IDR: frame_num {} -> {}".format(prev_ref, val)
                        break
                if nal == 5:
                    prev_ref = 0
                elif ref_idc:
                    prev_ref = val
                nal = None
    finally:
        p.kill()
        p.wait()
    return why


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
    meta = {"version": VERSION, "keyint": KEYINT, "x264": X264_PARAMS}
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


def _stamp_stale(meta):
    """Reason string when a stamped artifact was built with encoder settings
    that no longer match (keyint or the x264 params), else None. Stamps from
    before 2.10.0 carry no x264 key — their encodes lack the ref fix, so
    they rebuild once."""
    if meta.get("keyint") != KEYINT:
        return "keyint changed ({} -> {})".format(meta.get("keyint"), KEYINT)
    if meta.get("x264") != X264_PARAMS:
        return "encoder settings changed"
    return None


def _commit(tmp, dst):
    """Atomically publish a finished build and stamp it."""
    os.replace(tmp, dst)
    _stamp(dst)


def is_fresh(dst, src, setts, check_grade=True):
    """The one cache-validity check for every prepared artifact.

    Fresh = not force_reencode, dst exists and is at least as new as src, and
    the build stamp's keyint and x264 params match. Artifacts without a stamp
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
        why = _stamp_stale(meta)
        if why:
            log("{}, rebuilding: {}".format(why, dst))
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

    r = _ffmpeg(cmd, media_duration(src))
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
    r = _ffmpeg(cmd, media_duration(src))
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
    r = _ffmpeg(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
         "-i", src, "-c", "copy", "-an", "-f", "mpegts", tmp],
        media_duration(src))
    if r.returncode != 0:
        log("remux FAILED for {}: {}".format(src, (r.stderr or "").strip()[-400:]))
        _rm(tmp)
        return False
    _commit(tmp, dst)
    return True


# Direct serving: a source that needs no change is published straight from
# video_samples instead of from a TS copy in the workspace — the copy doubled
# disk use. A small sidecar (<ts_path>.direct) caches the decision and the
# as-is warnings, so cached runs skip the packet-index scan just like a
# stamped artifact does. AVI and friends keep the up-front TS copy (no usable
# timestamps for a -c copy publish).
DIRECT_EXTS = (".mp4", ".m4v", ".mov", ".mkv", ".ts")
DIRECT_SUFFIX = ".direct"
SOURCE_CHECKS = 2       # bump when source analysis gains a check: cached
                        # direct decisions from older checks are redone
                        # (2 = _broken_chain, added in 2.10.1)


def _src_sig(src):
    st = os.stat(src)
    return {"src_mtime": st.st_mtime, "src_size": st.st_size}


def _write_direct(ts_path, src, warns):
    meta = {"version": VERSION, "keyint": KEYINT, "src": src,
            "checks": SOURCE_CHECKS}
    meta.update(_src_sig(src))
    if warns:
        meta["warn"] = warns
    try:
        with open(ts_path + DIRECT_SUFFIX, "w", encoding="utf-8") as f:
            json.dump(meta, f)
    except OSError:
        pass    # undecided next run -> just re-analysed


def _direct_cached(ts_path, src, setts):
    """The cached direct-serving decision for src: its as-is warnings ([]
    when clean), or None when there is no valid decision (no sidecar, source
    replaced or touched, keyint changed (the GOP verdict depends on it),
    made by an older set of source checks, or camera_grade now forbids the
    as-is issues it recorded)."""
    try:
        with open(ts_path + DIRECT_SUFFIX, encoding="utf-8") as f:
            meta = json.load(f)
        sig = _src_sig(src)
    except (OSError, ValueError):
        return None
    if (setts["force_reencode"] or meta.get("src") != src
            or meta.get("keyint") != KEYINT
            or meta.get("checks") != SOURCE_CHECKS
            or any(meta.get(k) != v for k, v in sig.items())):
        return None
    warns = meta.get("warn") or []
    if setts["camera_grade"] and warns:
        return None
    return warns


def materialize_ts(src, ts_path, setts, warns):
    """Fallback for a directly-served original whose publisher died: build
    the TS copy (remux, re-encode if even that fails) that it is served from
    from now on — the next startup finds it fresh and keeps using it."""
    log("direct publish of {} failed — building TS copy {}".format(src, ts_path))
    _rm(ts_path + DIRECT_SUFFIX)
    if remux_to_ts(src, ts_path):
        if warns:
            _stamp(ts_path, warn=warns)
        return True
    log("remux failed, re-encoding instead: {}".format(src))
    return transcode(src, ts_path, setts, ffprobe_info(src))


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
    out_dur = MINI_FREEZE_S + dur
    if with_tail_black:
        black = MINI_LOOP_S - MINI_FREEZE_S - dur
        if black > 0.05 and dur > 0:
            vf += ":stop_duration={:.3f}:stop_mode=add:color=black".format(black)
            out_dur = MINI_LOOP_S
        else:
            log("mini: {} clip {:.1f}s + {}s freeze >= {}s loop — no black tail"
                .format(src, dur, MINI_FREEZE_S, MINI_LOOP_S))

    tmp = _tmp_for(dst)
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
           "-i", src, "-vf", vf,
           "-c:v", "libx264", "-preset", X264_PRESET,
           "-x264-params", X264_PARAMS,
           "-pix_fmt", "yuv420p", "-an", "-f", "mpegts", tmp]
    r = _ffmpeg(cmd, out_dur if dur else 0.0)
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
    dur = media_duration(src)
    r = _ffmpeg(cmd, dur + 2 * CONCAT_FREEZE_S + CONCAT_GAP_S if dur else 0.0)
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
    built = 0
    for i, (src, stem) in enumerate(items, 1):
        seg = os.path.join(out_dir, "{}__seg{}_{}x{}_f{}.ts".format(
            stem, i, w, h, fps_s))
        # each segment owns 1/n of the chain's progress
        _TASK.span = ((i - 1) / len(items), 1.0 / len(items))
        if is_fresh(seg, src, setts, check_grade=False):
            log("cached: {}".format(seg))
        else:
            _report_step("Building chain", "{}/{}".format(i, len(items)))
            built += 1
            if not build_concat_segment(src, seg, i, w, h, fps):
                continue                   # bad video skipped; its number stays reserved
        segs.append((i, src, seg))
    _TASK.span = (0.0, 1.0)
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
             and not _stamp_stale(meta)
             and meta.get("segments") == seg_names
             and os.path.getmtime(chain) >= max(
                 os.path.getmtime(s) for _, _, s in segs))
    if fresh:
        log("cached: {}".format(chain))
        if not built:
            _report_note("cached")
    else:
        _report_step("Joining chain")
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
        return [], [(mount, chain, w, h, fps, setts["probe_idle"])]
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


def feeder_cmd(content, url, w, h, fps, signal_paths, idle="black"):
    """Probe feeder: idle (black, or a frozen frame with idle="frame") until
    any of signal_paths is touched, then content once, repeat (one
    continuous publish session — see probe_feeder.py).
    Two signal files per stream: the shared global one (fires every probe at
    once) and the stream's own (fires just this camera — no cross-talk)."""
    feeder = os.path.join(os.path.dirname(os.path.abspath(__file__)), "probe_feeder.py")
    cmd = [sys.executable, feeder,
           "--content", content, "--url", url,
           "--width", str(w), "--height", str(h),
           "--fps", str(fps), "--x264-params", X264_PARAMS,
           "--preset", X264_PRESET, "--idle", idle]
    for sp in signal_paths:
        cmd += ["--signal-file", sp]
    return cmd


# -------------------------------------------------------------- prepare -----

def prepare_source(cfg, workspace, src, folder, stem, mount_stem=None):
    """Prepare one source video end-to-end (transcode/remux, mini composite,
    rotation body, forward-rotated copies) and return its streams as
    (publishers [(mount, file, warns, fallback)],
     feeders [(mount, file, w, h, fps, idle)]).
    warns lists realism issues kept in an as-is file (camera_grade=false) —
    listed under the URL banner. fallback is set when `file` is the original
    served directly: (src, ts_path, setts, warns) for materialize_ts(). Touches only this video's
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
    ts_path = play_path           # workspace TS; play_path becomes src when direct
    direct_warns = _direct_cached(ts_path, src, setts)
    if is_fresh(play_path, src, setts):
        log("cached: {}".format(play_path))
        # as-is artifact (camera_grade=false): its realism issues were
        # recorded in the stamp at build time — reuse them for the banner
        warns = (_read_stamp(play_path) or {}).get("warn") or []
        _report_note("asis" if warns else "cached")
        _rm(ts_path + DIRECT_SUFFIX)   # a fallback TS supersedes direct serving
    elif direct_warns is not None:
        log("cached: direct {}".format(src))
        warns, play_path = direct_warns, src
        _report_note("asis" if warns else "cached")
    else:
        _report_step("Checking")
        info = ffprobe_info(src)
        reasons, warns = needs_transcode(src, setts, info)
        if reasons:
            log("transcoding [{}]: {} -> {}".format(",".join(reasons), src, play_path))
            _report_step("Re-encoding", _reason_text(reasons))
            _report_note("reencoded", _reason_text(reasons))
            warns = []   # a re-encode normalises everything
            if not transcode(src, play_path, setts, info):
                return pubs, feeders
        elif src.lower().endswith(DIRECT_EXTS):
            # nothing to change: serve the original in place instead of a
            # TS copy of it (half the disk). Should its publisher ever die
            # (the old MP4 loop-wrap failure), the supervisor builds the TS
            # copy then and switches the stream over — see materialize_ts().
            log("serving original directly{}: {}".format(
                " as-is [{}]".format(",".join(warns)) if warns else "", src))
            if warns:
                _report_note("asis")
            _write_direct(ts_path, src, warns)
            _rm(ts_path)                    # pre-2.10 remux copy, now dead weight
            _rm(ts_path + STAMP_SUFFIX)
            play_path = src
        else:
            log("remuxing to ts: {} -> {}".format(src, play_path))
            _report_step("Remuxing")
            # Stream-copy remux fails on containers with no usable
            # timestamps (e.g. AVI: "first pts and dts value must be set").
            # Fall back to a re-encode, which synthesises fresh PTS/DTS and
            # produces a clean loopable TS — so a quirky source still serves
            # (and a probe stream still gets a valid file to wrap).
            if not remux_to_ts(src, play_path):
                log("remux failed, re-encoding instead: {}".format(src))
                _report_step("Re-encoding", "remux failed")
                _report_note("reencoded", "remux failed")
                warns = []
                if not transcode(src, play_path, setts, info):
                    return pubs, feeders
            elif warns:
                _report_note("asis")
                log("serving as-is [{}] (camera_grade=false): {}".format(
                    ",".join(warns), play_path))
                _stamp(play_path, warn=warns)
    # direct-served original: what the supervisor needs to fall back to a TS
    fallback = (src, ts_path, setts, warns) if play_path == src else None

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
        else:
            _report_step("Building mini")
            if not build_mini(play_path, mini_content, cinfo,
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
        else:
            _report_step("Re-encoding", "shifted copies")
            if not _full_reencode(play_path, body, cut_frames):
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
            else:
                _report_step("Rotating copies")
                if build_forward(body, fwd, k):
                    src_file = fwd
            # build failed -> fall back to unshifted play_path
        if setts["probe"]:
            # the feeder re-encodes through its own sink, so served output
            # is always camera-grade regardless of the file's warnings
            feeders.append((mount, src_file, cinfo[1], cinfo[2], cinfo[3],
                            setts["probe_idle"]))
        else:
            # derived files (mini/body/fwd) are re-encoded and clean; only
            # the untouched as-is play file carries its realism warnings
            pubs.append((mount, src_file,
                         warns if src_file == play_path else [],
                         fallback if src_file == play_path else None))
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


WARN_TEXT = {"bframes": "B-frames", "gop": "long GOP"}
BANNER_W = 78      # ptyrun's pty is 80 columns; banner lines stay inside it
RED, GREEN, YELLOW, CYAN = "\033[31m", "\033[32m", "\033[33m", "\033[36m"
BOLD, DIM, RESET = "\033[1m", "\033[2m", "\033[0m"


def _paint(on):
    """paint(code, text) -> text wrapped in an ANSI code when colour is on."""
    return (lambda code, text: code + text + RESET) if on else (lambda code, text: text)


def _url_rows(base, mounts, mini_folders, warns=None, color=False):
    """URL lines (4-space indent) for `mounts` in order. A numbered mini
    folder's run collapses to its first and last URL with a count between
    them — dozens of URLs differing only by index would flood the screen
    (manifest.txt keeps the full map); a run holding a warned stream stays
    expanded so no warning hides in the fold. A warned URL (warns: mount ->
    [keys]) is painted red and followed by a dim "↳ reasons" line; the URL
    text itself stays bare so it copies cleanly."""
    warns = warns or {}
    paint = _paint(color)

    def row(m):
        if not warns.get(m):
            return ["    {}{}".format(base, m)]
        why = ", ".join(WARN_TEXT.get(k, k) for k in warns[m])
        return ["    " + paint(RED, base + m),
                paint(DIM, "      ↳ served as-is: {}".format(why))]

    order, runs = [], {}
    for m in mounts:
        mm = re.match(r"^/(.+)/\d+$", m)
        if mm and mm.group(1) in mini_folders:
            if mm.group(1) not in runs:
                order.append(("run", mm.group(1)))
                runs[mm.group(1)] = []
            runs[mm.group(1)].append(m)
        else:
            order.append(("url", m))
    out = []
    for kind, v in order:
        if kind == "url":
            out += row(v)
            continue
        run = runs[v]
        if len(run) <= 3 or any(warns.get(m) for m in run):
            for m in run:
                out += row(m)
            continue
        out += row(run[0])
        out.append(paint(DIM, "    ⋮  {} … {}  ({} more)".format(
            run[1][1:], run[-2][1:], len(run) - 2)))
        out += row(run[-1])
    return out


def _folder_of(mount):
    return mount[1:].split("/", 1)[0]


def banner_lines(base, port, transport, pub_mounts, feeder_mounts,
                 mini_folders, man_folders, color=False, log_hint=None):
    """The "streams live" banner, grouped by folder: a bold folder heading,
    its URLs below. URL lines carry nothing but the URL, so they copy
    cleanly; a stream served as-is with realism issues gets its URL in red
    (color=True) and a dim reasons line right under it. Probe folders add
    their fire command. A footer of dim labels follows: what as-is means
    (only when one is listed), fire-all, index maps, the log, how to stop.
    pub_mounts = [(mount, warns)], feeder_mounts = [(mount, idle)]."""
    paint = _paint(color)
    n = len(pub_mounts) + len(feeder_mounts)
    lines = ["", "{} {}  {}".format(
        paint(GREEN, "✔"),
        paint(BOLD, "{} stream{} live".format(n, "" if n == 1 else "s")),
        paint(DIM, "·  port {}  ·  {}".format(port, transport)))]

    warns = dict(pub_mounts)
    idles = dict(feeder_mounts)
    folders = []
    for m in [m for m, _ in pub_mounts] + [m for m, _ in feeder_mounts]:
        if _folder_of(m) not in folders:
            folders.append(_folder_of(m))
    for f in folders:
        pubs = [m for m, _ in pub_mounts if _folder_of(m) == f]
        feeds = [m for m, _ in feeder_mounts if _folder_of(m) == f]
        note = ""
        if f in mini_folders:
            note = "{} clips".format(len(pubs) + len(feeds))
        if feeds and not pubs:
            kinds = {idles[m] for m in feeds}
            what = ("black" if kinds == {"black"} else
                    "frozen frame" if kinds == {"frame"} else "black / frozen frame")
            note = (note + ", " if note else "") + \
                "{} until fired, then plays once".format(what)
        lines += ["", "  " + paint(BOLD, f) +
                  ("  " + paint(DIM, note) if note else "")]
        lines += _url_rows(base, pubs, mini_folders, warns, color)
        lines += _url_rows(base, feeds, mini_folders)
        if feeds:
            one = (feeds[0][1:].replace("/", "_") if len(feeds) == 1
                   else "{}_<name>".format(f))
            lines.append(paint(DIM, "    fire one  ") +
                         "touch workspace/{}_{}".format(SIGNAL_FILE, one))

    foot = []
    if any(w for _, w in pub_mounts):
        foot += [paint(DIM, l) for l in textwrap.wrap(
            "↳ as-is streams: not camera-like (slower joins, B-frame decode "
            "delay). camera_grade = true re-encodes them.",
            BANNER_W - 2, initial_indent="  ", subsequent_indent="    ",
            break_on_hyphens=False)]
        foot.append("")
    label = lambda k, v: paint(DIM, "  {:<6}".format(k)) + v
    if feeder_mounts:
        foot.append(label("fire", "touch workspace/{}".format(SIGNAL_FILE)) +
                    paint(DIM, "  (all probes)"))
    for i, f in enumerate(man_folders):
        foot.append(label("maps" if i == 0 else "",
                          "workspace/{}/{}".format(f, MANIFEST_NAME)))
    if log_hint:
        foot.append(label("logs", log_hint))
    foot.append(label("stop", "Ctrl-C"))
    return lines + [""] + foot


# ---------------------------------------------------------------- console ---

GO_COL = "\033[18G"   # = loader.GO_COL: past compose's 17-col line prefix
UP, EL = "\033[1A", "\033[K"
RECOVERED_SECONDS = 10.0   # a restarted stream alive this long is "back up"


def uptime_text(sec):
    """Short uptime: 42s, 18m, 1h18m."""
    sec = int(sec)
    if sec < 60:
        return "{}s".format(sec)
    m = sec // 60
    return "{}m".format(m) if m < 60 else "{}h{:02d}m".format(m // 60, m % 60)


class Console:
    """Runtime output below the banner: timestamped event lines and, on a
    live terminal, one status line pinned under them (`● 70/70 publishing
    · up 18m`), redrawn in place. Same technique as loader.py: every line
    ends in a real newline, so compose prefixes it, and a redraw climbs
    one line and erases from column 18 only, never touching compose's
    prefix. Without a live terminal (piped, CI, dev logs on stdout) events
    are plain append-only lines and there is no status line."""

    def __init__(self, live, color, out=None):
        self.live = live
        self.paint = _paint(color)
        self.out = out or sys.stdout
        self.status = None
        self.lock = threading.Lock()

    def _write(self, text):
        self.out.write(text)
        self.out.flush()

    def event(self, glyph, code, text, tail="", final=False):
        """`HH:MM:SS  ! text tail` — glyph coloured, tail dim. final=True
        replaces the status line for good (a fatal event is the last
        word)."""
        p = self.paint
        line = "{}  {} {}".format(p(DIM, time.strftime("%H:%M:%S")),
                                  p(code, glyph), text)
        if tail:
            line += " " + p(DIM, tail)
        with self.lock:
            if self.status is None:
                self._write(line + "\n")
            elif final:
                self._write(UP + GO_COL + EL + line + "\n")
                self.status = None
            else:
                self._write(UP + GO_COL + EL + line + "\n" +
                            GO_COL + EL + self.status + "\n")

    def set_status(self, text):
        if not self.live:
            return
        with self.lock:
            if text == self.status:
                return
            self._write((UP if self.status is not None else "") +
                        GO_COL + EL + text + "\n")
            self.status = text

    def heartbeat(self, alive, total, up):
        p = self.paint
        self.set_status("{} {}/{} publishing  {}".format(
            p(CYAN, "●"), alive, total,
            p(DIM, "·  up {}".format(uptime_text(up)))))

    def stopping(self, n):
        p = self.paint
        self.set_status("{} {} Stopping {} stream{}".format(
            p(CYAN, "⠼"), p(BOLD, "rtsp-streamer"), n, "" if n == 1 else "s"))

    def stopped(self, up, took):
        p = self.paint
        line = "{} {} Stopped {}".format(
            p(GREEN, "✔"), p(BOLD, "rtsp-streamer"),
            p(DIM, "· up {} · {:.1f}s".format(uptime_text(up), took)))
        with self.lock:
            self._write((UP + GO_COL + EL if self.status is not None else "")
                        + line + "\n")
            self.status = None


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
    global LOG_FH, REPORTER

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

    color = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
    paint = _paint(color)

    # -- prepare: decide per video whether to stream the source directly or a
    #    cached transcoded copy from the workspace (loader animation in prod).
    #    The animated block needs a TTY on stdout — in the image that is
    #    always true (the entrypoint runs streamer.py under its own pty via
    #    ptyrun.py), so it animates under `docker compose up` with no `tty:`
    #    in the compose file. Without any TTY (direct run with piped output)
    #    prod falls back to append-only snapshot lines, one per video.
    nfolders = len({folder_and_stem(s, videos_dir)[0] for s in sources})
    say("{} {}".format(paint(BOLD, "RTSP Streamer"), paint(DIM, "{}  ·  {} video{} in {} folder{}".format(
        VERSION, len(sources), "" if len(sources) == 1 else "s",
        nfolders, "" if nfolders == 1 else "s"))))
    anim = None
    loader = None
    if not DEV:
        try:
            import loader
        except Exception:
            loader = None
        if loader is not None and sys.stdout.isatty():
            try:
                anim = loader.start()
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
    publishers = []     # (mount, file, warns, fallback)
    probe_feeders = []  # (mount, content_file, w, h, fps, idle)
    done = [0]
    done_lock = threading.Lock()

    if anim is not None:
        loader.set_items([(name, grp) for name, grp, _ in tasks])
        REPORTER = loader

    def run_job(task):
        name, _grp, fn = task
        _TASK.name, _TASK.span = name, (0.0, 1.0)
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
        REPORTER = None
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
    pub_mounts, feeder_mounts = [], []   # (mount, warns) / (mount, idle)
    # direct-served originals: mount -> (src, ts_path, setts, warns), the
    # TS fallback built if its publisher dies (see materialize_ts)
    fallbacks = {}
    for mount, fpath, warns, fallback in publishers:
        mount = unique_mount(mount)
        url = "rtsp://127.0.0.1:{}{}".format(port, mount)
        stream_cmds[mount] = publish_cmd(fpath, url)
        pub_mounts.append((mount, warns))
        if fallback:
            fallbacks[mount] = fallback
    for mount, fpath, w, h, fps, idle in probe_feeders:
        mount = unique_mount(mount)
        url = "rtsp://127.0.0.1:{}{}".format(port, mount)
        # per-stream signal alongside the global one: touch workspace/start
        # fires every probe (as before), touch workspace/start_<f>_<name>
        # fires just this camera
        own_sig = "{}_{}".format(SIGNAL_FILE, mount[1:].replace("/", "_"))
        stream_cmds[mount] = feeder_cmd(
            fpath, url, w, h, fps,
            [signal_path, os.path.join(workspace, own_sig)], idle)
        feeder_mounts.append((mount, idle))

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
    else:
        pending = set()

    for line in banner_lines(
            "rtsp://{}:{}".format(advertise, port), port, transport,
            pub_mounts, feeder_mounts, set(mini_index),
            sorted(set(mini_index) | set(concat_folders)),
            color=color, log_hint=None if DEV else "workspace/streamer.log"):
        say(line)
    say()

    t_live = time.time()     # uptime counts from here: the streams are live
    con = Console(live=sys.stdout.isatty() and not DEV, color=color)
    if pending:
        con.event("!", YELLOW, "{} not publishing after {:.0f}s".format(
            ", ".join(sorted(m[1:] for m in pending)) if len(pending) <= 3
            else "{} streams".format(len(pending)), READY_WAIT_SECONDS),
            "· retrying")

    last_watch = 0.0
    fb_building = {}     # ts_path -> {"ok": None|bool}: TS fallback builds in flight

    def start_fallback(fb):
        job = {"ok": None}
        fb_building[fb[1]] = job

        def build():
            try:
                job["ok"] = materialize_ts(*fb)
            except Exception:
                log("TS fallback FAILED for {}: {}".format(
                    fb[0], traceback.format_exc().strip()))
                job["ok"] = False
        threading.Thread(target=build, daemon=True).start()

    crashed = False
    try:
        while not stopping:
            time.sleep(1.0)
            if mtx.poll() is not None:
                log("mediamtx died (rc={}) — shutting down".format(mtx.returncode))
                con.event("✖", RED, paint(BOLD, "mediamtx died"),
                          "(rc={}) · shutting down, see streamer.log".format(mtx.returncode),
                          final=True)
                crashed = True
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
                            con.event("!", YELLOW, "{} stalled".format(mount[1:]),
                                      "(no data {:.0f}s) · restarting".format(
                                          now - st["bytes_ts"]))
                            p.kill()   # kill, not terminate: a wedged process may ignore TERM

            # finished TS fallback builds: repoint every mount served from
            # that original (running copies switch on their next restart)
            for ts_path, job in list(fb_building.items()):
                if job["ok"] is None:
                    continue
                del fb_building[ts_path]
                for m in [m for m, fb in fallbacks.items() if fb[1] == ts_path]:
                    del fallbacks[m]
                    if job["ok"]:
                        stream_cmds[m] = publish_cmd(
                            ts_path, "rtsp://127.0.0.1:{}{}".format(port, m))
                        procs[m]["recover_note"] = "· serving TS copy"
                    else:
                        con.event("!", YELLOW, "{} TS copy failed".format(m[1:]),
                                  "· retrying the original, see streamer.log")
                    if procs[m]["proc"] is None:
                        procs[m]["retry_at"] = now

            for mount, st in procs.items():
                if st["proc"] is not None and st["proc"].poll() is None:
                    if now - st["started"] > STABLE_RESET_SECONDS:
                        st["backoff"] = RESTART_BACKOFF_START
                    if st.get("recovering") and now - st["started"] >= RECOVERED_SECONDS:
                        st["recovering"] = False
                        con.event("✔", GREEN, "{} back up".format(mount[1:]),
                                  st.pop("recover_note", ""))
                    continue
                # stream process died
                if st["proc"] is not None and mount in fallbacks:
                    # an original served directly died: hold restarts until
                    # its TS copy is built, then serve that (materialize_ts)
                    log("stream {} exited (rc={}), switching to a TS copy".format(
                        mount, st["proc"].returncode))
                    con.event("!", YELLOW, "{} publisher exited".format(mount[1:]),
                              "(rc={}) · building TS copy".format(st["proc"].returncode))
                    st["proc"], st["retry_at"] = None, float("inf")
                    if fallbacks[mount][1] not in fb_building:
                        start_fallback(fallbacks[mount])
                elif st["proc"] is not None:
                    log("stream {} exited (rc={}), restarting in {:.0f}s".format(
                        mount, st["proc"].returncode, st["backoff"]))
                    con.event("!", YELLOW, "{} exited".format(mount[1:]),
                              "(rc={}) · restarting in {:.0f}s".format(
                                  st["proc"].returncode, st["backoff"]))
                    st["retry_at"] = now + st["backoff"]
                    st["backoff"] = min(st["backoff"] * 2, RESTART_BACKOFF_MAX)
                    st["proc"] = None
                elif now >= st["retry_at"]:
                    st["proc"] = subprocess.Popen(stream_cmds[mount],
                                                  stdout=proc_out, stderr=proc_out)
                    st["started"] = now
                    st["bytes"], st["bytes_ts"] = None, now
                    st["recovering"] = True

            con.heartbeat(sum(1 for st in procs.values()
                              if st["proc"] is not None and st["proc"].poll() is None),
                          len(procs), now - t_live)
    finally:
        t_stop = time.time()
        if not crashed:
            con.stopping(len(procs))
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
        if not crashed:
            con.stopped(t_stop - t_live, time.time() - t_stop)
    return 1 if crashed else 0


if __name__ == "__main__":
    sys.exit(main())
