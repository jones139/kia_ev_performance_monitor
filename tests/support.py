"""Shared helpers for the Kia EV scripts' test suite.

Provides:
  * sys.path setup so test modules can import kia_common / collect / analyze
    from the repository root regardless of the working directory
  * lightweight stand-ins for the API's Vehicle / DailyDrivingStats objects
    (FakeDayStat, fake_vehicle) so tests never touch the network
  * CSV writers matching the exact column layouts used by collect.py
  * build_synthetic_dataset(): a deterministic fake driving history
    (battery samples + per-day stats) whose true full-charge range,
    efficiency and usable capacity are known, so analyze.py's estimates
    can be asserted against ground truth
  * run_analyze(): runs analyze.py as a subprocess against a temp data dir

Interface notes: every function is pure/deterministic and network-free.
"""

from __future__ import annotations

import csv
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import collect  # noqa: E402  (imported for its CSV column layouts)


@dataclass
class FakeDayStat:
    """Stand-in for hyundai_kia_connect_api.DailyDrivingStats.

    Fields mirror the API dataclass: ``date`` (datetime), energy figures in
    WATT-HOURS (total_consumed, engine_consumption, climate_consumption,
    onboard_electronics_consumption, battery_care_consumption,
    regenerated_energy) and ``distance`` with ``distance_unit`` ("km"/"mi").
    """

    date: datetime | None = None
    total_consumed: int | None = None
    engine_consumption: int | None = None
    climate_consumption: int | None = None
    onboard_electronics_consumption: int | None = None
    battery_care_consumption: int | None = None
    regenerated_energy: int | None = None
    distance: float | None = None
    distance_unit: str | None = "km"


def fake_vehicle(**overrides) -> SimpleNamespace:
    """Build a minimal stand-in for the API's Vehicle object.

    Interface: keyword arguments override the defaults (name, VIN,
    ev_battery_percentage, odometer, odometer_unit, ev_battery_capacity,
    daily_stats, last_updated_at).  Returns a types.SimpleNamespace with
    exactly the attributes that collect.py / kia_common read, so tests run
    without the Kia API installed or any network access.
    """
    vehicle = SimpleNamespace(
        name="PV5",
        VIN="TESTVIN0001",
        ev_battery_percentage=57,
        odometer=9363.2,
        odometer_unit="km",
        ev_battery_capacity=256320,
        daily_stats=[],
        last_updated_at=datetime(2026, 11, 13, 18, 0, 0),
    )
    for key, value in overrides.items():
        setattr(vehicle, key, value)
    return vehicle


def write_samples_csv(path: Path, rows: list[dict]) -> None:
    """Write a samples.csv file with collect.py's exact column layout.

    Interface: takes the destination Path and a list of dicts (any subset
    of collect.SAMPLE_COLUMNS; missing keys become empty cells).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=collect.SAMPLE_COLUMNS,
                                restval="")
        writer.writeheader()
        writer.writerows(rows)


def write_daily_csv(path: Path, rows: list[dict]) -> None:
    """Write a daily.csv file with collect.py's exact column layout.

    Interface: takes the destination Path and a list of dicts (any subset
    of collect.DAILY_COLUMNS; missing keys become empty cells).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=collect.DAILY_COLUMNS,
                                restval="")
        writer.writeheader()
        writer.writerows(rows)


def build_synthetic_dataset(
    start_day: date,
    distances_km: list[float],
    efficiency_km_per_kwh: float = 5.0,
    full_range_km: float = 356.0,
    capacity_kwh: float = 71.2,
    charged_days: set[int] | None = None,
    no_sample_days: set[int] | None = None,
    morning_pct: float = 90.0,
) -> tuple[list[dict], list[dict]]:
    """Create a deterministic fake driving history with known ground truth.

    Interface: takes a start date, a list of per-day distances in km (index
    0 = start_day), the true efficiency and true full-charge range used to
    derive energy use and battery-% drops, the usable capacity recorded in
    the samples, and sets of day indexes that were charged during the day
    (evening % > morning %, invalidating method 1) or have no battery
    samples at all.  Returns (samples_rows, daily_rows) ready for
    write_samples_csv / write_daily_csv.

    Ground truth: kwh_used = distance / efficiency, battery-% drop =
    distance / full_range * 100, so analyze.py should recover
    ``full_range_km`` from both methods and ``capacity_kwh`` as the implied
    capacity.  Days with <1 km appear in neither daily row nor samples.
    """
    charged_days = charged_days or set()
    no_sample_days = no_sample_days or set()
    samples_rows: list[dict] = []
    daily_rows: list[dict] = []

    for offset, km in enumerate(distances_km):
        day = start_day + timedelta(days=offset)
        if km < 1.0:
            continue  # rest day: no driving stats reported
        kwh = round(km / efficiency_km_per_kwh, 3)
        daily_rows.append({
            "date": day.isoformat(),
            "distance_km": km,
            "kwh_used": kwh,
            "engine_kwh": round(kwh * 0.85, 4),
            "climate_kwh": round(kwh * 0.10, 4),
            "electronics_kwh": round(kwh * 0.05, 4),
            "battery_care_kwh": 0.0,
            "regen_kwh": round(kwh * 0.15, 4),
            "collected_at": "2026-11-13T00:00:00+00:00",
        })
        if offset in no_sample_days:
            continue
        pct_drop = round(km / full_range_km * 100.0, 2)
        evening_pct = 95.0 if offset in charged_days else morning_pct - pct_drop
        morning_ts = datetime.combine(day, time(6, 30))
        evening_ts = datetime.combine(day, time(23, 45))
        for ts, pct, sample_type in (
            (morning_ts, morning_pct, "post_charge"),
            (evening_ts, evening_pct, "pre_charge"),
        ):
            samples_rows.append({
                "timestamp_local": ts.isoformat(timespec="seconds"),
                "date_local": day.isoformat(),
                "sample_type": sample_type,
                "vehicle_name": "PV5-TEST",
                "vin": "TESTVIN0001",
                "battery_pct": pct,
                "odometer_km": 9000.0 + km,
                "odometer_unit": "km",
                "ev_capacity_raw": 256320,
                "capacity_kwh_used": capacity_kwh,
                "capacity_source": "api",
                "vehicle_updated_at": ts.isoformat(timespec="seconds"),
            })
    return samples_rows, daily_rows


def run_analyze(data_dir: Path, output: Path,
                extra_args: list[str] | None = None) -> subprocess.CompletedProcess:
    """Run analyze.py as a subprocess against a temporary data directory.

    Interface: takes the temp data dir (containing samples.csv/daily.csv),
    the output PNG path and any extra CLI arguments (e.g.
    ["--capacity-kwh", "50"]).  Runs with the same interpreter executing
    the tests; returns the CompletedProcess (stdout/stderr captured).

    Reporting units are pinned to km so assertions do not depend on the
    user's [analysis].output_units preference; extras are appended after
    the pin, so an explicit ``--units miles`` in ``extra_args`` still wins.
    """
    command = [
        sys.executable, str(REPO_ROOT / "analyze.py"),
        "--data-dir", str(data_dir),
        "--output", str(output),
        "--units", "km",
        *(extra_args or []),
    ]
    return subprocess.run(command, capture_output=True, text=True,
                          cwd=str(REPO_ROOT))


def parse_rolling_values(stdout: str) -> list[float]:
    """Extract the rolling-average range values from an analyze.py summary.

    Interface: takes the summary text printed by analyze.py and returns the
    rolling-average range values (in whatever units the run reported) in
    order of appearance (method 1 first, then method 2).  Methods with no
    estimates contribute nothing.
    """
    return [float(value) for value in
            re.findall(r"rolling avg\s*:\s*([\d.]+)\s*(?:km|mi)\b", stdout)]


def parse_planning_values(stdout: str) -> list[float]:
    """Extract the planning-range values from an analyze.py summary.

    Interface: takes the summary text printed by analyze.py and returns the
    planning-range values (in whatever units the run reported) in order of
    appearance (method 1 first, then method 2).  Methods with no estimates
    contribute nothing.
    """
    return [float(value) for value in
            re.findall(r"planning range\s*:\s*([\d.]+)\s*(?:km|mi)\b", stdout)]


def run_publish(config_path: Path,
                extra_args: list[str] | None = None) -> subprocess.CompletedProcess:
    """Run publish.py as a subprocess against a temporary config file.

    Interface: takes the config.toml path (normally written into a temp
    directory and pointing [paths].data_dir / [publish].output_dir at that
    directory) and any extra CLI arguments (e.g. ["--units", "miles"]).
    Runs with the same interpreter executing the tests; returns the
    CompletedProcess (stdout/stderr captured).
    """
    command = [
        sys.executable, str(REPO_ROOT / "publish.py"),
        "--config", str(config_path),
        *(extra_args or []),
    ]
    return subprocess.run(command, capture_output=True, text=True,
                          cwd=str(REPO_ROOT))
