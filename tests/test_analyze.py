"""Unit tests for analyze.py (loaders, pairing, estimates, rolling, graph).

Run from the repository root with:
    venv/bin/python -m unittest discover -s tests -v
or directly:
    venv/bin/python tests/test_analyze.py
All tests are offline and filesystem-only (temp dirs); the graph test uses
matplotlib's headless Agg backend.
"""

from __future__ import annotations

import copy
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

    def test_midday_charge_detected_but_duplicate_anchor_ignored(self):
        """A second charge ending hours later flags the day; a duplicate
        record of the same overnight charge (minutes apart) must not."""
        samples_rows = [
            # 2 Nov: collector + spreadsheet anchor for the same 06:30 end.
            {"timestamp_local": "2026-11-02T06:30:00+00:00", "date_local": "2026-11-02",
             "sample_type": "post_charge", "battery_pct": "90"},
            {"timestamp_local": "2026-11-02T06:45:00+00:00", "date_local": "2026-11-02",
             "sample_type": "post_charge", "battery_pct": "90"},
            {"timestamp_local": "2026-11-02T23:45:00+00:00", "date_local": "2026-11-02",
             "sample_type": "pre_charge", "battery_pct": "80"},
            # 3 Nov: a real mid-day top-up ends at 12:30.
            {"timestamp_local": "2026-11-03T06:30:00+00:00", "date_local": "2026-11-03",
             "sample_type": "post_charge", "battery_pct": "80"},
            {"timestamp_local": "2026-11-03T11:00:00+00:00", "date_local": "2026-11-03",
             "sample_type": "pre_charge", "battery_pct": "40"},
            {"timestamp_local": "2026-11-03T12:30:00+00:00", "date_local": "2026-11-03",
             "sample_type": "post_charge", "battery_pct": "85"},
            {"timestamp_local": "2026-11-03T23:45:00+00:00", "date_local": "2026-11-03",
             "sample_type": "pre_charge", "battery_pct": "70"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "samples.csv"
            write_samples_csv(path, samples_rows)
            samples = analyze.load_samples(path)
        records = {r.date: r for r in
                   analyze.build_day_records(samples, {}, CONFIG, 71.2)}
        self.assertFalse(records[date(2026, 11, 2)].charged_during_day)
        self.assertTrue(records[date(2026, 11, 3)].charged_during_day)


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

    def test_flagged_midday_charge_drops_method1_only(self):
        """A flagged top-up invalidates the % estimate, not the kWh one."""
        records = [record(0, pct_start=90.0, pct_end=80.0, distance_km=50.0,
                          kwh_used=10.0, capacity_kwh=71.2,
                          charged_during_day=True)]
        series, skipped = analyze.estimate_series(records, CONFIG)
        self.assertEqual(series["range_pct"], [])
        self.assertEqual(series["range_kwh"], [(D1, 71.2 * 50.0 / 10.0)])
        self.assertEqual(skipped["charged_during_day"], 1)

    def test_kwh_correction_scales_method2_only(self):
        """kwh_correction divides method 2's energy, leaving method 1 alone."""
        records = [record(0, pct_start=90.0, pct_end=80.0, distance_km=50.0,
                          kwh_used=10.0, capacity_kwh=71.2)]
        plain, _ = analyze.estimate_series(records, CONFIG)
        corrected, _ = analyze.estimate_series(records, CONFIG, 1.25)
        self.assertEqual(plain["range_pct"], corrected["range_pct"])
        self.assertAlmostEqual(
            dict(corrected["range_kwh"])[D1],
            dict(plain["range_kwh"])[D1] / 1.25, places=6,
        )


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


class EnergyAccountingTest(unittest.TestCase):
    """Tests for analyze.energy_accounting SOC-vs-API energy comparison."""

    def test_sums_only_days_with_both_quantities(self):
        """Battery energy uses the % drop x capacity; API energy is summed."""
        records = [
            record(0, pct_start=90.0, pct_end=70.0, kwh_used=10.0,
                   distance_km=100.0, capacity_kwh=71.2),
            record(1, pct_start=80.0, pct_end=60.0, kwh_used=12.0,
                   distance_km=200.0, capacity_kwh=71.2),
            record(2, pct_start=60.0, pct_end=60.0, kwh_used=1.0,
                   distance_km=5.0, capacity_kwh=71.2),  # zero drop: excluded
            record(3, pct_start=70.0, pct_end=None, kwh_used=5.0,
                   distance_km=5.0, capacity_kwh=71.2),  # missing %: excluded
            record(4, pct_start=50.0, pct_end=40.0, kwh_used=None,
                   distance_km=5.0, capacity_kwh=71.2),  # missing kWh: excluded
        ]
        result = analyze.energy_accounting(records)
        self.assertEqual(result["days"], 2)
        self.assertAlmostEqual(result["soc_kwh"], 0.40 * 71.2, places=6)
        self.assertAlmostEqual(result["api_kwh"], 22.0, places=6)
        self.assertAlmostEqual(result["ratio"], 22.0 / 28.48, places=6)
        self.assertAlmostEqual(result["gap_kwh"], 28.48 - 22.0, places=6)
        self.assertAlmostEqual(result["gap_per_day"], (28.48 - 22.0) / 2,
                               places=6)
        self.assertAlmostEqual(result["distance_km"], 300.0, places=6)
        self.assertAlmostEqual(result["gap_per_100km"],
                               (28.48 - 22.0) / 300.0 * 100.0, places=6)

    def test_missing_data_returns_none(self):
        """No qualifying days -> ratio/gap figures are None."""
        result = analyze.energy_accounting([])
        self.assertEqual(result["days"], 0)
        self.assertIsNone(result["ratio"])
        self.assertIsNone(result["gap_per_day"])
        self.assertIsNone(result["gap_per_100km"])

    def test_midday_charge_days_are_excluded(self):
        """A top-up day has an understated drop and must not bias the ratio."""
        records = [
            record(0, pct_start=90.0, pct_end=70.0, kwh_used=10.0,
                   distance_km=50.0, capacity_kwh=71.2),
            record(1, pct_start=90.0, pct_end=85.0, kwh_used=10.0,
                   distance_km=50.0, capacity_kwh=71.2,
                   charged_during_day=True),   # would halve the apparent ratio
        ]
        result = analyze.energy_accounting(records)
        self.assertEqual(result["days"], 1)
        self.assertAlmostEqual(result["soc_kwh"], 0.20 * 71.2, places=6)
        self.assertAlmostEqual(result["api_kwh"], 10.0, places=6)


class ResolveKwhCorrectionTest(unittest.TestCase):
    """Tests for analyze.resolve_kwh_correction."""

    @staticmethod
    def _records(count: int, ratio: float) -> list[analyze.DayRecord]:
        """Records with a 10% drop (7.12 kWh) and API kWh set to ``ratio``."""
        return [
            record(index, pct_start=100.0, pct_end=90.0,
                   kwh_used=7.12 * ratio, distance_km=50.0,
                   capacity_kwh=71.2)
            for index in range(count)
        ]

    def test_auto_uses_inverse_of_energy_ratio(self):
        """With enough days, auto returns 1 / (api/soc) and explains it."""
        factor, note = analyze.resolve_kwh_correction(
            CONFIG, self._records(12, 0.80)
        )
        self.assertAlmostEqual(factor, 1.25, places=6)
        self.assertIn("80%", note)
        self.assertIn("12 day", note)

    def test_auto_needs_enough_days(self):
        """Too few qualifying days -> no correction."""
        factor, note = analyze.resolve_kwh_correction(
            CONFIG, self._records(3, 0.80)
        )
        self.assertEqual(factor, 1.0)
        self.assertIn("using 1.00", note)

    def test_auto_ignores_ratio_above_one(self):
        """If the API matches or exceeds SOC energy, no correction is made."""
        factor, _ = analyze.resolve_kwh_correction(
            CONFIG, self._records(12, 1.02)
        )
        self.assertEqual(factor, 1.0)

    def test_explicit_number_wins_and_clamps(self):
        """A configured factor is used directly, clamped to [1.0, 2.0]."""
        config = copy.deepcopy(CONFIG)
        config["analysis"]["api_energy_correction"] = 1.3
        self.assertAlmostEqual(
            analyze.resolve_kwh_correction(config, self._records(1, 1.0))[0],
            1.3, places=6,
        )
        config["analysis"]["api_energy_correction"] = 0.7
        self.assertEqual(
            analyze.resolve_kwh_correction(config, [])[0], 1.0
        )
        config["analysis"]["api_energy_correction"] = 9.0
        self.assertEqual(
            analyze.resolve_kwh_correction(config, [])[0], 2.0
        )

    def test_invalid_setting_falls_back(self):
        """A non-numeric string logs and falls back to 1.0."""
        config = copy.deepcopy(CONFIG)
        config["analysis"]["api_energy_correction"] = "banana"
        factor, note = analyze.resolve_kwh_correction(config, [])
        self.assertEqual(factor, 1.0)
        self.assertIn("invalid", note)


class OutputUnitsTest(unittest.TestCase):
    """Tests for analyze.output_units reporting-unit resolution."""

    def config_with_units(self, value):
        """Defaults merged with one [analysis].output_units value."""
        return kia_common._deep_merge(kia_common.DEFAULTS,
                                      {"analysis": {"output_units": value}})

    def test_default_is_km(self):
        """Built-in defaults report in km with factor 1."""
        self.assertEqual(analyze.output_units(kia_common._deep_merge(
            kia_common.DEFAULTS, {})), ("km", 1.0))

    def test_miles_config_and_spellings(self):
        """"miles"/"mi" both select the 1/1.609344 conversion."""
        for spelling in ("miles", "mi", "MILES"):
            label, factor = analyze.output_units(
                self.config_with_units(spelling))
            self.assertEqual(label, "mi")
            self.assertAlmostEqual(factor, 1 / 1.609344, places=9)

    def test_cli_override_wins(self):
        """An override string beats the configured value."""
        self.assertEqual(
            analyze.output_units(self.config_with_units("km"), "miles")[0],
            "mi")

    def test_unknown_value_warns_and_falls_back(self):
        """A nonsense unit logs a warning and reports km."""
        with self.assertLogs(analyze.logger, level="WARNING"):
            label, factor = analyze.output_units(
                self.config_with_units("furlongs"))
        self.assertEqual((label, factor), ("km", 1.0))


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
