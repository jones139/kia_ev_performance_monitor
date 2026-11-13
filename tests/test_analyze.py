"""Unit tests for analyze.py (loaders, pairing, estimates, rolling, graph).

Run from the repository root with:
    venv/bin/python -m unittest discover -s tests -v
or directly:
    venv/bin/python tests/test_analyze.py
All tests are offline and filesystem-only (temp dirs); the graph test uses
matplotlib's headless Agg backend.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

try:
    import support  # noqa: F401  (sets up sys.path)
except ImportError:  # loaded as tests.test_x package member
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import support  # noqa: E402,F401

import analyze
import kia_common
from support import write_daily_csv, write_samples_csv

CONFIG = kia_common.load_config()  # real config.toml; only defaults matter here

D1 = date(2026, 11, 1)


def record(offset: int, **fields) -> analyze.DayRecord:
    """A DayRecord for D1+offset with the given field overrides."""
    return analyze.DayRecord(date=D1 + timedelta(days=offset), **fields)


class ToFloatTest(unittest.TestCase):
    """Tests for analyze._to_float CSV cell parsing."""

    def test_values(self):
        """Empty/invalid cells -> None; numeric strings -> float."""
        self.assertIsNone(analyze._to_float(None))
        self.assertIsNone(analyze._to_float(""))
        self.assertIsNone(analyze._to_float("  "))
        self.assertIsNone(analyze._to_float("abc"))
        self.assertEqual(analyze._to_float("3.5"), 3.5)
        self.assertEqual(analyze._to_float(" 42 "), 42.0)


class LoadersTest(unittest.TestCase):
    """Tests for analyze.load_samples / analyze.load_daily."""

    def test_load_samples_sorts_and_skips_malformed(self):
        """Rows sort by timestamp; an unparseable row is skipped+logged."""
        rows = [
            {"timestamp_local": "2026-11-02T06:30:00+00:00", "date_local": "2026-11-02",
             "sample_type": "post_charge", "battery_pct": "80",
             "capacity_kwh_used": "71.2", "vehicle_name": "PV5"},
            {"timestamp_local": "not-a-time", "date_local": "2026-11-02",
             "sample_type": "pre_charge", "battery_pct": "70"},
            {"timestamp_local": "2026-11-01T23:45:00+00:00", "date_local": "2026-11-01",
             "sample_type": "pre_charge", "battery_pct": "", "vehicle_name": "PV5"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "samples.csv"
            write_samples_csv(path, rows)
            with self.assertLogs(analyze.logger, level="WARNING"):
                samples = analyze.load_samples(path)
        self.assertEqual(len(samples), 2)
        self.assertEqual(samples[0]["date"], date(2026, 11, 1))
        self.assertIsNone(samples[0]["battery_pct"])
        self.assertEqual(samples[1]["battery_pct"], 80.0)
        self.assertEqual(samples[1]["capacity_kwh"], 71.2)

    def test_load_samples_missing_file(self):
        """A missing samples.csv yields an empty list, not an error."""
        self.assertEqual(analyze.load_samples(Path("/nonexistent/s.csv")), [])

    def test_load_daily_parses_and_skips_malformed(self):
        """Valid rows parse to floats; a bad date is skipped+logged."""
        rows = [
            {"date": "2026-11-01", "distance_km": "40.0", "kwh_used": "8.0"},
            {"date": "garbage", "distance_km": "1", "kwh_used": "1"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "daily.csv"
            write_daily_csv(path, rows)
            with self.assertLogs(analyze.logger, level="WARNING"):
                daily = analyze.load_daily(path)
        self.assertEqual(list(daily), [date(2026, 11, 1)])
        self.assertEqual(daily[date(2026, 11, 1)],
                         {"distance_km": 40.0, "kwh_used": 8.0,
                          "source": "api", "distance_range_km": None})


class BuildDayRecordsTest(unittest.TestCase):
    """Tests for analyze.build_day_records pairing and capacity fill."""

    def test_pairing_and_capacity_forward_fill(self):
        """First morning/last evening pair per day; capacity forward-filled."""
        samples_rows = [
            {"timestamp_local": "2026-11-02T06:30:00+00:00", "date_local": "2026-11-02",
             "sample_type": "post_charge", "battery_pct": "90",
             "capacity_kwh_used": "71.2", "vehicle_name": "PV5"},
            {"timestamp_local": "2026-11-02T23:45:00+00:00", "date_local": "2026-11-02",
             "sample_type": "pre_charge", "battery_pct": "80",
             "capacity_kwh_used": "71.2", "vehicle_name": "PV5"},
            {"timestamp_local": "2026-11-03T06:30:00+00:00", "date_local": "2026-11-03",
             "sample_type": "post_charge", "battery_pct": "80",
             "capacity_kwh_used": "71.2", "vehicle_name": "PV5"},
        ]
        daily_rows = [
            {"date": "2026-11-01", "distance_km": "10.0", "kwh_used": "2.0"},
            {"date": "2026-11-02", "distance_km": "40.0", "kwh_used": "8.0"},
            {"date": "2026-11-03", "distance_km": "20.0", "kwh_used": "4.0"},
            {"date": "2026-11-04", "distance_km": "30.0", "kwh_used": "6.0"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            samples_path = Path(tmp) / "samples.csv"
            daily_path = Path(tmp) / "daily.csv"
            write_samples_csv(samples_path, samples_rows)
            write_daily_csv(daily_path, daily_rows)
            samples = analyze.load_samples(samples_path)
            daily = analyze.load_daily(daily_path)
        records = {r.date: r for r in
                   analyze.build_day_records(samples, daily, CONFIG, 60.0)}
        self.assertEqual(sorted(records), [
            date(2026, 11, d) for d in (1, 2, 3, 4)])
        self.assertEqual(records[date(2026, 11, 1)].capacity_kwh, 60.0)  # default
        self.assertEqual(records[date(2026, 11, 2)].pct_start, 90.0)
        self.assertEqual(records[date(2026, 11, 2)].pct_end, 80.0)
        self.assertTrue(records[date(2026, 11, 2)].pair_measured)
        self.assertEqual(records[date(2026, 11, 3)].pct_start, 80.0)
        self.assertIsNone(records[date(2026, 11, 3)].pct_end)
        self.assertFalse(records[date(2026, 11, 3)].pair_measured)
        self.assertEqual(records[date(2026, 11, 4)].capacity_kwh, 71.2)  # filled


class EstimateSeriesTest(unittest.TestCase):
    """Tests for analyze.estimate_series thresholds and skip reasons."""

    def setUp(self):
        """Build one DayRecord exercising every accept/skip rule."""
        cap = 71.2
        self.records = [
            record(0, pct_start=90.0, pct_end=78.76, distance_km=40.0,
                   kwh_used=8.0, capacity_kwh=cap, pair_measured=True),
            # both methods + implied
            record(1, pct_start=90.0, pct_end=89.5, distance_km=2.0,
                   kwh_used=0.4, capacity_kwh=cap),        # % drop too small
            record(2, pct_start=80.0, pct_end=85.0, distance_km=30.0,
                   kwh_used=6.0, capacity_kwh=cap),        # charged during day
            record(3, pct_start=90.0, pct_end=80.0, distance_km=0.5,
                   kwh_used=0.1, capacity_kwh=cap),        # no driving: ignored
            record(4, distance_km=50.0, kwh_used=10.0,
                   capacity_kwh=cap),                      # no sample pair
            record(5, pct_start=90.0, pct_end=84.0, distance_km=20.0,
                   kwh_used=0.1, capacity_kwh=cap),        # kWh too small
        ]
        self.series, self.skipped = analyze.estimate_series(self.records, CONFIG)

    def test_method1_values(self):
        """Accepted % days produce distance/pct_drop*100 estimates."""
        values = dict(self.series["range_pct"])
        self.assertEqual(sorted(values), [D1, D1 + timedelta(days=5)])
        self.assertAlmostEqual(values[D1], 40.0 / 11.24 * 100, places=1)
        self.assertAlmostEqual(values[D1 + timedelta(days=5)], 20.0 / 6.0 * 100,
                               places=1)

    def test_method2_values(self):
        """Accepted kWh days produce capacity*distance/kwh estimates."""
        values = dict(self.series["range_kwh"])
        self.assertEqual(sorted(values),
                         [D1 + timedelta(days=i) for i in (0, 1, 2, 4)])
        self.assertAlmostEqual(values[D1], 71.2 * 40.0 / 8.0, places=3)

    def test_implied_capacity_only_for_big_drops(self):
        """Implied capacity needs pct drop >= capacity_check_min_pct_delta."""
        self.assertEqual([d for d, _ in self.series["implied_capacity"]], [D1])
        self.assertAlmostEqual(dict(self.series["implied_capacity"])[D1],
                               8.0 / 0.1124, places=0)

    def test_skip_counters(self):
        """Every rejection rule must be counted exactly once here."""
        self.assertEqual(self.skipped, {
            "charged_during_day": 1,
            "pct_delta_too_small": 1,
            "usage_too_small": 1,
            "missing_sample_pair": 1,
            "implausible_estimate": 0,
        })

    def test_raw_series_record_every_day(self):
        """distance/kwh raw series keep all days with API stats."""
        self.assertEqual(len(self.series["distance"]), 6)
        self.assertEqual(len(self.series["kwh"]), 6)


class RollingAverageTest(unittest.TestCase):
    """Tests for analyze.rolling_average windowing rules."""

    def test_window_means_and_gap_filling(self):
        """Every calendar date with data in its window gets a mean."""
        series = [(D1, 10.0), (D1 + timedelta(days=2), 20.0)]
        result = analyze.rolling_average(series, 3)
        self.assertEqual(result, [
            (D1, 10.0),
            (D1 + timedelta(days=1), 10.0),
            (D1 + timedelta(days=2), 15.0),
        ])

    def test_window_of_one_day(self):
        """A 1-day window only emits dates that have their own value."""
        series = [(D1, 10.0), (D1 + timedelta(days=2), 20.0)]
        self.assertEqual(analyze.rolling_average(series, 1), series)

    def test_empty_series(self):
        """No input values -> no output values."""
        self.assertEqual(analyze.rolling_average([], 7), [])


class ScaleSeriesTest(unittest.TestCase):
    """Tests for analyze.scale_series planning-line scaling."""

    def test_factor_applied_to_values_only(self):
        """Dates are preserved; values multiplied by the factor."""
        scaled = analyze.scale_series([(D1, 100.0), (D1, 50.0)], 0.8)
        self.assertEqual(scaled, [(D1, 80.0), (D1, 40.0)])


class MakeGraphTest(unittest.TestCase):
    """Tests for analyze.make_graph rendering (headless Agg)."""

    def panels(self, empty_first: bool = False):
        """Two graph panels; optionally the first has no data at all."""
        spot = [] if empty_first else [(D1, 300.0), (D1 + timedelta(days=1), 320.0)]
        rolling = [] if empty_first else [(D1, 300.0), (D1 + timedelta(days=1), 310.0)]
        planning = [] if empty_first else [(D1, 240.0), (D1 + timedelta(days=1), 248.0)]
        return [
            ("Method 1", spot, rolling, planning),
            ("Method 2", [(D1, 356.0)], [(D1, 356.0)], [(D1, 284.8)]),
        ]

    def render(self, empty_first: bool) -> Path:
        """Render the test panels into a temp PNG and return its path."""
        tmp = tempfile.mkdtemp(prefix="kia_graph_", dir="/tmp/opencode")
        self.addCleanup(shutil.rmtree, tmp, True)
        path = Path(tmp) / "graph.png"
        analyze.make_graph(path, self.panels(empty_first), window_days=7,
                           plan_max_pct=100, plan_min_pct=20,
                           vehicle_name="PV5-TEST")
        return path

    def test_graph_written(self):
        """A populated two-panel graph must produce a non-trivial PNG."""
        path = self.render(empty_first=False)
        self.assertTrue(path.exists())
        self.assertGreater(path.stat().st_size, 10_000)

    def test_graph_with_empty_panel(self):
        """An empty first panel (no estimates yet) still renders fine."""
        path = self.render(empty_first=True)
        self.assertTrue(path.exists())
        self.assertGreater(path.stat().st_size, 10_000)


if __name__ == "__main__":
    unittest.main()
