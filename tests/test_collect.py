"""Unit tests for collect.py (sample typing, CSV writers, daily upsert).

Run from the repository root with:
    venv/bin/python -m unittest discover -s tests -v
or directly:
    venv/bin/python tests/test_collect.py
All tests are offline and filesystem-only (temp dirs).
"""

from __future__ import annotations

import csv
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

try:
    import support  # noqa: F401  (sets up sys.path)
except ImportError:  # loaded as tests.test_x package member
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import support  # noqa: E402,F401

import collect
import kia_common
from support import FakeDayStat, fake_vehicle, write_daily_csv

CONFIG = kia_common.load_config()  # real config.toml; only defaults matter here


def read_rows(path: Path) -> list[dict]:
    """Read a CSV file back into a list of dicts."""
    with open(path, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


class SampleTypeTest(unittest.TestCase):
    """Tests for collect.determine_sample_type labelling rules."""

    def at(self, hour: int, minute: int = 0) -> datetime:
        """A timezone-aware datetime at the given local hour."""
        return datetime(2026, 11, 13, hour, minute,
                        tzinfo=kia_common.get_timezone(CONFIG))

    def test_override_wins(self):
        """An explicit --sample-type must override any time-based rule."""
        self.assertEqual(
            collect.determine_sample_type(self.at(3), CONFIG, "pre_charge"),
            "pre_charge",
        )
        self.assertEqual(
            collect.determine_sample_type(self.at(23), CONFIG, "post_charge"),
            "post_charge",
        )

    def test_auto_rules(self):
        """Before the cutoff -> post_charge; at/after it -> pre_charge."""
        cutoff = CONFIG["schedule"]["morning_cutoff_hour"]
        self.assertEqual(collect.determine_sample_type(self.at(cutoff - 1), CONFIG),
                         "post_charge")
        self.assertEqual(collect.determine_sample_type(self.at(cutoff), CONFIG),
                         "pre_charge")
        self.assertEqual(collect.determine_sample_type(self.at(23, 45), CONFIG),
                         "pre_charge")


class CollectSampleTest(unittest.TestCase):
    """Tests for collect.collect_sample row construction."""

    def test_row_has_all_columns_and_values(self):
        """The row dict must cover every SAMPLE_COLUMNS key with values."""
        now = datetime(2026, 11, 13, 23, 45, tzinfo=kia_common.get_timezone(CONFIG))
        row = collect.collect_sample(
            fake_vehicle(), "vid1", now, "pre_charge", 71.2, "api"
        )
        self.assertEqual(set(row), set(collect.SAMPLE_COLUMNS))
        self.assertEqual(row["sample_type"], "pre_charge")
        self.assertEqual(row["battery_pct"], 57)
        self.assertEqual(row["date_local"], "2026-11-13")
        self.assertEqual(row["capacity_kwh_used"], 71.2)

    def test_missing_values_become_empty_strings(self):
        """None battery/odometer/capacity must serialise as ""."""
        now = datetime(2026, 11, 13, 6, 30, tzinfo=kia_common.get_timezone(CONFIG))
        vehicle = fake_vehicle(ev_battery_percentage=None, odometer=None,
                               ev_battery_capacity=None)
        row = collect.collect_sample(vehicle, "vid1", now, "post_charge", 71.2, "config")
        self.assertEqual(row["battery_pct"], "")
        self.assertEqual(row["odometer_km"], "")
        self.assertEqual(row["ev_capacity_raw"], "")


class AppendSamplesTest(unittest.TestCase):
    """Tests for collect.append_samples_row append/header behaviour."""

    def test_header_written_once_and_rows_append(self):
        """First write creates the header; later writes only add rows."""
        now = datetime(2026, 11, 13, 6, 30, tzinfo=kia_common.get_timezone(CONFIG))
        row = collect.collect_sample(fake_vehicle(), "vid1", now,
                                     "post_charge", 71.2, "api")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "samples.csv"
            collect.append_samples_row(path, row)
            collect.append_samples_row(path, dict(row, battery_pct=80))
            rows = read_rows(path)
            with open(path, encoding="utf-8") as handle:
                header_count = sum(1 for line in handle
                                   if line.startswith("timestamp_local,"))
        self.assertEqual(header_count, 1)
        self.assertEqual(len(rows), 2)
        self.assertEqual([r["battery_pct"] for r in rows], ["57", "80"])


class UpsertDailyStatsTest(unittest.TestCase):
    """Tests for collect.upsert_daily_stats merge/refresh semantics."""

    def day(self, iso: str, wh: int, km: float, unit: str = "km") -> FakeDayStat:
        """A FakeDayStat for the given ISO date with Wh consumption."""
        return FakeDayStat(date=datetime.fromisoformat(iso), total_consumed=wh,
                           distance=km, distance_unit=unit)

    def test_wh_to_kwh_and_new_file(self):
        """Watt-hours must be stored as kWh on first write."""
        vehicle = fake_vehicle(daily_stats=[
            self.day("2026-11-12", 5000, 25.0),
            self.day("2026-11-13", 3000, 15.0),
        ])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "daily.csv"
            count = collect.upsert_daily_stats(path, vehicle, "2026-11-13T00:00:00+00:00")
            rows = {r["date"]: r for r in read_rows(path)}
        self.assertEqual(count, 2)
        self.assertEqual(rows["2026-11-12"]["kwh_used"], "5.0")
        self.assertEqual(rows["2026-11-12"]["distance_km"], "25.0")

    def test_refresh_overwrites_same_date_and_keeps_old(self):
        """Newer API figures replace same dates; dropped dates survive."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "daily.csv"
            first = fake_vehicle(daily_stats=[
                self.day("2026-11-12", 5000, 25.0),
                self.day("2026-11-11", 1000, 5.0),
            ])
            collect.upsert_daily_stats(path, first, "t1")
            second = fake_vehicle(daily_stats=[self.day("2026-11-12", 6000, 30.0)])
            collect.upsert_daily_stats(path, second, "t2")
            rows = {r["date"]: r for r in read_rows(path)}
        self.assertEqual(rows["2026-11-12"]["kwh_used"], "6.0")
        self.assertEqual(rows["2026-11-12"]["collected_at"], "t2")
        self.assertEqual(rows["2026-11-11"]["kwh_used"], "1.0")  # preserved

    def test_duplicate_dates_keep_largest_consumption(self):
        """Two API entries for one date: the most complete one wins."""
        vehicle = fake_vehicle(daily_stats=[
            self.day("2026-11-12", 6000, 30.0),
            self.day("2026-11-12", 7000, 35.0),
        ])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "daily.csv"
            collect.upsert_daily_stats(path, vehicle, "t1")
            rows = read_rows(path)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kwh_used"], "7.0")

    def test_miles_converted_to_km(self):
        """distance_unit=mi must be stored converted to km."""
        vehicle = fake_vehicle(daily_stats=[self.day("2026-11-12", 5000, 10.0, "mi")])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "daily.csv"
            collect.upsert_daily_stats(path, vehicle, "t1")
            rows = read_rows(path)
        self.assertAlmostEqual(float(rows[0]["distance_km"]), 16.09344, places=4)

    def test_existing_file_survives_empty_api_response(self):
        """An API response with no stats must not wipe stored history."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "daily.csv"
            write_daily_csv(path, [{"date": "2026-10-01", "distance_km": 40.0,
                                    "kwh_used": 8.0}])
            count = collect.upsert_daily_stats(
                path, fake_vehicle(daily_stats=[]), "t1"
            )
            rows = read_rows(path)
        self.assertEqual(count, 0)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["date"], "2026-10-01")

    def test_rows_sorted_by_date(self):
        """Output rows must be ordered by ascending date."""
        vehicle = fake_vehicle(daily_stats=[
            self.day("2026-11-13", 3000, 15.0),
            self.day("2026-11-12", 5000, 25.0),
        ])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "daily.csv"
            collect.upsert_daily_stats(path, vehicle, "t1")
            rows = read_rows(path)
        self.assertEqual([r["date"] for r in rows], ["2026-11-12", "2026-11-13"])


if __name__ == "__main__":
    unittest.main()
