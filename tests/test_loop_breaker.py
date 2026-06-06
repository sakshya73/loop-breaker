"""
Unit tests for Loop Breaker detection logic. Zero dependencies.

Run:  python3 -m unittest discover -s tests
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "hooks"))

import loop_breaker as lb  # noqa: E402


def cfg(**over):
    c = dict(lb.DEFAULTS)
    c.update(over)
    return c


def hist(calls):
    """Build history from (tool, args_dict) tuples."""
    out = []
    for tool, args in calls:
        canon = lb.canonical_args(args)
        out.append({"fp": lb.fingerprint(tool, canon), "args": canon[:2000], "tool": tool})
    return out


def incoming(tool, args):
    canon = lb.canonical_args(args)
    return lb.fingerprint(tool, canon), canon[:2000]


class Fingerprint(unittest.TestCase):
    def test_canonical_args_order_independent(self):
        a = lb.canonical_args({"x": 1, "y": 2})
        b = lb.canonical_args({"y": 2, "x": 1})
        self.assertEqual(a, b)

    def test_fingerprint_differs_by_args(self):
        f1 = lb.fingerprint("Bash", lb.canonical_args({"command": "ls"}))
        f2 = lb.fingerprint("Bash", lb.canonical_args({"command": "pwd"}))
        self.assertNotEqual(f1, f2)

    def test_similarity_bounds(self):
        self.assertEqual(lb.similarity("abc", "abc"), 1.0)
        self.assertEqual(lb.similarity("", "abc"), 0.0)
        self.assertTrue(0.0 <= lb.similarity("hello world", "hello there") <= 1.0)


class Consecutive(unittest.TestCase):
    def test_trips_at_threshold(self):
        c = cfg(consecutive_threshold=5)
        h = hist([("Edit", {"path": "a.py", "old": "x"})] * 4)
        fp, args = incoming("Edit", {"path": "a.py", "old": "x"})
        tripped, kind, _ = lb.detect(h, fp, args, c)
        self.assertTrue(tripped)
        self.assertEqual(kind, "consecutive")

    def test_does_not_trip_below_threshold(self):
        c = cfg(consecutive_threshold=5)
        h = hist([("Edit", {"path": "a.py", "old": "x"})] * 3)
        fp, args = incoming("Edit", {"path": "a.py", "old": "x"})
        tripped, _, _ = lb.detect(h, fp, args, c)
        self.assertFalse(tripped)

    def test_fuzzy_near_identical_counts(self):
        # Same edit retried with a trailing whitespace tweak each time.
        c = cfg(consecutive_threshold=4, fuzzy_threshold=0.9)
        base = "def foo(): return 1   # attempt"
        h = hist([("Edit", {"path": "a.py", "old": base + " "}),
                  ("Edit", {"path": "a.py", "old": base + "  "}),
                  ("Edit", {"path": "a.py", "old": base + "   "})])
        fp, args = incoming("Edit", {"path": "a.py", "old": base + "    "})
        tripped, kind, _ = lb.detect(h, fp, args, c)
        self.assertTrue(tripped)
        self.assertEqual(kind, "consecutive")


class ProductiveIterationNotFlagged(unittest.TestCase):
    def test_edit_test_edit_test_with_changing_edits_is_ok(self):
        # Classic fix->test->fix->test: edits differ, test command repeats but is
        # never consecutive, and the cycle has a changing element -> no trip.
        c = cfg()
        h = hist([
            ("Edit", {"path": "a.py", "old": "v1"}),
            ("Bash", {"command": "pytest"}),
            ("Edit", {"path": "a.py", "old": "v2"}),
            ("Bash", {"command": "pytest"}),
            ("Edit", {"path": "a.py", "old": "v3"}),
        ])
        fp, args = incoming("Bash", {"command": "pytest"})
        tripped, _, _ = lb.detect(h, fp, args, c)
        self.assertFalse(tripped)

    def test_reading_many_different_files_is_ok(self):
        c = cfg()
        h = hist([("Read", {"path": f"f{i}.py"}) for i in range(8)])
        fp, args = incoming("Read", {"path": "f8.py"})
        tripped, _, _ = lb.detect(h, fp, args, c)
        self.assertFalse(tripped)


class Cycle(unittest.TestCase):
    def test_abab_with_identical_args_trips(self):
        # Read same file, run same command, over and over -> stuck cycle.
        c = cfg(consecutive_threshold=99, cycle_reps=3, cycle_max_period=3)
        pattern = [("Read", {"path": "x.py"}), ("Bash", {"command": "make"})]
        h = hist(pattern * 2 + [pattern[0]])  # R R-Bash R R-Bash R(Read)
        fp, args = incoming("Bash", {"command": "make"})
        tripped, kind, _ = lb.detect(h, fp, args, c)
        self.assertTrue(tripped)
        self.assertEqual(kind, "cycle")

    def test_abab_with_changing_args_does_not_trip(self):
        c = cfg(consecutive_threshold=99, cycle_reps=3, cycle_max_period=3)
        h = hist([
            ("Edit", {"path": "x.py", "old": "1"}), ("Bash", {"command": "make"}),
            ("Edit", {"path": "x.py", "old": "2"}), ("Bash", {"command": "make"}),
            ("Edit", {"path": "x.py", "old": "3"}),
        ])
        fp, args = incoming("Bash", {"command": "make"})
        tripped, _, _ = lb.detect(h, fp, args, c)
        self.assertFalse(tripped)


class Budget(unittest.TestCase):
    def test_estimate_tokens_positive(self):
        self.assertGreaterEqual(lb.estimate_tokens(lb.canonical_args({"command": "ls -la"})), 3)


class ConfigLoading(unittest.TestCase):
    def test_env_override(self):
        os.environ["LOOP_BREAKER_CONSECUTIVE_THRESHOLD"] = "9"
        try:
            c = lb.load_config()
            self.assertEqual(c["consecutive_threshold"], 9)
        finally:
            del os.environ["LOOP_BREAKER_CONSECUTIVE_THRESHOLD"]

    def test_mode_env_override(self):
        os.environ["LOOP_BREAKER_MODE"] = "warn"
        try:
            c = lb.load_config()
            self.assertEqual(c["mode"], "warn")
        finally:
            del os.environ["LOOP_BREAKER_MODE"]


if __name__ == "__main__":
    unittest.main()
