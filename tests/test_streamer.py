"""Unit tests for streamer.py's pure logic — no ffmpeg, no network.

Run:  python3 -m unittest discover -s tests -v
"""

import configparser
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

    def lines(self, pubs=(), feeders=(), minis=(), mans=()):
        return streamer.banner_lines(self.BASE, 41000, "tcp", list(pubs),
                                     list(feeders), set(minis), list(mans))

    def test_url_lines_are_bare(self):
        out = self.lines(pubs=[("/a/x", ["bframes"]), ("/a/y", [])])
        self.assertIn("  rtsp://10.0.0.1:41000/a/x", out)
        self.assertIn("  rtsp://10.0.0.1:41000/a/y", out)

    def test_warnings_grouped_by_reason(self):
        out = self.lines(pubs=[("/a/x", ["bframes"]), ("/a/y", ["gop"]),
                               ("/a/z", ["bframes", "gop"])])
        bf = [l for l in out if l.strip().startswith("B-frames")]
        gop = [l for l in out if l.strip().startswith("long GOP")]
        self.assertEqual(len(bf), 1)
        self.assertIn("a/x", bf[0])
        self.assertIn("a/z", bf[0])
        self.assertIn("a/y", gop[0])
        self.assertNotIn("a/x", gop[0])

    def test_no_warning_block_when_clean(self):
        out = self.lines(pubs=[("/a/x", [])])
        self.assertFalse(any("as-is" in l for l in out))

    def test_probe_fire_instructions_once(self):
        out = self.lines(feeders=[("/p/a", "black"), ("/p/b", "black")])
        self.assertEqual(sum("touch workspace" in l for l in out), 2)
        self.assertIn("  rtsp://10.0.0.1:41000/p/a", out)
        self.assertTrue(any("black until fired" in l for l in out))

    def test_probe_frame_idle_heading(self):
        out = self.lines(feeders=[("/p/a", "frame")])
        self.assertTrue(any("frozen frame until fired" in l for l in out))

    def test_mini_run_collapses(self):
        out = self.lines(pubs=[("/mini/{}".format(i), []) for i in range(1, 11)],
                         minis={"mini"})
        self.assertIn("  rtsp://10.0.0.1:41000/mini/1", out)
        self.assertIn("  rtsp://10.0.0.1:41000/mini/10", out)
        self.assertTrue(any("(8 more)" in l for l in out))
        self.assertNotIn("  rtsp://10.0.0.1:41000/mini/5", out)

    def test_lines_fit_pty(self):
        names = [("/folder/clip_{:03d}".format(i), ["bframes"]) for i in range(40)]
        out = self.lines(pubs=names)
        warn = [l for l in out if l.startswith("    ") or l.startswith(" " * 15)]
        self.assertTrue(warn)
        self.assertTrue(all(len(l) <= streamer.BANNER_W for l in warn))


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
