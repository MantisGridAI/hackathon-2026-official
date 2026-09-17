"""Tiny synthetic measurements; none are real telemetry evidence."""
import json
import unittest

from agents.rca.onset import prepare_samples, summarize_series


class OnsetTests(unittest.TestCase):
    def test_zero_reference_and_peak_are_distinct(self):
        result = summarize_series([(0, 0), (60, 0), (120, 0), (180, 3), (240, 5), (300, 9)], 180, 360)
        episode = result["episodes"][0]
        self.assertEqual(episode["onset_interval_s"], [120, 180])
        self.assertEqual(episode["peak_timestamp_s"], 300)
        self.assertLessEqual(result["strength"], 20)

    def test_gap_does_not_join_isolated_peaks(self):
        result = summarize_series([(i * 60, 0) for i in range(10)] + [(600, 10), (1200, 10)], 600, 1300)
        self.assertEqual(len(result["episodes"]), 2)
        self.assertTrue(all(e["spike"] for e in result["episodes"]))
        self.assertTrue(result["episodes"][1]["gap_before"])

    def test_two_episodes_are_preserved(self):
        result = summarize_series([(0, 1), (60, 1), (120, 1), (180, 8), (240, 8), (300, 1), (360, 9), (420, 9)], 180, 480)
        self.assertEqual(len(result["episodes"]), 2)
        self.assertEqual(result["episodes"][1]["onset_interval_s"], [300, 360])

    def test_constant_and_missing_reference_are_not_anomalies(self):
        self.assertEqual(summarize_series([(0, 3), (60, 3), (120, 3)], 60, 180)["episodes"], [])
        missing = summarize_series([(120, 3), (180, 9)], 120, 240)
        self.assertIsNone(missing["strength"])
        self.assertEqual(missing["baseline_n"], 0)

    def test_known_counter_reset_and_duplicates(self):
        samples, quality = prepare_samples([(0, 10), (60, 70), (120, 1), (180, 61), (180, 61)], semantics="counter")
        self.assertEqual(samples.tolist(), [[60, 1], [180, 1]])
        self.assertEqual(quality["counter_resets"], 1)
        self.assertEqual(quality["duplicate_samples"], 1)

    def test_unknown_counters_are_not_differenced(self):
        samples, quality = prepare_samples([(0, 10), (60, 70), (120, 1)])
        self.assertEqual(len(samples), 3)
        self.assertEqual(quality["counter_resets"], 0)

    def test_invalid_values_are_finite_json(self):
        result = summarize_series([(0, 0), (60, float("nan")), (120, float("inf")), (180, "bad"), (240, 10)], 200, 300)
        self.assertEqual(result["invalid_samples"], 3)
        json.dumps(result, allow_nan=False)

    def test_reference_sensitivity(self):
        result = summarize_series([(0, 0), (60, 0), (120, 10), (180, 10), (240, 20)], 240, 300)
        self.assertTrue(result["reference_sensitive"])

    def test_reference_envelope_and_legacy_replay_schema(self):
        from agents.rca.onset import DEFAULTS
        samples = [(i * 60, v) for i, v in enumerate([0., 2., 0., 0., 2., 0., 5., 5.])]
        current = summarize_series(samples, 360, 480)
        self.assertEqual(current["baseline_p10"], 0.)
        self.assertEqual(current["baseline_p90"], 2.)
        saved_v1 = {k: v for k, v in DEFAULTS.items() if k != "summary_version"}
        replay = summarize_series(samples, 360, 480, params=saved_v1)
        self.assertNotIn("baseline_p10", replay)
        self.assertEqual({k: v for k, v in current.items() if k not in {"baseline_p10", "baseline_p90"}}, replay)


if __name__ == "__main__":
    unittest.main()
