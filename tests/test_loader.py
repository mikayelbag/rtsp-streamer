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


class TestBoundedBlock(unittest.TestCase):
    """Finished units scroll away as one-shot ✔ lines; the animated block
    stays a handful of lines no matter how many videos there are."""

    def test_block_bounded_and_done_scrolls(self):
        names = ["f/v{}".format(i) for i in range(30)]
        loader.set_items(names)
        for n in names[:10]:
            loader.item_start(n)
            loader.item_done(n)
        loader.item_start(names[10])
        perm, block = loader._frame(0, time.monotonic(), 80)
        plain_perm = [ANSI.sub("", l) for l in perm]
        plain_block = [ANSI.sub("", l) for l in block]
        self.assertEqual(len(perm), 10)              # one ✔ line per done
        self.assertTrue(all("Ready" in l for l in plain_perm))
        # block: header + 1 running + waiting count — never one per video
        self.assertEqual(len(block), 3)
        self.assertIn("10/30", plain_block[0])
        self.assertIn(names[10], plain_block[1])
        self.assertIn("19 waiting", plain_block[2])

    def test_done_lines_emit_once(self):
        loader.set_items(["a", "b"])
        loader.item_start("a")
        loader.item_done("a")
        perm1, _ = loader._frame(0, time.monotonic(), 80)
        perm2, _ = loader._frame(1, time.monotonic(), 80)
        self.assertEqual(len(perm1), 1)
        self.assertEqual(perm2, [])                  # already scrolled away


class TestGrouping(unittest.TestCase):
    """Numbered mini mounts share one row and one final first…last line."""

    def test_group_single_row_then_final_range(self):
        items = [("mini/{}".format(i), "mini") for i in range(1, 66)]
        items.append(("tfa/cam", None))
        loader.set_items(items)
        for i in range(1, 13):
            loader.item_start("mini/{}".format(i))
            loader.item_done("mini/{}".format(i))
        loader.item_start("mini/13")
        perm, block = loader._frame(0, time.monotonic(), 80)
        self.assertEqual(perm, [])       # group unfinished: nothing scrolls
        plain = [ANSI.sub("", l) for l in block]
        self.assertEqual(len(block), 3)  # header + group row + waiting
        self.assertIn("mini 12/65 Preparing", plain[1])
        self.assertIn("1 waiting", plain[2])
        for i in range(13, 66):
            loader.item_start("mini/{}".format(i))
            loader.item_done("mini/{}".format(i))
        perm, _ = loader._frame(1, time.monotonic(), 80)
        self.assertEqual(len(perm), 1)   # 65 clips -> ONE scrolled line
        self.assertIn("mini/1 … mini/65 Ready (65 clips)",
                      ANSI.sub("", perm[0]))


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
        self.assertIn("a/one", out)
        self.assertIn("a/two", out)
        self.assertIn(loader.TICK, out)
        self.assertTrue(out.endswith(loader.SHOW_CURSOR))
        plain = ANSI.sub("", out)
        self.assertEqual(sum("Ready" in l for l in plain.splitlines()), 3)

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
