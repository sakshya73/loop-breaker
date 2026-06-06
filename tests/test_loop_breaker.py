"""
Unit tests for Loop Breaker detection logic. Zero dependencies.

Run:  python3 -m unittest discover -s tests -v

Coverage mirrors the adversarial hardening pass: false positives that must NOT
trip (sibling files, growing writes, todo progress, parameterized re-runs,
pagination, read-only diagnostics), false negatives that MUST trip (long cycles,
retry storms, drift), plus config/state robustness.
"""

import os
import sys
import json
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "hooks"))

import loop_breaker as lb  # noqa: E402


def cfg(**over):
    c = dict(lb.DEFAULTS)
    c.update(over)
    return lb._clamp(c)


def entry(tool, args):
    return {
        "fp": lb.exact_fingerprint(tool, args),
        "sfp": lb.structural_fingerprint(tool, args),
        "tool": tool,
        "ro": lb.is_read_only(tool, args),
    }


def hist(calls):
    return [entry(t, a) for t, a in calls]


def incoming(tool, args):
    return lb.exact_fingerprint(tool, args), lb.structural_fingerprint(tool, args), lb.is_read_only(tool, args)


def run(calls, **over):
    """Feed a full call list through detect() call-by-call; return (tripped, kind) at the LAST call."""
    c = cfg(**over)
    h = []
    tripped, kind = False, ""
    for tool, args in calls:
        fp, sfp, ro = incoming(tool, args)
        tripped, kind, _, _ = lb.detect(h, fp, sfp, ro, c)
        h.append(entry(tool, args))
    return tripped, kind


class Fingerprint(unittest.TestCase):
    def test_order_independent(self):
        self.assertEqual(lb.canonical_args({"x": 1, "y": 2}), lb.canonical_args({"y": 2, "x": 1}))

    def test_whitespace_only_diff_is_same(self):
        a = lb.fingerprint("Bash", lb.canonical_args({"command": "npm   test"}))
        b = lb.fingerprint("Bash", lb.canonical_args({"command": "npm test"}))
        self.assertEqual(a, b)

    def test_distinct_args_differ(self):
        a = lb.fingerprint("Bash", lb.canonical_args({"command": "ls"}))
        b = lb.fingerprint("Bash", lb.canonical_args({"command": "pwd"}))
        self.assertNotEqual(a, b)


class StructuralFingerprint(unittest.TestCase):
    def test_uuid_and_timestamp_collapse(self):
        a = lb.structural_fingerprint("Bash", {"command": "retry", "id": "550e8400-e29b-41d4-a716-446655440000", "ts": 1717000000000})
        b = lb.structural_fingerprint("Bash", {"command": "retry", "id": "f47ac10b-58cc-4372-a567-0e02b2c3d479", "ts": 1717000999999})
        self.assertEqual(a, b)

    def test_volatile_key_collapses(self):
        a = lb.structural_fingerprint("Bash", {"command": "retry", "attempt": 1})
        b = lb.structural_fingerprint("Bash", {"command": "retry", "attempt": 9})
        self.assertEqual(a, b)

    def test_distinct_content_stays_distinct(self):
        a = lb.structural_fingerprint("Edit", {"file_path": "a.py", "new_string": "def foo(): pass"})
        b = lb.structural_fingerprint("Edit", {"file_path": "a.py", "new_string": "def bar(): pass"})
        self.assertNotEqual(a, b)


class IgnoredFields(unittest.TestCase):
    def test_description_ignored(self):
        # Bash includes a human-written "description" that often varies per retry.
        a = lb.exact_fingerprint("Bash", {"command": "echo hi", "description": "attempt 1"})
        b = lb.exact_fingerprint("Bash", {"command": "echo hi", "description": "attempt 2"})
        self.assertEqual(a, b)

    def test_command_still_distinguishes(self):
        a = lb.exact_fingerprint("Bash", {"command": "echo a", "description": "x"})
        b = lb.exact_fingerprint("Bash", {"command": "echo b", "description": "x"})
        self.assertNotEqual(a, b)

    def test_loop_with_varying_description_trips(self):
        calls = [("Bash", {"command": "echo loop", "description": f"attempt {i}"}) for i in range(5)]
        tripped, kind = run(calls)
        self.assertTrue(tripped)
        self.assertEqual(kind, "consecutive")

    def test_description_preserved_for_non_bash(self):
        # For tools where 'description' is the semantic payload (e.g. MCP CreateTask),
        # different descriptions must stay DISTINCT (the dropping is Bash-scoped).
        a = lb.exact_fingerprint("CreateTask", {"name": "t", "description": "Sync from prod"})
        b = lb.exact_fingerprint("CreateTask", {"name": "t", "description": "Wipe DB"})
        self.assertNotEqual(a, b)

    def test_non_bash_description_loop_not_blocked(self):
        # Five genuinely different tasks must NOT be merged into a stuck loop.
        calls = [("CreateTask", {"name": "t", "description": f"distinct task {i}"}) for i in range(5)]
        self.assertFalse(run(calls)[0])


class ReadOnly(unittest.TestCase):
    def test_read_tool(self):
        self.assertTrue(lb.is_read_only("Read", {"file_path": "a.py"}))

    def test_git_status(self):
        self.assertTrue(lb.is_read_only("Bash", {"command": "git status"}))

    def test_chained_command_not_read_only(self):
        self.assertFalse(lb.is_read_only("Bash", {"command": "git status && rm -rf /"}))

    def test_mutating_command(self):
        self.assertFalse(lb.is_read_only("Bash", {"command": "rm file"}))


class Consecutive(unittest.TestCase):
    def test_trips_at_threshold(self):
        calls = [("Edit", {"file_path": "a.py", "old_string": "x"})] * 5
        tripped, kind = run(calls)
        self.assertTrue(tripped)
        self.assertEqual(kind, "consecutive")

    def test_not_below_threshold(self):
        calls = [("Edit", {"file_path": "a.py", "old_string": "x"})] * 4
        self.assertFalse(run(calls)[0])

    def test_whitespace_run_only_retries_trip(self):
        # Same command, only the length of an internal whitespace run differs;
        # _norm_ws collapses runs so these are treated as the same stuck call.
        calls = [("Bash", {"command": "npm" + " " * (i + 1) + "test"}) for i in range(5)]
        self.assertTrue(run(calls)[0])


class FalsePositives_MustNotTrip(unittest.TestCase):
    def test_distinct_sibling_files(self):
        base = "/Users/dev/monorepo/packages/core/src/internal/modules/feature_"
        for suffixes in (["A", "B", "C", "D", "E"], ["foo.h", "foo.c", "bar.h", "bar.c", "baz.h"]):
            calls = [("Read", {"file_path": base + s + ".ts"}) for s in suffixes]
            self.assertFalse(run(calls)[0], suffixes)

    def test_locale_files(self):
        calls = [("Read", {"file_path": f"/app/i18n/{lng}.json"}) for lng in ("en", "de", "fr", "es", "it")]
        self.assertFalse(run(calls)[0])

    def test_growing_write_same_file(self):
        header = "// generated\n" + "x" * 3000
        calls = [("Write", {"file_path": "/p/gen.js", "content": header + ("\nrow %d" % i) * (i + 1)}) for i in range(5)]
        self.assertFalse(run(calls)[0])

    def test_todo_progress(self):
        items = [{"id": n, "status": "pending"} for n in range(40)]
        calls = []
        for i in range(6):
            snap = [dict(it) for it in items]
            for n in range(30, 30 + i + 1):
                snap[n]["status"] = "completed"
            calls.append(("TodoWrite", {"todos": snap}))
        self.assertFalse(run(calls)[0])

    def test_bash_seed_variants(self):
        cmd = "python -m pytest tests/ -q --maxfail=1 " + "-x " * 50
        calls = [("Bash", {"command": cmd + f"--seed={i}"}) for i in range(5)]
        self.assertFalse(run(calls)[0])

    def test_poll_with_increasing_sleep(self):
        calls = [("Bash", {"command": f"kubectl get pods -n prod && sleep {n}"}) for n in (5, 10, 15, 20, 25)]
        self.assertFalse(run(calls)[0])

    def test_read_pagination(self):
        calls = [("Read", {"file_path": "/big.log", "limit": 100, "offset": o}) for o in (0, 100, 200, 300, 400)]
        self.assertFalse(run(calls)[0])

    def test_productive_fix_test_loop(self):
        calls = []
        for i in range(4):
            calls.append(("Edit", {"file_path": "a.py", "old_string": f"v{i}"}))
            calls.append(("Bash", {"command": "pytest"}))
        self.assertFalse(run(calls)[0])


class ReadOnlyCycleExemption(unittest.TestCase):
    def _diagnostic_cycle(self):
        pattern = [("Bash", {"command": "git status"}), ("Bash", {"command": "git diff --stat"})]
        return pattern * 4

    def test_read_only_alternation_exempt(self):
        self.assertFalse(run(self._diagnostic_cycle())[0])

    def test_exemption_can_be_disabled(self):
        tripped, kind = run(self._diagnostic_cycle(), read_only_cycle_exempt=False)
        self.assertTrue(tripped)
        self.assertEqual(kind, "cycle")


class FalseNegatives_MustTrip(unittest.TestCase):
    def test_period_4_cycle(self):
        pattern = [
            ("Read", {"file_path": "a.py"}),
            ("Edit", {"file_path": "a.py", "old_string": "x"}),
            ("Bash", {"command": "pytest"}),
            ("Read", {"file_path": "error.log"}),
        ]
        tripped, kind = run(pattern * 4)
        self.assertTrue(tripped)
        self.assertEqual(kind, "cycle")

    def test_period_5_edit_rotation(self):
        pattern = [("Edit", {"file_path": f"{c}.py", "old_string": c}) for c in "ABCDE"]
        self.assertTrue(run(pattern * 4)[0])

    def test_abab_identical_args(self):
        pattern = [("Read", {"file_path": "x.py"}), ("Bash", {"command": "make"})]
        tripped, kind = run(pattern * 4)
        self.assertTrue(tripped)
        self.assertEqual(kind, "cycle")

    def test_abab_changing_args_does_not_trip(self):
        calls = []
        for i in range(4):
            calls.append(("Edit", {"file_path": "x.py", "old_string": str(i)}))
            calls.append(("Bash", {"command": "make"}))
        self.assertFalse(run(calls)[0])

    def test_retry_storm_uuid_timestamp(self):
        import uuid
        calls = [("Bash", {"command": "deploy", "id": str(uuid.UUID(int=i)), "ts": 1717000000000 + i}) for i in range(6)]
        tripped, kind = run(calls)
        self.assertTrue(tripped)
        self.assertEqual(kind, "structural")

    def test_retry_storm_attempt_counter(self):
        calls = [("Bash", {"command": "deploy", "attempt": i}) for i in range(6)]
        tripped, kind = run(calls)
        self.assertTrue(tripped)
        self.assertEqual(kind, "structural")

    def test_drift_long_digit_run(self):
        calls = [("Edit", {"file_path": "a.py", "new_string": "A" * 100 + str(i) * 40}) for i in range(6)]
        tripped, kind = run(calls)
        self.assertTrue(tripped)
        self.assertEqual(kind, "structural")

    def test_structural_can_be_disabled(self):
        calls = [("Bash", {"command": "deploy", "attempt": i}) for i in range(6)]
        self.assertFalse(run(calls, structural_detection=False)[0])


class ConfigRobustness(unittest.TestCase):
    def test_string_threshold_coerced(self):
        c = cfg(consecutive_threshold="5")
        self.assertEqual(c["consecutive_threshold"], 5)

    def test_negative_threshold_falls_back(self):
        c = cfg(consecutive_threshold=-1)
        self.assertEqual(c["consecutive_threshold"], lb.DEFAULTS["consecutive_threshold"])

    def test_history_size_coupled_to_cycle(self):
        c = cfg(cycle_max_period=25, cycle_reps=3, history_size=60)
        self.assertGreaterEqual(c["history_size"], 75)

    def test_bad_mode_falls_back(self):
        self.assertEqual(cfg(mode="banana")["mode"], "kill")

    def test_env_override(self):
        os.environ["LOOP_BREAKER_CONSECUTIVE_THRESHOLD"] = "9"
        try:
            self.assertEqual(lb.load_config()["consecutive_threshold"], 9)
        finally:
            del os.environ["LOOP_BREAKER_CONSECUTIVE_THRESHOLD"]

    def test_clamp_missing_mode_does_not_crash(self):
        c = {k: v for k, v in lb.DEFAULTS.items() if k != "mode"}
        out = lb._clamp(c)
        self.assertEqual(out["mode"], "kill")


class StateRobustness(unittest.TestCase):
    def test_load_state_drops_non_dict_history(self):
        d = tempfile.mkdtemp()
        os.environ["LOOP_BREAKER_STATE_DIR"] = d
        try:
            path = os.path.join(d, "sess.json")
            with open(path, "w") as fh:
                json.dump({"calls": 1, "history": ["notadict", {"fp": "a", "sfp": "a", "tool": "X", "ro": False}]}, fh)
            st = lb.load_state("sess")
            self.assertTrue(all(isinstance(e, dict) for e in st["history"]))
            self.assertEqual(len(st["history"]), 1)
        finally:
            del os.environ["LOOP_BREAKER_STATE_DIR"]

    def test_detect_survives_non_dict_entry(self):
        # Should not raise even if a stray non-dict sneaks into history.
        fp, sfp, ro = incoming("Edit", {"file_path": "a.py", "old_string": "x"})
        try:
            lb.detect([{"fp": "z", "sfp": "z", "tool": "Edit", "ro": False}, "notadict"], fp, sfp, ro, cfg())
        except Exception as e:
            self.fail(f"detect raised on non-dict history entry: {e}")

    def test_load_state_coerces_null_counters(self):
        d = tempfile.mkdtemp()
        os.environ["LOOP_BREAKER_STATE_DIR"] = d
        try:
            with open(os.path.join(d, "n.json"), "w") as fh:
                json.dump({"calls": None, "est_tokens": None, "history": []}, fh)
            st = lb.load_state("n")
            self.assertEqual(st["calls"], 0)
            self.assertEqual(st["est_tokens"], 0)
        finally:
            del os.environ["LOOP_BREAKER_STATE_DIR"]


if __name__ == "__main__":
    unittest.main()
