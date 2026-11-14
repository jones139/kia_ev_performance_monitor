"""Unit tests for one_off/import_manual_charges.py (spreadsheet import).

Run from the repository root with:
    venv/bin/python -m unittest discover -s tests -v
or directly:
    venv/bin/python tests/test_import_manual.py
All tests are offline and filesystem-only (temp dirs + generated xlsx).
"""

from __future__ import annotations

import csv
import shutil
import sys
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

try:
    import support  # noqa: F401  (sets up sys.path)
except ImportError:  # loaded as tests.test_x package member
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import support  # noqa: E402,F401

sys.path.insert(0, str(support.REPO_ROOT / "one_off"))
import import_manual_charges as imp  # noqa: E402

import collect  # noqa: E402  (for SAMPLE_COLUMNS / DAILY_COLUMNS)
import openpyxl  # noqa: E402


def make_workbook(path: Path) -> None:
    """Write a minimal Charging sheet: header rows, 3 events, template row.

    Event rows (1-based columns as in the real sheet): date, mileage
    (miles), SOC start, SOC end, kWh supplied, location(col 9), notes(25).
    """
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Charging"
    ws["A2"] = "TESTPLT"
    for row, values in (
        (8, {1: datetime(2026, 11, 1), 2: 1000.0, 4: 80.0, 5: 20.0, 9: "Home"}),
        (9, {1: datetime(2026, 11, 3), 2: 1050.0, 3: 40.0, 4: 80.0, 5: 30.0,
             9: "Home"}),
        (10, {1: datetime(2026, 11, 6), 2: 1100.0, 3: 30.0, 5: 25.0,
              9: "Away"}),
        (11, {9: "Home"}),  # template row: no date -> end of log
    ):
        for col, value in values.items():
            ws.cell(row=row, column=col, value=value)
    wb.save(path)


class ParseEventsTest(unittest.TestCase):
    """Tests for imp.parse_charge_events sheet parsing."""

    def setUp(self):
        """Create the synthetic workbook in a temp dir."""
        self.tmp = Path(tempfile.mkdtemp(prefix="kia_xlsx_", dir="/tmp/opencode"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.xlsx = self.tmp / "log.xlsx"
        make_workbook(self.xlsx)

    def test_events_and_registration(self):
        """Three event rows parse; template row stops the log; A2 = reg."""
        events, registration = imp.parse_charge_events(self.xlsx)
        self.assertEqual(registration, "TESTPLT")
        self.assertEqual(len(events), 3)
        first, second, third = events
        self.assertEqual(first.date, date(2026, 11, 1))
        self.assertAlmostEqual(first.odometer_km, 1000.0 * 1.609344, places=1)
        self.assertIsNone(first.soc_start)
        self.assertEqual(second.soc_start, 40.0)
        self.assertEqual(third.location, "Away")

    def test_home_vs_away_datetimes(self):
        """Home charges start on the recorded evening, end next morning."""
        events, _ = imp.parse_charge_events(self.xlsx)
        home, away = events[0], events[2]
        self.assertTrue(home.is_home)
        self.assertFalse(away.is_home)
        self.assertEqual(home.start_datetime(), datetime(2026, 11, 1, 23, 30))
        self.assertEqual(home.end_datetime(), datetime(2026, 11, 2, 7, 30))
        self.assertEqual(away.start_datetime(), datetime(2026, 11, 6, 11, 0))

    def test_missing_file_raises(self):
        """A missing xlsx must raise FileNotFoundError."""
        with self.assertRaises(FileNotFoundError):
            imp.parse_charge_events(self.tmp / "nope.xlsx")


class PeriodsTest(unittest.TestCase):
    """Tests for imp.build_driving_periods interval derivation."""

    def periods(self):
        """Parse the synthetic workbook and derive its periods."""
        tmp = Path(tempfile.mkdtemp(prefix="kia_per_", dir="/tmp/opencode"))
        self.addCleanup(shutil.rmtree, tmp, True)
        xlsx = tmp / "log.xlsx"
        make_workbook(xlsx)
        events, _ = imp.parse_charge_events(xlsx)
        return imp.build_driving_periods(events)

    def test_two_periods_with_distances(self):
        """Consecutive charge pairs yield periods with km distances."""
        periods = self.periods()
        self.assertEqual(len(periods), 2)
        first, second = periods
        self.assertEqual(first.soc_from, 80.0)
        self.assertEqual(first.soc_to, 40.0)
        self.assertAlmostEqual(first.distance_km, 50 * 1.609344, places=1)
        self.assertEqual(first.days, [date(2026, 11, 2), date(2026, 11, 3)])
        self.assertEqual(second.days[0], date(2026, 11, 4))

    def test_rising_battery_skips_period(self):
        """A period whose battery rose (unlogged charge) is dropped."""
        events = [
            imp.ChargeEvent(1, date(2026, 11, 1), 100.0, None, 50.0, None, "Home", ""),
            imp.ChargeEvent(2, date(2026, 11, 2), 200.0, 60.0, None, None, "Home", ""),
        ]
        self.assertEqual(imp.build_driving_periods(events), [])


class ApportionTest(unittest.TestCase):
    """Tests for imp.apportion weight/even splitting."""

    def test_weighted_and_even_and_sum(self):
        """Weights used when complete; even otherwise; sums preserved."""
        days = [date(2026, 11, 1), date(2026, 11, 2)]
        weighted = imp.apportion(30.0, days, {days[0]: 10.0, days[1]: 20.0})
        self.assertAlmostEqual(weighted[days[0]], 10.0, places=9)
        self.assertAlmostEqual(weighted[days[1]], 20.0, places=9)
        partial = imp.apportion(30.0, days, {days[0]: 10.0})
        self.assertEqual(partial[days[0]], 15.0)
        self.assertAlmostEqual(sum(imp.apportion(10.0, days, None).values()),
                               10.0, places=9)


class PeriodWeightsTest(unittest.TestCase):
    """Tests for imp.period_weights rescaling rules."""

    def period(self):
        """A two-day period with a known 60 km odometer distance."""
        return imp.DrivingPeriod(
            datetime(2026, 11, 1, 7, 30), datetime(2026, 11, 2, 23, 30),
            80.0, 40.0, 60.0, [date(2026, 11, 1), date(2026, 11, 2)])

    def test_rescales_api_shape_to_odometer_total(self):
        """Full API coverage: weights keep the shape but sum to 60 km."""
        weights = imp.period_weights(self.period(),
                                     {date(2026, 11, 1): 10.0,
                                      date(2026, 11, 2): 30.0})
        self.assertIsNotNone(weights)
        self.assertAlmostEqual(sum(weights.values()), 60.0, places=9)
        self.assertAlmostEqual(weights[date(2026, 11, 2)], 45.0, places=9)

    def test_rest_days_as_zero_stay_covered(self):
        """A rest day with an explicit 0.0 weight still counts as covered."""
        period = self.period()
        weights = imp.period_weights(period, {date(2026, 11, 1): 40.0,
                                              date(2026, 11, 2): 0.0})
        self.assertAlmostEqual(weights[date(2026, 11, 2)], 0.0, places=9)
        self.assertAlmostEqual(weights[date(2026, 11, 1)], 60.0, places=9)

    def test_missing_day_or_distance_gives_none(self):
        """Partial coverage or unknown period distance -> even splits."""
        self.assertIsNone(imp.period_weights(self.period(),
                                             {date(2026, 11, 1): 10.0}))
        no_distance = imp.DrivingPeriod(
            datetime(2026, 11, 1, 7, 30), datetime(2026, 11, 2, 23, 30),
            80.0, 40.0, None, [date(2026, 11, 1), date(2026, 11, 2)])
        self.assertIsNone(imp.period_weights(no_distance,
                                             {date(2026, 11, 1): 10.0,
                                              date(2026, 11, 2): 10.0}))


class SynthesiseTest(unittest.TestCase):
    """Tests for imp.synthesise_samples / imp.synthesise_daily."""

    def build(self):
        """Parse the synthetic workbook into events/periods."""
        tmp = Path(tempfile.mkdtemp(prefix="kia_syn_", dir="/tmp/opencode"))
        self.addCleanup(shutil.rmtree, tmp, True)
        xlsx = tmp / "log.xlsx"
        make_workbook(xlsx)
        events, reg = imp.parse_charge_events(xlsx)
        return events, imp.build_driving_periods(events), reg, tmp

    def test_sample_levels_consistent_across_midnight(self):
        """Each day's derived end level equals the next day's start level."""
        events, periods, reg, _ = self.build()
        rows = imp.synthesise_samples(events, periods, {}, reg)
        by_ts = {r["timestamp_local"]: r for r in rows}
        self.assertEqual(by_ts["2026-11-02T23:59"]["battery_pct"],
                         by_ts["2026-11-03T00:01"]["battery_pct"])
        # anchored rows present: charge end post and charge start pre
        self.assertEqual(by_ts["2026-11-02T07:30"]["battery_pct"], 80.0)
        self.assertEqual(by_ts["2026-11-03T23:30"]["battery_pct"], 40.0)
        # even split (no weights): 40% over 2 days -> 20 per day
        self.assertEqual(float(by_ts["2026-11-02T23:59"]["battery_pct"]), 60.0)

    def test_weights_follow_distances(self):
        """With per-day weights the drop follows the distance shares."""
        events, periods, reg, _ = self.build()
        weights = {date(2026, 11, 2): 30.0, date(2026, 11, 3): 10.0}
        rows = imp.synthesise_samples(events, periods, weights, reg)
        by_ts = {r["timestamp_local"]: r for r in rows}
        self.assertAlmostEqual(float(by_ts["2026-11-02T23:59"]["battery_pct"]),
                               80.0 - 40.0 * 0.75, places=6)

    def test_daily_only_for_uncovered_dates(self):
        """Derived daily rows skip dates the API already covers."""
        events, periods, _, _ = self.build()
        rows, overlay = imp.synthesise_daily(periods, {date(2026, 11, 2)},
                                             {}, "t1")
        dates = {r["date"] for r in rows}
        self.assertNotIn("2026-11-02", dates)
        self.assertIn("2026-11-03", dates)
        self.assertTrue(all(r["kwh_used"] == "" for r in rows))
        # overlay covers every period day incl. API-covered ones
        self.assertAlmostEqual(sum(overlay[d] for d in periods[0].days),
                               periods[0].distance_km, places=1)


class ReferenceCsvTest(unittest.TestCase):
    """Tests for imp.write_reference_csv column alignment."""

    def test_period_rows_match_header_columns(self):
        """Period rows must sit in the same columns as the header."""
        tmp = Path(tempfile.mkdtemp(prefix="kia_ref_", dir="/tmp/opencode"))
        self.addCleanup(shutil.rmtree, tmp, True)
        xlsx = tmp / "log.xlsx"
        make_workbook(xlsx)
        events, _ = imp.parse_charge_events(xlsx)
        periods = imp.build_driving_periods(events)
        path = tmp / "reference.csv"
        imp.write_reference_csv(path, events, periods)
        with open(path, newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        period = next(row for row in rows if row["record"] == "period")
        self.assertEqual(period["days"], "2026-11-02..2026-11-03")
        self.assertAlmostEqual(float(period["distance_km"]),
                               50 * 1.609344, places=1)
        self.assertAlmostEqual(float(period["pct_drop"]), 40.0, places=6)
        self.assertEqual(period["notes"], "")


class MergeTest(unittest.TestCase):
    """Tests for imp.merge_samples / imp.merge_daily idempotency."""

    def setUp(self):
        """Temp dir for the CSV files under test."""
        self.tmp = Path(tempfile.mkdtemp(prefix="kia_mrg_", dir="/tmp/opencode"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_samples_merge_keeps_collector_and_reruns_stably(self):
        """Collector rows survive; a second run changes nothing."""
        path = self.tmp / "samples.csv"
        collect_row = {col: "" for col in collect.SAMPLE_COLUMNS}
        collect_row.update(timestamp_local="2026-11-05T06:30:00+00:00",
                           date_local="2026-11-05", sample_type="post_charge",
                           battery_pct="77", source="collect")
        with open(path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=collect.SAMPLE_COLUMNS)
            writer.writeheader()
            writer.writerow(collect_row)
        manual = [{col: "" for col in collect.SAMPLE_COLUMNS}
                  for _ in range(2)]
        manual[0].update(timestamp_local="2026-11-01T07:30", date_local="2026-11-01",
                         sample_type="post_charge", battery_pct="80", source="manual")
        manual[1].update(timestamp_local="2026-11-02T23:30", date_local="2026-11-02",
                         sample_type="pre_charge", battery_pct="40", source="manual")
        imp.merge_samples(path, manual)
        first = path.read_text()
        imp.merge_samples(path, manual)  # re-run must be a no-op
        self.assertEqual(path.read_text(), first)
        with open(path, newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        sources = sorted(r["source"] for r in rows)
        self.assertEqual(sources, ["collect", "manual", "manual"])

    def test_daily_merge_regenerates_derived_keeps_api(self):
        """API rows persist; derived rows are replaced on re-run."""
        path = self.tmp / "daily.csv"
        imp.merge_daily(path, [{"date": "2026-11-01", "distance_km": 10.0,
                                "kwh_used": "", "source": "manual_derived"}])
        imp.merge_daily(path, [{"date": "2026-11-01", "distance_km": 12.0,
                                "kwh_used": "", "source": "manual_derived"},
                               {"date": "2026-11-02", "distance_km": 5.0,
                                "kwh_used": "", "source": "manual_derived"}],
                        {date(2026, 11, 1): 12.0, date(2026, 11, 2): 5.0})
        with open(path, newline="", encoding="utf-8") as handle:
            rows = {r["date"]: r for r in csv.DictReader(handle)}
        self.assertEqual(rows["2026-11-01"]["distance_km"], "12.0")  # replaced
        self.assertEqual(rows["2026-11-01"]["distance_range_km"], "12.0")
        self.assertEqual(sorted(rows), ["2026-11-01", "2026-11-02"])


if __name__ == "__main__":
    unittest.main()
