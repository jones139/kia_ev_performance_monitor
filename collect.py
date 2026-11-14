#!/usr/bin/env python3
"""Collect Kia EV battery / usage data and store it in CSV files.

Designed to be run TWICE a day from cron on a server (see README.md):

  * just BEFORE the scheduled overnight charge starts (e.g. 23:45):
    a "pre_charge" sample - the battery level at the END of the day.
  * just AFTER the overnight charge finishes (e.g. 06:30):
    a "post_charge" sample - the battery level at the START of the day.

Assumption: the car is not driven overnight, so the post_charge sample is
the day's starting battery level and the pre_charge sample is its ending
level.

Two files are written into the data directory (see config.toml [paths]):

  samples.csv : one APPENDED row per run - timestamp, sample type, battery
                %, odometer, battery-capacity info, provenance (source).
                Two rows per day give the start/end battery levels.

  daily.csv   : per-calendar-day driving stats reported by the vehicle API
                (distance km, kWh used, plus the consumption breakdown),
                UPSERTED on every run: the API's value for a day is
                refreshed each time, so partial "today" figures are later
                replaced by complete ones, and history older than the API's
                ~30-day window is preserved.  The distance_range_km column
                holds the spreadsheet import's period-consistent distance
                share used by method 1 (empty otherwise).

analyze.py later combines these files to estimate vehicle range.

The authentication token is cached between runs (by default in
credentials.token.json, next to the credentials file) so that the twice-daily
runs reuse its refresh token instead of logging in with the password every
time.  A corrupt or rejected cache is ignored and the script simply logs in
again.

Command line interface:
    collect.py [--config FILE] [--credentials FILE]
               [--sample-type {auto,pre_charge,post_charge}]
    (auto infers the type from the local hour vs morning_cutoff_hour)
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
from datetime import date, datetime, timezone

import kia_common
from kia_common import logger

# Column layout of samples.csv (one row per collect.py run).
SAMPLE_COLUMNS = [
    "timestamp_local",     # ISO timestamp of this run (with UTC offset)
    "date_local",          # local calendar date (YYYY-MM-DD) this sample belongs to
    "sample_type",         # "post_charge" (start of day) or "pre_charge" (end of day)
    "vehicle_name",        # friendly vehicle name from the API
    "vin",                 # vehicle VIN
    "battery_pct",         # EV traction battery level, %
    "odometer_km",         # odometer reading, km
    "odometer_unit",       # unit reported by the API (expected: km)
    "ev_capacity_raw",     # raw vehicle.ev_battery_capacity value from the API
    "capacity_kwh_used",   # resolved usable capacity in kWh (see kia_common)
    "capacity_source",     # "api" or "config" - which value was used
    "vehicle_updated_at",  # how fresh the vehicle's cached state is
    "source",              # provenance: "collect", "manual" or "manual_derived"
]

# Column layout of daily.csv (one row per calendar day, refreshed per run).
DAILY_COLUMNS = [
    "date",                 # calendar date (YYYY-MM-DD) as reported by the API
    "distance_km",          # distance driven that day, km
    "kwh_used",             # total energy consumed that day, kWh
    "engine_kwh",           # drivetrain consumption, kWh
    "climate_kwh",          # climate control consumption, kWh
    "electronics_kwh",      # onboard electronics consumption, kWh
    "battery_care_kwh",     # battery care consumption, kWh
    "regen_kwh",            # regenerated (recovered) energy, kWh
    "collected_at",         # UTC ISO timestamp of the run that last wrote this row
    "source",               # provenance: "api" (collect.py) or "manual_derived"
    "distance_range_km",    # period-consistent distance share for method 1
]


def determine_sample_type(now: datetime, config: dict, override: str = "auto") -> str:
    """Decide whether this run is a start-of-day or end-of-day sample.

    Interface: takes the current timezone-aware datetime, the config dict
    and the --sample-type CLI value ("auto", "pre_charge" or "post_charge").
    Returns "pre_charge" or "post_charge".  With "auto", a run before
    [schedule].morning_cutoff_hour (local) is assumed to be just after the
    overnight charge ("post_charge"), and a later run just before it
    ("pre_charge").
    """
    if override != "auto":
        return override
    cutoff = int(config["schedule"]["morning_cutoff_hour"])
    return "post_charge" if now.hour < cutoff else "pre_charge"


def collect_sample(
    vehicle,
    vehicle_id: str,
    now: datetime,
    sample_type: str,
    capacity_kwh: float,
    capacity_source: str,
    source: str = "collect",
) -> dict:
    """Build one samples.csv row from the current vehicle state.

    Interface: takes the Vehicle object, its API id, the timezone-aware run
    time, the sample type, the resolved capacity (value + source code) and
    a provenance tag (``source``: "collect" for API collector runs; the
    one-off spreadsheet import writes "manual"/"manual_derived" rows
    itself).  Returns a dict keyed by SAMPLE_COLUMNS with values converted
    to strings suitable for CSV ("''" for missing values).
    """
    return {
        "timestamp_local": now.isoformat(timespec="seconds"),
        "date_local": now.date().isoformat(),
        "sample_type": sample_type,
        "vehicle_name": getattr(vehicle, "name", "") or "",
        "vin": getattr(vehicle, "VIN", "") or vehicle_id,
        "battery_pct": "" if vehicle.ev_battery_percentage is None
                       else vehicle.ev_battery_percentage,
        "odometer_km": "" if vehicle.odometer is None else vehicle.odometer,
        "odometer_unit": getattr(vehicle, "odometer_unit", "") or "",
        "ev_capacity_raw": "" if vehicle.ev_battery_capacity is None
                           else vehicle.ev_battery_capacity,
        "capacity_kwh_used": round(capacity_kwh, 3),
        "capacity_source": capacity_source,
        "vehicle_updated_at": kia_common.vehicle_data_timestamp(vehicle),
        "source": source,
    }


def append_samples_row(path, row: dict) -> None:
    """Append one sample row to samples.csv, creating the file if needed.

    Interface: takes the destination Path and a dict keyed by
    SAMPLE_COLUMNS.  Writes a header line first when the file is new or
    empty.  The file is append-only; nothing is ever rewritten.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not path.exists() or path.stat().st_size == 0
    with open(path, "a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=SAMPLE_COLUMNS)
        if is_new:
            writer.writeheader()
        writer.writerow(row)


def _wh_to_kwh(value) -> float | None:
    """Convert a watt-hour figure from the API to kWh (None passes through)."""
    return None if value is None else round(float(value) / 1000.0, 4)


def upsert_daily_stats(path, vehicle, collected_at: str) -> int:
    """Write the API's per-day driving stats into daily.csv (upsert).

    Interface: takes the destination Path, the Vehicle object and an ISO
    timestamp marking when this data was collected.  Returns the number of
    API day-entries processed.

    Existing rows for the same date are REPLACED by the newest API figures
    (the API's entry for 'today' is partial until the day ends, so later
    runs refine it); dates that have dropped out of the API's limited
    history window are kept, so the CSV accumulates a permanent record.
    If one API response contains duplicate entries for a date, the entry
    with the largest total_consumed (i.e. the most complete) wins.
    """
    # Read existing history (if any), keyed by date string.
    existing: dict[str, dict] = {}
    if path.exists() and path.stat().st_size > 0:
        with open(path, "r", newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                if row.get("date"):
                    row.setdefault("source", "api")  # files predate the column
                    row.setdefault("distance_range_km", "")
                    existing[row["date"]] = row

    # Process the API's daily stats, best (largest consumption) entry per date.
    updates: dict[str, dict] = {}
    for day in getattr(vehicle, "daily_stats", []) or []:
        if day.date is None:
            continue
        day_date = day.date.date() if isinstance(day.date, datetime) else day.date
        key = day_date.isoformat() if isinstance(day_date, date) else str(day_date)
        candidate = {
            "date": key,
            "distance_km": kia_common.normalise_distance_km(
                day.distance, getattr(day, "distance_unit", None)
            ),
            "kwh_used": _wh_to_kwh(day.total_consumed),
            "engine_kwh": _wh_to_kwh(day.engine_consumption),
            "climate_kwh": _wh_to_kwh(day.climate_consumption),
            "electronics_kwh": _wh_to_kwh(day.onboard_electronics_consumption),
            "battery_care_kwh": _wh_to_kwh(day.battery_care_consumption),
            "regen_kwh": _wh_to_kwh(day.regenerated_energy),
            "collected_at": collected_at,
            "source": "api",
            "distance_range_km": "",
        }
        previous = updates.get(key)
        if previous is None or (candidate["kwh_used"] or 0) >= (previous["kwh_used"] or 0):
            updates[key] = candidate

    # Merge: newest API figures overwrite same-date rows; old rows are kept.
    existing.update(updates)

    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=DAILY_COLUMNS)
        writer.writeheader()
        for key in sorted(existing):
            writer.writerow(existing[key])
    return len(updates)


def connect_and_refresh(credentials: dict, config: dict, token_path):
    """Connect to the Kia API, reusing and refreshing the cached token.

    Interface: takes the credentials dict, the loaded config dict and the
    token cache Path.  Loads a cached token when present, asks the API
    library to refresh its short-lived access token, and saves the updated
    token back to ``token_path`` for the next run.  If the cached token is
    rejected (e.g. its refresh token has expired) it falls back to a
    password login, so a stale cache can never wedge the collector.
    Returns the refreshed VehicleManager.
    """
    retries = int(config["api"]["retry_count"])
    delay_seconds = float(config["api"]["retry_delay_seconds"])

    cached_token = kia_common.load_cached_token(token_path)
    if cached_token is not None:
        print("collect: connect_and_refresh() - no cached token found, logging in with credentials")
        cached_username = getattr(cached_token, "username", None)
        if cached_username and cached_username != credentials["username"]:
            logger.warning(
                "Cached token belongs to %s, not %s; logging in again",
                cached_username, credentials["username"],
            )
            cached_token = None
    else:
        print("collect: connect_and_refresh() - cached token found, attempting to refresh")

    vm = kia_common.connect_vehicle_manager(credentials, token=cached_token)
    try:
        kia_common.refresh_vehicles(
            vm, retries=retries, delay_seconds=delay_seconds
        )
    except Exception as error:  # noqa: BLE001 - retry with a fresh login
        if cached_token is None:
            raise
        logger.warning(
            "Cached authentication token could not be used (%s); "
            "logging in with the stored credentials",
            error,
        )
        vm = kia_common.connect_vehicle_manager(credentials)
        kia_common.refresh_vehicles(
            vm, retries=retries, delay_seconds=delay_seconds
        )

    # Persist the (possibly refreshed) token.  A read-only directory must
    # not lose the sample we have already fetched, so save failures only
    # produce a warning; the next run will just log in again.
    try:
        kia_common.save_cached_token(vm.token, token_path)
        logger.debug("Saved authentication token to %s", token_path)
    except OSError as error:
        logger.warning(
            "Could not save authentication token to %s (%s); "
            "the next run will log in again",
            token_path, error,
        )
    return vm


def main(argv: list[str] | None = None) -> int:
    """Entry point: fetch vehicle state once and record it to the CSV files.

    Interface: takes optional argv (defaults to sys.argv[1:]); supports
    --config, --credentials and --sample-type.  Returns 0 on success, 1 on
    any failure (logged), so cron can detect problems.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", help="path to config.toml")
    parser.add_argument("--credentials", help="path to credentials.json")
    parser.add_argument(
        "--sample-type",
        choices=("auto", "pre_charge", "post_charge"),
        default="auto",
        help="what this run represents (default: infer from the local hour)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )

    try:
        config = kia_common.load_config(args.config)
        credentials = kia_common.load_credentials(args.credentials)
        paths = kia_common.paths_from_config(config)
        local_timezone = kia_common.get_timezone(config)
        now = datetime.now(local_timezone)
        sample_type = determine_sample_type(now, config, args.sample_type)

        # Connect to the Kia Connect API and refresh vehicle state, reusing
        # the token from the previous run when possible.
        token_path = kia_common.token_cache_path(args.credentials)
        vm = connect_and_refresh(credentials, config, token_path)
        vehicle_id, vehicle = kia_common.select_vehicle(
            vm, config["vehicle"]["vin"]
        )
        capacity_kwh, capacity_source, capacity_desc = (
            kia_common.resolve_battery_capacity_kwh(vehicle, config)
        )

        # Record the instant sample (battery level etc.).
        row = collect_sample(
            vehicle, vehicle_id, now, sample_type, capacity_kwh, capacity_source
        )
        append_samples_row(paths["samples"], row)
        logger.info(
            "Recorded %s sample for %s: battery %s%%, odometer %s km, "
            "capacity %s (%s)",
            sample_type, row["vehicle_name"], row["battery_pct"],
            row["odometer_km"], f"{capacity_kwh:g} kWh", capacity_desc,
        )

        # Refresh the per-day driving stats history.
        collected_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        day_count = upsert_daily_stats(paths["daily"], vehicle, collected_at)
        logger.info(
            "Updated %d day(s) of driving stats in %s", day_count, paths["daily"]
        )
        return 0
    except Exception:  # noqa: BLE001 - top-level guard for cron
        logger.exception("Data collection failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
