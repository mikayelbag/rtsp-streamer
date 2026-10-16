"""Unit tests for streamer.py's pure logic — no ffmpeg, no network.

Run:  python3 -m unittest discover -s tests -v
"""

import configparser
import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import streamer


def make_cfg(text):
    cfg = configparser.ConfigParser(
        comment_prefixes=(";", "#"),
        inline_comment_prefixes=(";", "#"),
        interpolation=None,
    )
    cfg.read_string(text)
    return cfg


class TestSanitize(unittest.TestCase):
    def test_plain_name_untouched(self):
        self.assertEqual(streamer.sanitize("clip_01"), "clip_01")

    def test_spaces_become_underscores(self):
        self.assertEqual(streamer.sanitize("Passenger Dwell Time"),
                         "Passenger_Dwell_Time")

    def test_illegal_chars_replaced_and_collapsed(self):
        self.assertEqual(streamer.sanitize("a(b)!!c"), "a_b_c")

    def test_leading_trailing_underscores_trimmed(self):
        self.assertEqual(streamer.sanitize("(clip)"), "clip")

    def test_empty_result_falls_back(self):
        self.assertEqual(streamer.sanitize("()"), "stream")
        self.assertEqual(streamer.sanitize(""), "stream")

    def test_allowed_specials_kept(self):
        self.assertEqual(streamer.sanitize("a.b~c-d"), "a.b~c-d")


class TestFolderAndStem(unittest.TestCase):
    def test_video_in_subfolder(self):
        f, s = streamer.folder_and_stem("/v/ids/clip.mp4", "/v")
        self.assertEqual((f, s), ("ids", "clip"))

    def test_video_at_root_maps_to_root(self):
        f, s = streamer.folder_and_stem("/v/clip.mp4", "/v")
        self.assertEqual((f, s), ("root", "clip"))

    def test_nested_uses_immediate_parent(self):
        f, s = streamer.folder_and_stem("/v/a/b/clip.mp4", "/v")
        self.assertEqual((f, s), ("b", "clip"))

    def test_names_sanitized(self):
        f, s = streamer.folder_and_stem("/v/my cams/my clip (2).mp4", "/v")
        self.assertEqual((f, s), ("my_cams", "my_clip_2"))


class TestTranscodedName(unittest.TestCase):
    def setts(self, **kw):
        s = {"size": "keep", "fps": "keep", "bitrate_kbps": "keep",
             "force_reencode": False}
        s.update(kw)
        return s

    def test_no_settings_plain_ts(self):
        self.assertEqual(streamer.transcoded_name("clip", self.setts()),
                         "clip.ts")

    def test_tags_encode_settings(self):
        name = streamer.transcoded_name(
            "clip", self.setts(size="1280x720", fps="25", bitrate_kbps="2000",
                               force_reencode=True))
        self.assertEqual(name, "clip__r1280x720_f25_b2000k_enc.ts")

    def test_distinct_settings_distinct_names(self):
        a = streamer.transcoded_name("clip", self.setts(size="1280x720"))
        b = streamer.transcoded_name("clip", self.setts(fps="25"))
        self.assertNotEqual(a, b)


class TestSettingsFor(unittest.TestCase):
    def test_defaults(self):
        cfg = make_cfg("[streamer]\n")
        s = streamer.settings_for(cfg, "ids", "clip")
        self.assertEqual(s["copies"], 1)
        self.assertEqual(s["shift_frames"], 0)
        self.assertEqual(s["fps"], "keep")
        self.assertFalse(s["probe"])
        self.assertFalse(s["camera_grade"])

    def test_folder_overrides_global(self):
        cfg = make_cfg("[streamer]\ncopies = 2\n[folder:ids]\ncopies = 4\n")
        self.assertEqual(streamer.settings_for(cfg, "ids", "clip")["copies"], 4)
        self.assertEqual(streamer.settings_for(cfg, "tfa", "clip")["copies"], 2)

    def test_video_overrides_folder(self):
        cfg = make_cfg(
            "[streamer]\n[folder:ids]\ncopies = 4\n[video:ids/clip]\ncopies = 0\n")
        self.assertEqual(streamer.settings_for(cfg, "ids", "clip")["copies"], 0)
        self.assertEqual(streamer.settings_for(cfg, "ids", "other")["copies"], 4)

    def test_blank_value_leaves_default(self):
        cfg = make_cfg("[streamer]\nfps =\n")
        self.assertEqual(streamer.settings_for(cfg, "ids", "clip")["fps"], "keep")

    def test_bool_words(self):
        for word, want in (("1", True), ("true", True), ("YES", True),
                           ("on", True), ("0", False), ("false", False),
                           ("off", False)):
            cfg = make_cfg("[streamer]\nprobe = {}\n".format(word))
            self.assertEqual(
                streamer.settings_for(cfg, "f", "s")["probe"], want, word)


class TestValidateConfig(unittest.TestCase):
    def check(self, body):
        cfg = make_cfg("[streamer]\n" + body)
        streamer.validate_config(cfg)      # raises SystemExit on bad input

    def test_good_values_pass(self):
        self.check("copies = 3\nfps = 29.97\nsize = 1920x1080\n"
                   "bitrate_kbps = 2000\nprobe = true\n")

    def test_keep_passes_everywhere(self):
        self.check("fps = keep\nsize = keep\nbitrate_kbps = keep\n")

    def test_bad_int_exits(self):
        with self.assertRaises(SystemExit):
            self.check("copies = many\n")

    def test_bad_bool_exits(self):
        with self.assertRaises(SystemExit):
            self.check("probe = maybe\n")

    def test_bad_size_exits(self):
        with self.assertRaises(SystemExit):
            self.check("size = 1920*1080\n")

    def test_bad_fps_exits(self):
        with self.assertRaises(SystemExit):
            self.check("fps = fast\n")

    def test_zero_fps_exits(self):
        with self.assertRaises(SystemExit):
            self.check("fps = 0\n")

    def test_bad_value_in_folder_section_exits(self):
        with self.assertRaises(SystemExit):
            self.check("[folder:ids]\ncopies = x\n")

    def test_probe_idle_values(self):
        self.check("probe_idle = frame\n")
        self.check("probe_idle = black\n")
        with self.assertRaises(SystemExit):
            self.check("probe_idle = first\n")

    def test_unrelated_section_ignored(self):
        self.check("[something_else]\ncopies = not_an_int\n")


class TestStamp(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.dst = os.path.join(self.dir.name, "clip.ts")
        with open(self.dst, "wb") as f:
            f.write(b"x")

    def test_roundtrip(self):
        streamer._stamp(self.dst, warn=["bframes"], extra={"segments": ["a"]})
        meta = streamer._read_stamp(self.dst)
        self.assertEqual(meta["version"], streamer.VERSION)
        self.assertEqual(meta["keyint"], streamer.KEYINT)
        self.assertEqual(meta["warn"], ["bframes"])
        self.assertEqual(meta["segments"], ["a"])

    def test_missing_stamp_none(self):
        self.assertIsNone(streamer._read_stamp(self.dst))

    def test_corrupt_stamp_none(self):
        with open(self.dst + streamer.STAMP_SUFFIX, "w") as f:
            f.write("{not json")
        self.assertIsNone(streamer._read_stamp(self.dst))


class TestIsFresh(unittest.TestCase):
    """is_fresh paths that need no ffprobe: missing files, mtime, stamps."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.src = os.path.join(self.dir.name, "src.mp4")
        self.dst = os.path.join(self.dir.name, "dst.ts")
        with open(self.src, "wb") as f:
            f.write(b"s")
        self.setts = {"force_reencode": False, "camera_grade": False}

    def touch(self, path, mtime):
        with open(path, "ab"):
            pass
        os.utime(path, (mtime, mtime))

    def test_missing_dst_not_fresh(self):
        self.assertFalse(streamer.is_fresh(self.dst, self.src, self.setts))

    def test_force_reencode_never_fresh(self):
        self.touch(self.dst, os.path.getmtime(self.src) + 10)
        streamer._stamp(self.dst)
        setts = dict(self.setts, force_reencode=True)
        self.assertFalse(streamer.is_fresh(self.dst, self.src, setts))

    def test_stale_mtime_not_fresh(self):
        self.touch(self.dst, os.path.getmtime(self.src) - 10)
        streamer._stamp(self.dst)
        self.assertFalse(streamer.is_fresh(self.dst, self.src, self.setts))

    def test_stamped_matching_keyint_fresh(self):
        self.touch(self.dst, os.path.getmtime(self.src) + 10)
        streamer._stamp(self.dst)
        self.assertTrue(streamer.is_fresh(self.dst, self.src, self.setts))

    def test_keyint_change_invalidates(self):
        self.touch(self.dst, os.path.getmtime(self.src) + 10)
        streamer._stamp(self.dst)
        meta = streamer._read_stamp(self.dst)
        meta["keyint"] = streamer.KEYINT + 1
        with open(self.dst + streamer.STAMP_SUFFIX, "w") as f:
            json.dump(meta, f)
        self.assertFalse(streamer.is_fresh(self.dst, self.src, self.setts))

    def test_camera_grade_invalidates_warned_artifact(self):
        self.touch(self.dst, os.path.getmtime(self.src) + 10)
        streamer._stamp(self.dst, warn=["bframes"])
        self.assertTrue(streamer.is_fresh(self.dst, self.src, self.setts))
        setts = dict(self.setts, camera_grade=True)
        self.assertFalse(streamer.is_fresh(self.dst, self.src, setts))

    def test_x264_change_invalidates(self):
        self.touch(self.dst, os.path.getmtime(self.src) + 10)
        streamer._stamp(self.dst)
        meta = streamer._read_stamp(self.dst)
        meta["x264"] = "keyint=60:bframes=0"
        with open(self.dst + streamer.STAMP_SUFFIX, "w") as f:
            json.dump(meta, f)
        self.assertFalse(streamer.is_fresh(self.dst, self.src, self.setts))

    def test_pre_2_10_stamp_without_x264_rebuilds(self):
        self.touch(self.dst, os.path.getmtime(self.src) + 10)
        with open(self.dst + streamer.STAMP_SUFFIX, "w") as f:
            json.dump({"version": "2.9.0", "keyint": streamer.KEYINT}, f)
        self.assertFalse(streamer.is_fresh(self.dst, self.src, self.setts))

    def test_unstamped_no_grade_check_gets_stamped(self):
        self.touch(self.dst, os.path.getmtime(self.src) + 10)
        self.assertTrue(
            streamer.is_fresh(self.dst, self.src, self.setts, check_grade=False))
        self.assertIsNotNone(streamer._read_stamp(self.dst))


class TestDirect(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.src = os.path.join(self.dir.name, "src.mp4")
        self.ts = os.path.join(self.dir.name, "src.ts")
        with open(self.src, "wb") as f:
            f.write(b"s")
        self.setts = {"force_reencode": False, "camera_grade": False}

    def test_no_sidecar_undecided(self):
        self.assertIsNone(streamer._direct_cached(self.ts, self.src, self.setts))

    def test_roundtrip_keeps_warnings(self):
        streamer._write_direct(self.ts, self.src, ["bframes"])
        self.assertEqual(
            streamer._direct_cached(self.ts, self.src, self.setts), ["bframes"])

    def test_clean_source_empty_warnings(self):
        streamer._write_direct(self.ts, self.src, [])
        self.assertEqual(streamer._direct_cached(self.ts, self.src, self.setts), [])

    def test_replaced_source_undecided(self):
        streamer._write_direct(self.ts, self.src, [])
        with open(self.src, "ab") as f:
            f.write(b"more")
        self.assertIsNone(streamer._direct_cached(self.ts, self.src, self.setts))

    def test_camera_grade_rejects_warned(self):
        streamer._write_direct(self.ts, self.src, ["gop"])
        setts = dict(self.setts, camera_grade=True)
        self.assertIsNone(streamer._direct_cached(self.ts, self.src, setts))

    def test_force_reencode_undecided(self):
        streamer._write_direct(self.ts, self.src, [])
        setts = dict(self.setts, force_reencode=True)
        self.assertIsNone(streamer._direct_cached(self.ts, self.src, setts))


class TestBanner(unittest.TestCase):
    BASE = "rtsp://10.0.0.1:41000"
    U = "    rtsp://10.0.0.1:41000"          # a URL line: 4-space indent

    def lines(self, pubs=(), feeders=(), minis=(), mans=(), color=False,
              log_hint=None):
        return streamer.banner_lines(self.BASE, 41000, "tcp", list(pubs),
                                     list(feeders), set(minis), list(mans),
                                     color=color, log_hint=log_hint)

    def test_header_counts_streams(self):
        out = self.lines(pubs=[("/a/x", []), ("/b/y", [])],
                         feeders=[("/p/z", "black")])
        self.assertIn("✔ 3 streams live  ·  port 41000  ·  tcp", out)

    def test_grouped_by_folder(self):
        out = self.lines(pubs=[("/a/x", []), ("/b/y", []), ("/a/z", [])])
        a, b = out.index("  a"), out.index("  b")
        self.assertLess(a, b)
        self.assertEqual(out[a + 1:a + 3], [self.U + "/a/x", self.U + "/a/z"])
        self.assertEqual(out[b + 1], self.U + "/b/y")

    def test_url_lines_are_bare(self):
        out = self.lines(pubs=[("/a/x", ["bframes"]), ("/a/y", [])])
        self.assertIn(self.U + "/a/x", out)
        self.assertIn(self.U + "/a/y", out)

    def test_warning_under_its_url(self):
        out = self.lines(pubs=[("/a/x", ["bframes"]), ("/a/y", []),
                               ("/a/z", ["bframes", "gop"])])
        x = out.index(self.U + "/a/x")
        self.assertEqual(out[x + 1], "      ↳ served as-is: B-frames")
        y = out.index(self.U + "/a/y")
        self.assertNotIn("↳", out[y + 1])
        z = out.index(self.U + "/a/z")
        self.assertEqual(out[z + 1], "      ↳ served as-is: B-frames, long GOP")
        self.assertTrue(any("camera_grade = true" in l for l in out))

    def test_color_paints_only_glyphs_and_warned(self):
        out = self.lines(pubs=[("/a/x", ["gop"]), ("/a/y", [])], color=True)
        red = streamer.RED + self.BASE + "/a/x" + streamer.RESET
        self.assertIn("    " + red, out)
        self.assertIn(self.U + "/a/y", out)
        self.assertTrue(any(l.startswith(streamer.DIM + "      ↳") for l in out))
        self.assertTrue(out[1].startswith(streamer.GREEN + "✔" + streamer.RESET))

    def test_no_color_no_escapes(self):
        out = self.lines(pubs=[("/a/x", ["bframes"])], feeders=[("/p/q", "black")],
                         mans=["mini"], log_hint="workspace/streamer.log")
        self.assertFalse(any("\033" in l for l in out))

    def test_no_as_is_hint_when_clean(self):
        out = self.lines(pubs=[("/a/x", [])])
        self.assertFalse(any("↳" in l for l in out))

    def test_footer_labels(self):
        out = self.lines(pubs=[("/mini/1", [])], minis={"mini"},
                         mans=["mini", "chain"], log_hint="workspace/streamer.log")
        self.assertIn("  maps  workspace/mini/manifest.txt", out)
        self.assertIn("        workspace/chain/manifest.txt", out)
        self.assertIn("  logs  workspace/streamer.log", out)
        self.assertEqual(out[-1], "  stop  Ctrl-C")

    def test_probe_folder_fire_commands(self):
        out = self.lines(feeders=[("/p/a", "black"), ("/p/b", "black")])
        self.assertIn("  p  black until fired, then plays once", out)
        self.assertIn(self.U + "/p/a", out)
        self.assertIn("    fire one  touch workspace/start_p_<name>", out)
        self.assertIn("  fire  touch workspace/start  (all probes)", out)

    def test_single_probe_fire_names_it(self):
        out = self.lines(feeders=[("/p/door", "frame")])
        self.assertIn("    fire one  touch workspace/start_p_door", out)
        self.assertIn("  p  frozen frame until fired, then plays once", out)

    def test_mini_run_collapses(self):
        out = self.lines(pubs=[("/mini/{}".format(i), []) for i in range(1, 11)],
                         minis={"mini"})
        self.assertIn("  mini  10 clips", out)
        self.assertIn(self.U + "/mini/1", out)
        self.assertIn(self.U + "/mini/10", out)
        self.assertIn("    ⋮  mini/2 … mini/9  (8 more)", out)
        self.assertNotIn(self.U + "/mini/5", out)

    def test_warned_mini_run_not_collapsed(self):
        pubs = [("/mini/{}".format(i), []) for i in range(1, 11)]
        pubs[4] = ("/mini/5", ["bframes"])
        out = self.lines(pubs=pubs, minis={"mini"})
        self.assertIn(self.U + "/mini/5", out)
        self.assertFalse(any("more)" in l for l in out))

    def test_lines_fit_pty(self):
        names = [("/folder/clip_{:03d}".format(i), ["bframes", "gop"]) for i in range(40)]
        out = self.lines(pubs=names, mans=["folder"], log_hint="workspace/streamer.log")
        self.assertTrue(all(len(l) <= streamer.BANNER_W for l in out))


class TestConsole(unittest.TestCase):
    def make(self, live):
        buf = io.StringIO()
        return streamer.Console(live=live, color=False, out=buf), buf

    def test_plain_events_append(self):
        con, buf = self.make(live=False)
        con.heartbeat(3, 3, 5)                 # no status line without a TTY
        con.event("!", streamer.YELLOW, "a/x exited", "(rc=1) · restarting in 2s")
        out = buf.getvalue()
        self.assertNotIn("publishing", out)
        self.assertRegex(out, r"^\d\d:\d\d:\d\d  ! a/x exited \(rc=1\) · restarting in 2s\n$")

    def test_status_pinned_below_events(self):
        con, buf = self.make(live=True)
        con.heartbeat(2, 3, 30)
        con.heartbeat(2, 3, 30)                # unchanged: no redraw
        con.event("✔", streamer.GREEN, "a/x back up")
        out = buf.getvalue()
        self.assertEqual(out.count("2/3 publishing"), 2)   # drawn, then redrawn under the event
        tail = out.split("a/x back up")[1]
        self.assertIn("● 2/3 publishing  ·  up 30s\n", tail)
        # redraws climb one line and never write left of column 18
        self.assertIn(streamer.UP + streamer.GO_COL + streamer.EL, out)
        self.assertNotIn("\r", out)

    def test_final_event_replaces_status(self):
        con, buf = self.make(live=True)
        con.heartbeat(1, 1, 5)
        con.event("✖", streamer.RED, "mediamtx died", final=True)
        self.assertTrue(buf.getvalue().endswith("mediamtx died\n"))
        self.assertIsNone(con.status)

    def test_stop_sequence(self):
        con, buf = self.make(live=True)
        con.heartbeat(4, 4, 10)
        con.stopping(4)
        con.stopped(4700, 0.8)
        out = buf.getvalue()
        self.assertIn("Stopping 4 streams", out)
        self.assertTrue(out.endswith("✔ rtsp-streamer Stopped · up 1h18m · 0.8s\n"))

    def test_uptime_text(self):
        self.assertEqual(streamer.uptime_text(42), "42s")
        self.assertEqual(streamer.uptime_text(18 * 60 + 5), "18m")
        self.assertEqual(streamer.uptime_text(78 * 60), "1h18m")


class TestProgressReports(unittest.TestCase):
    def setUp(self):
        self.calls = []
        test = self

        class Rec:
            def item_progress(self, name, f):
                test.calls.append(("progress", name, round(f, 3)))

            def item_step(self, name, label, detail=""):
                test.calls.append(("step", name, label, detail))

            def item_note(self, name, kind, detail=""):
                test.calls.append(("note", name, kind, detail))

        streamer.REPORTER = Rec()
        streamer._TASK.name, streamer._TASK.span = "knife/k", (0.0, 1.0)

    def tearDown(self):
        streamer.REPORTER = None
        streamer._TASK.name = None

    def test_reason_text(self):
        self.assertEqual(streamer._reason_text(["broken"]), "broken chain")
        self.assertEqual(streamer._reason_text(["fps", "size", "codec=mpeg4"]),
                         "settings, codec mpeg4")

    def test_no_reporter_no_calls(self):
        streamer.REPORTER = None
        streamer._report_step("Checking")      # must be a silent no-op

    def test_step_and_note_reach_reporter(self):
        streamer._report_step("Re-encoding", "broken chain")
        streamer._report_note("reencoded", "broken chain")
        self.assertEqual(self.calls, [("step", "knife/k", "Re-encoding", "broken chain"),
                                      ("note", "knife/k", "reencoded", "broken chain")])

    def test_ffmpeg_progress_is_out_time_over_duration(self):
        """A 30 min output 15 min in reports 0.5 (ffmpeg -progress lines)."""
        lines = ["frame=10\n", "out_time_us=900000000\n", "progress=continue\n",
                 "out_time_us=N/A\n", "out_time_us=1800000000\n", "progress=end\n"]

        class FakeProc:
            def __init__(self, cmd, **kw):
                FakeProc.cmd = cmd
                self.stdout = iter(lines)
                self.stderr = io.StringIO("")
                self.returncode = 0

            def wait(self):
                return 0

        old = streamer.subprocess.Popen
        streamer.subprocess.Popen = FakeProc
        try:
            r = streamer._ffmpeg(["ffmpeg", "-i", "x", "out.ts"], out_dur=1800.0)
        finally:
            streamer.subprocess.Popen = old
        self.assertEqual(r.returncode, 0)
        self.assertEqual(FakeProc.cmd[:4], ["ffmpeg", "-progress", "pipe:1", "-nostats"])
        self.assertEqual([c[2] for c in self.calls], [0.5, 1.0])

    def test_span_maps_segment_progress(self):
        streamer._TASK.span = (0.5, 0.25)      # segment 3 of 4
        lines = ["out_time_us=5000000\n"]

        class FakeProc:
            def __init__(self, cmd, **kw):
                self.stdout = iter(lines)
                self.stderr = io.StringIO("")
                self.returncode = 0

            def wait(self):
                return 0

        old = streamer.subprocess.Popen
        streamer.subprocess.Popen = FakeProc
        try:
            streamer._ffmpeg(["ffmpeg", "out.ts"], out_dur=10.0)
        finally:
            streamer.subprocess.Popen = old
        self.assertEqual(self.calls, [("progress", "knife/k", 0.625)])


class TestBrokenChain(unittest.TestCase):
    """_broken_chain() on canned trace_headers output (no ffmpeg)."""

    @staticmethod
    def trace(pics, gaps_ok=1, log2_fn=12):
        """pics = [(nal_type, ref_idc, frame_num)] -> trace_headers lines."""
        pre = "[trace_headers @ 0x1] "
        out = [pre + "33          log2_max_frame_num_minus4      0001101 = {}".format(log2_fn),
               pre + "51          gaps_in_frame_num_allowed_flag       1 = {}".format(gaps_ok)]
        for nal, ref, fn in pics:
            out += [pre + "1           nal_ref_idc                         11 = {}".format(ref),
                    pre + "3           nal_unit_type                    00101 = {}".format(nal),
                    pre + "8           first_mb_in_slice                    1 = 0",
                    pre + "17          frame_num                       0001 = {}".format(fn)]
        return [l + "\n" for l in out]

    def run_on(self, lines):
        class FakeProc:
            def __init__(self, *a, **kw):
                self.stderr = iter(lines)
            def kill(self):
                pass
            def wait(self):
                return 0
        real = streamer.subprocess.Popen
        streamer.subprocess.Popen = FakeProc
        try:
            return streamer._broken_chain("x.mp4")
        finally:
            streamer.subprocess.Popen = real

    @staticmethod
    def gop(n=6):
        """IDR + ref/non-ref pairs, the camera pattern (frame_num every 2)."""
        pics = [(5, 3, 0)]
        for i in range(1, n):
            pics.append((1, 2 if i % 2 else 0, (i + 1) // 2))
        return pics

    def test_clean_stream(self):
        self.assertIsNone(self.run_on(self.trace(self.gop() + self.gop())))

    def test_lost_idr_detected(self):
        pics = self.gop(8) + self.gop(8)[1:]       # second IDR dropped
        why = self.run_on(self.trace(pics))
        self.assertIn("lost IDR", why)

    def test_must_start_on_idr(self):
        self.assertIn("IDR", self.run_on(self.trace(self.gop()[1:])))

    def test_small_gap_ok_when_allowed(self):
        pics = [(5, 3, 0), (1, 2, 1), (1, 2, 4), (1, 2, 5)]
        self.assertIsNone(self.run_on(self.trace(pics, gaps_ok=1)))
        self.assertIsNotNone(self.run_on(self.trace(pics, gaps_ok=0)))

    def test_frame_num_wrap_ok(self):
        pics = [(5, 3, 0)] + [(1, 2, i % 16) for i in range(1, 40)]
        self.assertIsNone(self.run_on(self.trace(pics, log2_fn=0)))


class TestNeedsTranscode(unittest.TestCase):
    def setts(self, **kw):
        s = {"force_reencode": False, "fps": "keep", "size": "keep",
             "bitrate_kbps": "keep", "camera_grade": False}
        s.update(kw)
        return s

    def test_unreadable_forces(self):
        reasons, _ = streamer.needs_transcode("x", self.setts(), None)
        self.assertIn("unreadable", reasons)

    def test_bad_codec_forces(self):
        info = ("mpeg4", 640, 480, 30.0, 0)
        reasons, _ = streamer.needs_transcode("x", self.setts(), info)
        self.assertEqual(reasons, ["codec=mpeg4"])

    def test_settings_force(self):
        info = ("h264", 1920, 1080, 30.0, 0)
        reasons, _ = streamer.needs_transcode(
            "x", self.setts(fps="25", size="1280x720", bitrate_kbps="900",
                            force_reencode=True), info)
        self.assertEqual(sorted(reasons),
                         sorted(["forced", "fps", "size", "bitrate"]))

    def test_bframes_warn_by_default(self):
        info = ("h264", 1920, 1080, 30.0, 2)
        reasons, warns = streamer.needs_transcode("x", self.setts(), info)
        self.assertEqual(reasons, [])
        self.assertEqual(warns, ["bframes"])

    def test_bframes_force_with_camera_grade(self):
        info = ("h264", 1920, 1080, 30.0, 2)
        reasons, warns = streamer.needs_transcode(
            "x", self.setts(camera_grade=True), info)
        self.assertEqual(reasons, ["bframes"])
        self.assertEqual(warns, [])


class TestKeyint(unittest.TestCase):
    def setUp(self):
        self.addCleanup(streamer._set_keyint, streamer.KEYINT)

    def test_set_keyint_updates_params(self):
        streamer._set_keyint(30)
        self.assertEqual(streamer.KEYINT, 30)
        self.assertIn("keyint=30:min-keyint=30", streamer.X264_PARAMS)
        self.assertIn("bframes=0", streamer.X264_PARAMS)
        self.assertIn("repeat-headers=1", streamer.X264_PARAMS)
        # NVDEC surface pool: ref_frame_count must exceed 1 (see X264_REF)
        self.assertIn("ref={}".format(streamer.X264_REF), streamer.X264_PARAMS)


class TestParsePorts(unittest.TestCase):
    def test_good_list(self):
        self.assertEqual(streamer.parse_ports("41000, 41001,41002"),
                         [41000, 41001, 41002])

    def test_single(self):
        self.assertEqual(streamer.parse_ports("8554"), [8554])

    def test_bad_value_exits(self):
        with self.assertRaises(SystemExit):
            streamer.parse_ports("41000, oops")

    def test_out_of_range_exits(self):
        with self.assertRaises(SystemExit):
            streamer.parse_ports("70000")

    def test_empty_exits(self):
        with self.assertRaises(SystemExit):
            streamer.parse_ports("")


if __name__ == "__main__":
    unittest.main()
