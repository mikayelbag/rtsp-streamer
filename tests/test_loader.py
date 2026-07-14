"""Unit tests for loader.py's display math — no TTY needed."""

import io
import os
import re
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import loader

ANSI = re.compile(r"\033\[[0-9;?]*[A-Za-z]")


class TestElapsed(unittest.TestCase):
    def test_seconds(self):
        self.assertEqual(loader._elapsed(3.51), "3.5s")
        self.assertEqual(loader._elapsed(59.94), "59.9s")

    def test_minutes(self):
        self.assertEqual(loader._elapsed(66), "1m06s")
        self.assertEqual(loader._elapsed(125.7), "2m05s")


class TestBarAt(unittest.TestCase):
    def test_empty(self):
        fill, track = loader._bar_at(0)
        self.assertEqual(fill, "")
        self.assertEqual(len(track), loader.BAR_W)

    def test_full(self):
        fill, track = loader._bar_at(loader.BAR_W * 8)
        self.assertEqual(fill, loader.FULL * loader.BAR_W)
        self.assertEqual(track, "")

    def test_partial_cell(self):
        fill, track = loader._bar_at(11)          # 1 full cell + 3 dots
        self.assertEqual(fill, loader.FULL + loader.CELL[2])
        self.assertEqual(len(fill) + len(track), loader.BAR_W)

    def test_clamps_out_of_range(self):
        self.assertEqual(loader._bar_at(-5)[0], "")
        self.assertEqual(loader._bar_at(10**6)[0], loader.FULL * loader.BAR_W)


class TestAdvance(unittest.TestCase):
    def test_monotonic_toward_target(self):
        shown = 0.0
        for _ in range(200):
            nxt = loader._advance(shown, done=2, total=4)
            self.assertGreaterEqual(nxt, shown)
            shown = nxt
        # settles at (or creeps just past) the real position, but never
        # claims the in-progress video
        span = loader.BAR_W * 8 / 4
        self.assertGreaterEqual(shown, span * 2)
        self.assertLess(shown, span * 3)

    def test_all_done_stops_at_target(self):
        shown = 0.0
        for _ in range(500):
            shown = loader._advance(shown, done=4, total=4)
        self.assertEqual(shown, loader.BAR_W * 8)

    def test_zero_total_no_move(self):
        self.assertEqual(loader._advance(5.0, done=0, total=0), 5.0)


class TestSnapshot(unittest.TestCase):
    def test_counts_shown(self):
        s = loader.snapshot(2, 5)
        self.assertIn("2/5", s)
        self.assertNotIn("\033", s)          # no ANSI in log-safe output

    def test_zero_total_safe(self):
        self.assertIn("0/0", loader.snapshot(0, 0))


class TestRow(unittest.TestCase):
    def test_right_aligned_elapsed(self):
        row = loader._row("name", "3.5s", 40)
        visible = re.sub(r"\033\[[0-9;]*m", "", row)
        self.assertEqual(len(visible), 40)     # elapsed lands at the edge
        self.assertTrue(row.endswith("3.5s" + loader.RESET))

    def test_long_left_truncated(self):
        row = loader._row("x" * 100, "3.5s", 40)
        self.assertIn("…", row)

    def test_tiny_width_does_not_crash(self):
        loader._row("some long name here", "10.5s", 5)


class TestDockerPullBlock(unittest.TestCase):
    """docker-pull layout: summary header is always the block's first line,
    below it one fixed row per folder that changes state in place."""

    def test_header_first_one_row_per_folder(self):
        names = ["ids/v{}".format(i) for i in range(30)]
        names += ["tfa/cam", "concat/chain"]
        loader.set_items(names)
        for n in names[:10]:
            loader.item_start(n)
            loader.item_done(n)
        loader.item_start("ids/v10")
        block = loader._frame(0, time.monotonic(), 80)
        plain = [ANSI.sub("", l) for l in block]
        # header + ids + tfa + concat — never one line per video
        self.assertEqual(len(block), 4)
        self.assertIn("10/32", plain[0])                 # summary stays on top
        self.assertIn("ids 10/30 Preparing v10", plain[1])
        self.assertIn("tfa Waiting", plain[2])
        self.assertIn("concat Waiting", plain[3])
        self.assertTrue(all(l.startswith("rtsp_streamer  | ")
                            for l in plain))             # gutter on every line

    def test_rows_change_state_in_place(self):
        loader.set_items(["a/one", "a/two", "b/one"])
        block0 = loader._frame(0, time.monotonic(), 80)
        for n in ("a/one", "a/two"):
            loader.item_start(n)
            loader.item_done(n)
        block1 = loader._frame(1, time.monotonic(), 80)
        self.assertEqual(len(block0), len(block1))       # nothing scrolls away
        plain = [ANSI.sub("", l) for l in block1]
        self.assertIn("2/3", plain[0])                   # header still first
        self.assertIn("a Ready (2 videos)", plain[1])    # ✔ in its own row
        self.assertIn("b Waiting", plain[2])

    def test_group_row_counts_then_final_count(self):
        items = [("mini/{}".format(i), "mini") for i in range(1, 66)]
        items.append(("tfa/cam", None))
        loader.set_items(items)
        for i in range(1, 13):
            loader.item_start("mini/{}".format(i))
            loader.item_done("mini/{}".format(i))
        loader.item_start("mini/13")
        block = loader._frame(0, time.monotonic(), 80)
        plain = [ANSI.sub("", l) for l in block]
        self.assertEqual(len(block), 3)  # header + mini row + tfa row
        self.assertIn("mini 12/65 Preparing", plain[1])
        self.assertNotIn("Preparing 13", plain[1])       # grouped: no name
        for i in range(13, 66):
            loader.item_start("mini/{}".format(i))
            loader.item_done("mini/{}".format(i))
        block = loader._frame(1, time.monotonic(), 80)
        plain = [ANSI.sub("", l) for l in block]
        self.assertEqual(len(block), 3)
        self.assertIn("mini Ready (65 clips)", plain[1])

    def test_short_terminal_collapses_waiting_rows(self):
        loader.set_items(["f{}/v".format(i) for i in range(30)])
        loader.item_start("f0/v")
        block = loader._frame(0, time.monotonic(), 80, rows=10)
        plain = [ANSI.sub("", l) for l in block]
        self.assertLessEqual(len(block), 8)              # fits rows-2
        self.assertIn("folders Waiting", plain[-1])
        self.assertIn("f0", plain[1])                    # active row kept


class TestPersistedFinalFrame(unittest.TestCase):
    """stop() leaves the finished block in scrollback like docker pull."""

    def _run_finalized(self):
        buf = io.StringIO()
        old_out, old_gts = sys.stdout, loader.shutil.get_terminal_size
        sys.stdout = buf
        loader.shutil.get_terminal_size = (
            lambda fallback=(80, 24): os.terminal_size((80, 24)))
        try:
            ev = threading.Event()
            ev.set()          # loop never spins: straight to the final frame
            loader._run(ev)
        finally:
            sys.stdout = old_out
            loader.shutil.get_terminal_size = old_gts
        return buf.getvalue()

    def test_block_persists_on_stop(self):
        loader.set_items(["a/one", "a/two"])
        for n in ("a/one", "a/two"):
            loader.item_start(n)
            loader.item_done(n)
        out = self._run_finalized()
        plain = ANSI.sub("", out)
        self.assertIn("a Ready (2 videos)", plain)
        self.assertIn("2/2 Ready", plain)
        self.assertIn(loader.TICK, out)
        self.assertTrue(out.endswith(loader.SHOW_CURSOR))
        # header + folder row, both ✔ — the whole block stays put
        self.assertEqual(sum("Ready" in l for l in plain.splitlines()), 2)

    def test_no_items_leaves_nothing(self):
        loader.set_items([])
        out = self._run_finalized()
        self.assertEqual(ANSI.sub("", out).strip(), "")


class TestStateMachine(unittest.TestCase):
    def test_item_lifecycle(self):
        loader.set_items(["a", "b"])
        loader.item_start("a")
        loader.item_done("a")
        self.assertEqual(loader._state["a"][0], loader.ST_DONE)
        self.assertEqual(loader._state["b"][0], loader.ST_WAIT)

    def test_done_without_start_records_times(self):
        loader.set_items(["a"])
        loader.item_done("a")
        st = loader._state["a"]
        self.assertEqual(st[0], loader.ST_DONE)
        self.assertIsNotNone(st[1])
        self.assertIsNotNone(st[2])

    def test_unknown_item_ignored(self):
        loader.set_items(["a"])
        loader.item_start("ghost")
        loader.item_done("ghost")


if __name__ == "__main__":
    unittest.main()
