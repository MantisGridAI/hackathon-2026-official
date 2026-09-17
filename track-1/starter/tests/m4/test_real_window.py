"""Optional telemetry-only check. Public development labels are never opened."""
import csv
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest

from agents.rca.contracts import RunConfig
from agents.rca.controller import investigate
from agents.rca.runtime import get_run_state, parse_case


@unittest.skipUnless(os.environ.get("RCA_TEST_DATA"), "RCA_TEST_DATA is not set; real telemetry check not run")
class RealWindowTests(unittest.TestCase):
    def test_second_label_free_case_with_actual_modules(self):
        dataset = Path(os.environ["RCA_TEST_DATA"])
        with (dataset / "query.csv").open(encoding="utf-8-sig", newline="") as stream:
            rows = csv.DictReader(stream)
            next(rows)
            instruction = next(rows)["instruction"]
        case = parse_case(instruction)
        with TemporaryDirectory() as output:
            state = get_run_state(dataset, {"out_dir": output}, RunConfig(mode="deterministic"))
            started = time.monotonic()
            result = investigate(case, state, deadline=started + 45)
            wall = time.monotonic() - started
            self.assertEqual(len(result.decision.answers), case.failure_count)
            self.assertTrue(result.evidence, "Expected some real telemetry evidence")
            self.assertEqual(state.usage_ledger, {})
            self.assertTrue(all(e.source_files for e in result.evidence))
            self.assertLess(wall, 50, "Modules must honor scan and CPU deadlines")
            print(f"real second case: {wall:.3f}s; {len(result.evidence)} evidence records; "
                  f"{len(result.decision.answers)} answers; zero model calls")


if __name__ == "__main__":
    unittest.main()
