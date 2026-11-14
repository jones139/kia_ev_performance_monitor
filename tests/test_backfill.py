"""Unit tests for one_off/backfill_api_history.py (offline only).

The live API call is not exercised; these tests cover the pure conversion
and merge helpers that decide what lands in daily.csv.

Run from the repository root with:
    venv/bin/python -m unittest discover -s tests -v
or directly:
    venv/bin/python tests/test_backfill.py
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

try:
    import support  # noqa: F401  (sets up sys.path)
except ImportError:  # loaded as tests.test_x package member
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import support  # noqa: E402,F401

sys.path.insert(0, str(support.REPO_ROOT / "one_off"))
import backfill_api_history as backfill  # noqa: E402

DETAIL = [
    {   # a normal driving day: energy in Wh, distance in km
        "drivingDate": "20260818", "drivingPeriod": 0,
        "totalPwrCsp": 13865, "motorPwrCsp": 11272, "climatePwrCsp": 1763,
        "eDPwrCsp": 830, "batteryMgPwrCsp": 0, "regenPwr": 7339,
        "calculativeOdo": 83,
    },
    {   # the partial "today" row must be skipped
        "drivingDate": "20260819", "drivingPeriod": 1,
        "totalPwrCsp": 1490, "calculativeOdo": 9,
    },
    {   # a day with no reported energy is skipped
        "drivingDate": "20260820", "drivingPeriod": 0,
        "totalPwrCsp": 0, "calculativeOdo": 0,
    },
]


class RowsFromDetailTest(unittest.TestCase):
    """Tests for backfill.rows_from_detail."""

    def test_converts_units_and_skips_partial_rows(self):
        """Wh -> kWh, km kept, and period!=0 / zero-energy rows dropped."""
        rows = backfill.rows_from_detail(DETAIL, "km", "2026-11-13T20:00:00+00:00")
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["date"], "2026-08-18")
        self.assertAlmostEqual(row["kwh_used"], 13.865, places=6)
        self.assertAlmostEqual(row["engine_kwh"], 11.272, places=6)
        self.assertAlmostEqual(row["climate_kwh"], 1.763, places=6)
        self.assertAlmostEqual(row["regen_kwh"], 7.339, places=6)
        self.assertEqual(row["distance_km"], 83.0)
        self.assertEqual(row["source"], "api")
        self.assertEqual(row["distance_range_km"], "")

    def test_miles_are_converted_to_km(self):
        """A mile-based API reading is normalised to km."""
        rows = backfill.rows_from_detail(DETAIL[:1], "mi", "2026-11-13T20:00:00+00:00")
        self.assertAlmostEqual(rows[0]["distance_km"], 83 * 1.609344, places=6)


class MergeDailyRowsTest(unittest.TestCase):
    """Tests for backfill.merge_daily_rows."""

    def test_preserves_existing_range_distance_and_other_days(self):
        """Upsert keeps distance_range_km and leaves untouched dates alone."""
        existing = {
            "2026-08-18": {"date": "2026-08-18", "distance_range_km": "82.5",
                           "distance_km": "80.0"},
            "2026-08-01": {"date": "2026-08-01", "distance_range_km": "",
                           "distance_km": "12.0"},
        }
        new_rows = backfill.rows_from_detail(
            DETAIL[:1], "km", "2026-11-13T20:00:00+00:00"
        )
        written = backfill.merge_daily_rows(existing, new_rows)
        self.assertEqual(written, 1)
        self.assertEqual(existing["2026-08-18"]["distance_range_km"], "82.5")
        self.assertEqual(existing["2026-08-18"]["distance_km"], 83.0)
        self.assertIn("2026-08-01", existing)

    def test_round_trip_through_csv(self):
        """read -> merge -> write produces rows with every DAILY_COLUMNS key."""
        tmp = Path(tempfile.mkdtemp(prefix="kia_backfill_", dir="/tmp/opencode"))
        self.addCleanup(shutil.rmtree, tmp, True)
        path = tmp / "daily.csv"
        existing = backfill.read_daily_rows(path)
        new_rows = backfill.rows_from_detail(
            DETAIL[:1], "km", "2026-11-13T20:00:00+00:00"
        )
        backfill.merge_daily_rows(existing, new_rows)
        backfill.write_daily_rows(path, existing)
        reloaded = backfill.read_daily_rows(path)
        self.assertEqual(sorted(reloaded), ["2026-08-18"])
        self.assertEqual(set(reloaded["2026-08-18"]),
                         set(support_daily_columns()))


def support_daily_columns() -> list[str]:
    """Return the daily.csv column list (avoids importing collect at load)."""
    import collect
    return collect.DAILY_COLUMNS


if __name__ == "__main__":
    unittest.main()
