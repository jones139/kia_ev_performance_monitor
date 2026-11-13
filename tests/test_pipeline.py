"""End-to-end test: synthetic dataset -> analyze.py -> summary + graph.

Builds a deterministic fake driving history whose true full-charge range
(356 km), efficiency (5.0 km/kWh) and usable capacity (71.2 kWh) are known,
runs analyze.py over it as a subprocess, and asserts that:

  * both estimation methods recover the true range (and the planning line
    is 80% of the rolling average)
  * the implied-capacity diagnostic recovers the true capacity
  * days that violate the sanity rules (daytime charging, tiny %-drop,
    missing sample pair) are reported as excluded
  * the two-panel PNG graph is produced
  * --capacity-kwh overrides method 2 as expected
  * running with no data exits non-zero

Run from the repository root with:
    venv/bin/python -m unittest discover -s tests -v
or directly:
    venv/bin/python tests/test_pipeline.py
"""

from __future__ import annotations

import re
import shutil
import tempfile
import unittest
from datetime import date
from pathlib import Path

try:
    import support  # noqa: F401  (sets up sys.path)
except ImportError:  # loaded as tests.test_x package member
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import support  # noqa: E402,F401

from support import (build_synthetic_dataset, parse_planning_values,
                     parse_rolling_values, run_analyze, write_daily_csv,
                     write_samples_csv)

# Ground truth of the synthetic dataset.
TRUE_RANGE_KM = 356.0
TRUE_CAPACITY_KWH = 71.2
EFFICIENCY = 5.0
PLAN_FACTOR = 0.8

# 12 days: rest days, normal days, one tiny-%-drop day (2 km), one daytime
# charge (index 8) and one day with driving but no battery samples (index 6).
DISTANCES = [0.0, 40.0, 60.0, 2.0, 80.0, 50.0, 45.0, 70.0, 30.0, 0.0, 55.0, 48.0]
CHARGED_DAYS = {8}
NO_SAMPLE_DAYS = {6}


class SyntheticPipelineTest(unittest.TestCase):
    """Full offline run of analyze.py over generated data."""

    def setUp(self):
        """Create a temp data dir holding the synthetic CSV files."""
        self.tmp = Path(tempfile.mkdtemp(prefix="kia_pipe_", dir="/tmp/opencode"))
        samples, daily = build_synthetic_dataset(
            start_day=date(2026, 11, 1),
            distances_km=DISTANCES,
            efficiency_km_per_kwh=EFFICIENCY,
            full_range_km=TRUE_RANGE_KM,
            capacity_kwh=TRUE_CAPACITY_KWH,
            charged_days=CHARGED_DAYS,
            no_sample_days=NO_SAMPLE_DAYS,
        )
        write_samples_csv(self.tmp / "samples.csv", samples)
        write_daily_csv(self.tmp / "daily.csv", daily)
        self.output = self.tmp / "graph.png"

    def tearDown(self):
        """Remove the temp data dir."""
        shutil.rmtree(self.tmp, ignore_errors=True)

    def analyze(self, *extra: str):
        """Run analyze.py against the temp dir and return the process."""
        return run_analyze(self.tmp, self.output, list(extra))

    def test_both_methods_recover_true_range(self):
        """Rolling averages of both methods match the synthetic truth."""
        result = self.analyze()
        self.assertEqual(result.returncode, 0, result.stderr)
        rolling = parse_rolling_values(result.stdout)
        self.assertEqual(len(rolling), 2, result.stdout)
        for value in rolling:
            self.assertAlmostEqual(value, TRUE_RANGE_KM, delta=20.0)
        planning = parse_planning_values(result.stdout)
        self.assertEqual(len(planning), 2)
        for roll, plan in zip(rolling, planning):
            self.assertAlmostEqual(plan, roll * PLAN_FACTOR, delta=1.0)

    def test_summary_reports_exclusions_and_implied_capacity(self):
        """Sanity-rule rejections and the capacity diagnostic are printed."""
        result = self.analyze()
        self.assertEqual(result.returncode, 0, result.stderr)
        for reason in ("charged during day", "pct delta too small",
                       "missing sample pair"):
            self.assertIn(reason, result.stdout)
        match = re.search(
            r"Implied usable battery capacity: median ([\d.]+) kWh", result.stdout
        )
        self.assertIsNotNone(match, result.stdout)
        self.assertAlmostEqual(float(match.group(1)), TRUE_CAPACITY_KWH, delta=4.0)

    def test_graph_file_created(self):
        """The two-panel PNG must exist and be a real image."""
        result = self.analyze()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.output.exists())
        self.assertGreater(self.output.stat().st_size, 10_000)
        with open(self.output, "rb") as handle:
            self.assertEqual(handle.read(8)[1:4], b"PNG")

    def test_capacity_override_changes_method2_only(self):
        """--capacity-kwh rescales method 2 but leaves method 1 alone."""
        result = self.analyze("--capacity-kwh", "50")
        self.assertEqual(result.returncode, 0, result.stderr)
        rolling = parse_rolling_values(result.stdout)
        self.assertEqual(len(rolling), 2)
        self.assertAlmostEqual(rolling[0], TRUE_RANGE_KM, delta=20.0)
        # method 2 = capacity x efficiency = 50 x 5.0 = 250 km
        self.assertAlmostEqual(rolling[1], 50.0 * EFFICIENCY, delta=10.0)

    def test_no_data_exits_nonzero(self):
        """An empty data dir must fail with a clear error, not a crash."""
        empty = self.tmp / "empty"
        empty.mkdir()
        result = run_analyze(empty, self.tmp / "unused.png")
        self.assertEqual(result.returncode, 1)
        self.assertIn("No data found", result.stderr)


if __name__ == "__main__":
    unittest.main()
