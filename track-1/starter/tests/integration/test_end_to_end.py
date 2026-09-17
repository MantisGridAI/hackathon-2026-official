"""Default runner, all five modules, and provenance smoke on synthetic telemetry."""
import csv
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from tests.fixtures.synthetic import make_dataset

STARTER = Path(__file__).resolve().parents[2]


class IntegrationTests(unittest.TestCase):
    def test_default_entrypoint_noncontinuous_ids_no_key_or_requests(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dataset, out = make_dataset(root / "dataset"), root / "out"
            env = dict(os.environ, RCA_MODE="deterministic", PYTHONDONTWRITEBYTECODE="1")
            env.pop("FEATHERLESS_API_KEY", None)
            before = {str(p.relative_to(dataset)): p.stat().st_mtime_ns for p in dataset.rglob("*") if p.is_file()}
            result = subprocess.run([sys.executable, str(STARTER/"run.py"), "--dataset", str(dataset),
                "--queries", str(dataset/"query.csv"), "--out", str(out)], env=env,
                cwd=STARTER, capture_output=True, text=True, timeout=45)
            self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
            with (out/"predictions.csv").open(encoding="utf-8", newline="") as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual([r["row_id"] for r in rows], ["7", "23"])
            for row, count in zip(rows, (1, 2)):
                prediction = json.loads(row["prediction"].removeprefix("```json\n").removesuffix("\n```"))
                self.assertEqual(len(prediction), count)
                text = (out/"evidence"/(row["row_id"]+".md")).read_text(encoding="utf-8")
                for title in ("Answer", "Confidence", "Evidence", "Ruled out"):
                    self.assertIn("## "+title, text)
                self.assertNotIn("AGENT RAISED", text)
            usage = [json.loads(line) for line in (out/"usage.jsonl").read_text().splitlines()]
            self.assertTrue(all(row["calls"] == 0 for row in usage))
            routes = [json.loads(line) for line in (out/"diagnostics/routes.jsonl").read_text().splitlines()]
            self.assertTrue(routes, "New routed default must produce its diagnostics")
            self.assertTrue(all(row.get("event_type", row.get("event")) != "request" for row in routes))
            after = {str(p.relative_to(dataset)): p.stat().st_mtime_ns for p in dataset.rglob("*") if p.is_file()}
            self.assertEqual(before, after, "Runtime must leave dataset read-only")
            # Resume must neither renumber IDs nor bill/rewrite already completed cases.
            result = subprocess.run([sys.executable, str(STARTER/"run.py"), "--dataset", str(dataset),
                "--queries", str(dataset/"query.csv"), "--out", str(out), "--resume"], env=env,
                cwd=STARTER, capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(len((out/"usage.jsonl").read_text().splitlines()), 2)

    def test_unique_root_packaging_and_official_evaluator_unchanged(self):
        root = STARTER.parents[1]
        self.assertTrue((root/"Dockerfile").exists())
        self.assertFalse((STARTER/"Dockerfile").exists())
        self.assertIn("COPY track-1/starter/ /app/", (root/"Dockerfile").read_text())
        original = subprocess.run(["git", "diff", "0ddd965", "--", "track-1/starter/score.py"],
                                  cwd=root, capture_output=True, text=True)
        self.assertEqual(original.returncode, 0)
        self.assertEqual(original.stdout, "")


if __name__ == "__main__":
    unittest.main()
