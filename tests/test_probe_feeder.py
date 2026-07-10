"""Unit tests for probe_feeder.py's pure pieces — no ffmpeg spawned."""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import probe_feeder


class TestBlackFrame(unittest.TestCase):
    def test_yuv420p_size(self):
        w, h = 1920, 1080
        frame = probe_feeder.black_frame(w, h)
        self.assertEqual(len(frame), w * h * 3 // 2)

    def test_limited_range_black(self):
        frame = probe_feeder.black_frame(4, 4)
        ysize = 16
        self.assertEqual(set(frame[:ysize]), {16})       # Y plane
        self.assertEqual(set(frame[ysize:]), {128})      # U+V planes


class TestSignalMtime(unittest.TestCase):
    def test_existing_file(self):
        with tempfile.NamedTemporaryFile() as f:
            m = probe_feeder.signal_mtime(f.name)
            self.assertAlmostEqual(m, os.stat(f.name).st_mtime)

    def test_missing_file_none(self):
        self.assertIsNone(probe_feeder.signal_mtime("/nonexistent/nope"))


if __name__ == "__main__":
    unittest.main()
