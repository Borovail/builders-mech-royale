"""Checks of chronology, mode separation, and the fixed-trajectory bound."""
import importlib.util
import contextlib
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location(
    "score_calibration", Path(__file__).resolve().parents[1] / "tools/score_calibration.py")
calibration = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(calibration)


class ScoreCalibrationTests(unittest.TestCase):
    def test_no_history_of_changes_transfers_no_local_gain(self):
        result = calibration.predictions([4.875], [1.15], 4.2083333333)
        self.assertEqual(result["shrunk_local_delta"], (1.15, 0.0))

    def test_negative_association_transfers_no_local_gain(self):
        self.assertEqual(calibration.transfer_coefficient([5, 4], [1, 2]), 0.0)

    def test_future_label_cannot_change_previous_predictions(self):
        local, source, digest = calibration.load_local("original")
        original = calibration.OFFICIAL
        before = calibration.backtest("original", local, source, digest)
        try:
            calibration.OFFICIAL = (*original[:-1], 14.0)
            after = calibration.backtest("original", local, source, digest)
        finally:
            calibration.OFFICIAL = original
        self.assertEqual([row["prediction"] for row in before],
                         [row["prediction"] for row in after])
        self.assertNotEqual(before[-1]["absolute_error"], after[-1]["absolute_error"])

    def test_modes_use_their_own_local_reference(self):
        original, _, _ = calibration.load_local("original")
        fast, _, _ = calibration.load_local("per-observation")
        self.assertEqual(original[-1], 6.125)
        self.assertNotEqual(original[-1], fast[-1])
        with self.assertRaises(ValueError):
            calibration.load_local("different-engine")

    def test_unphysical_candidate_score_is_rejected(self):
        for candidate in (-1, 16, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                calibration.predictions([6], [2], candidate)

    def test_zone_deletion_leaves_more_than_official(self):
        bound = calibration.zone_bound()
        self.assertEqual(bound["maximum_zone_deletion"], 2)
        self.assertEqual(bound["remaining_gap"], 2.125)
        self.assertAlmostEqual(bound["maximum_fraction_of_gap"], 96 / 198)


class RunReportTests(unittest.TestCase):
    def report(self):
        metadata = copy.deepcopy(calibration.PINNED_METADATA)
        metadata.update({"count": 48, "seeds_start": 8101,
                         "engine_changes": ["local supply-ring subclass"],
                         "planning_clock": {
                             "mode": "fixed_counter",
                             "seconds_per_perf_counter_call": 0.000001,
                             "scope": "evaluated bot navigation module only"}})
        return {"status": "complete", "metadata": metadata,
                "summary": {"mean_score": 3.0, "matches": 48, "exceptions": 0},
                "matches": [{"seed": seed, "score": 3, "place": 13,
                             "scenario": "squad16", "exceptions": 0}
                            for seed in range(8101, 8149)]}

    def inspect(self, report):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run.json"
            path.write_text(json.dumps(report), encoding="utf-8")
            return calibration.inspect_run_report(path)

    def test_manifest_selects_both_histories(self):
        report = self.report()
        self.assertEqual(self.inspect(report)["mode"], "original")
        report["metadata"]["engine_changes"].append(
            "update house commands at each player observation")
        self.assertEqual(self.inspect(report)["mode"], "per-observation")

    def test_mean_is_recomputed_and_disagreement_blocks_calibration(self):
        report = self.report()
        report["summary"]["mean_score"] = 12.5
        result = self.inspect(report)
        self.assertEqual(result["raw_local_mean"], 3)
        self.assertIsNone(result["mode"])
        self.assertTrue(any("summary mean" in reason for reason in result["reasons"]))

    def test_failed_and_incomplete_runs_keep_only_raw_mean(self):
        report = self.report()
        report["status"] = "failed"
        result = self.inspect(report)
        self.assertEqual(result["raw_local_mean"], 3)
        self.assertIsNone(result["mode"])
        report = self.report()
        report["matches"].pop()
        self.assertIsNone(self.inspect(report)["mode"])

    def test_empty_or_nonfinite_data_have_no_mean(self):
        for matches in ([], [None], [{"score": float("nan")}],
                        [{"score": float("inf")}], [{"score": True}], [{"score": 16}]):
            report = self.report()
            report["matches"] = matches
            with self.subTest(matches=matches), self.assertRaises(ValueError):
                self.inspect(report)

    def test_duplicate_and_different_seeds_cannot_reuse_history(self):
        for replacement in (8101, 9101, "8148", True):
            report = self.report()
            report["matches"][-1]["seed"] = replacement
            with self.subTest(seed=replacement):
                self.assertIsNone(self.inspect(report)["mode"])

    def test_every_pinned_field_must_match(self):
        for key in calibration.PINNED_METADATA:
            report = self.report()
            report["metadata"][key] = "different"
            with self.subTest(key=key):
                self.assertIsNone(self.inspect(report)["mode"])

    def test_physics_overlay_is_rejected_even_without_declared_engine_change(self):
        for field in ("cube_size_sha256", "spawn_arena_sha256", "movement_arena_sha256"):
            report = self.report()
            report["metadata"][field] = "new physics"
            with self.subTest(field=field):
                self.assertIsNone(self.inspect(report)["mode"])
        report = self.report()
        report["metadata"]["engine_changes"].append("new physics")
        self.assertIsNone(self.inspect(report)["mode"])

    def test_wallclock_control_prints_raw_but_no_server_numbers(self):
        report = self.report()
        report["metadata"]["planning_clock"] = {"mode": "wall_clock"}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "external-control.json"
            path.write_text(json.dumps(report), encoding="utf-8")
            output = io.StringIO()
            with patch.object(sys, "argv", ["score_calibration.py", "--run-json", str(path)]), \
                    contextlib.redirect_stdout(output):
                calibration.main()
        text = output.getvalue()
        self.assertIn("3.000000", text)
        self.assertIn("No historical calibration", text)
        self.assertNotIn("previous_official", text)
        self.assertNotIn("2.00", text)


if __name__ == "__main__":
    unittest.main()
