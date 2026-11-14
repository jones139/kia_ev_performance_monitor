#!/usr/bin/env python3
"""One-off: backfill the API's ~90-day daily driving history into daily.csv.

WHY THIS EXISTS
    collect.py stores vehicle.daily_stats, which the library fills from the
    API's 30-day window (drvhistory with periodTarget=0).  The same endpoint
    also accepts periodTarget=1 and returns a longer detail list (observed:
    73 driving days spanning ~90 calendar days) that the library discards.
    Those older days fall out of the API permanently, so run this soon after
    setting the project up to keep them.

WHAT IT DOES
    Logs in with credentials.json, fetches the longer detail list and upserts
    every day into the [paths].daily_file CSV (source="api").  Existing rows
    are preserved where the backfill has nothing better: a row's method-1
    distance_range_km column is never overwritten.  Running it repeatedly is
    safe (upsert on the date column).

Command line interface:
    backfill_api_history.py [--config FILE] [--credentials FILE]
    Uses the same config.toml / credentials.json as collect.py.

This script is archived in one_off/ because it is a one-time data seed, not
part of the regular cron pipeline.
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import collect  # noqa: E402  (DAILY_COLUMNS, CSV helpers)
import kia_common  # noqa: E402
from kia_common import logger  # noqa: E402

# The library's name for the longer window (behaviour verified August 2026).
ALL_TIME_PERIOD = 1


def fetch_all_time_detail(vm, vehicle) -> list[dict]:
    """Fetch the API's long-window daily detail list.

    Interface: takes a logged-in VehicleManager and the selected Vehicle.
    Posts the same drvhistory request the library uses for its 30-day
    stats, but with ``periodTarget=1`` (the longer window), and returns
    the ``resMsg.drivingInfoDetail`` list of raw day dicts.  Returns []
    when the response carries no detail.  Raises the underlying requests
    or API error on transport failure.
    """
    url = vm.api.SPA_API_URL + "vehicles/" + vehicle.id + "/drvhistory"
    response = vm.api.session.post(
        url,
        json={"periodTarget": ALL_TIME_PERIOD},
        headers=vm.api._get_vehicle_headers(vm.token, vehicle),
    )
    payload = response.json()
    return payload.get("resMsg", {}).get("drivingInfoDetail", []) or []


def rows_from_detail(
    detail: list[dict], distance_unit: str | None, collected_at: str
) -> list[dict]:
    """Convert raw API day dicts into daily.csv rows.

    Interface: takes the detail list (fetch_all_time_detail), the unit the
    API uses for distance (vehicle.odometer_unit) and the collection
    timestamp.  Returns a list of dicts keyed by collect.DAILY_COLUMNS,
    one per day with totalPwrCsp > 0.  Energy values are Wh in the API
    and are stored as kWh; distance is normalised to km.  The
    distance_range_km cell is left empty, so an existing imported value
    survives the merge (see merge_daily_rows).
    """
    rows: list[dict] = []
    for day in detail:
        if day.get("drivingPeriod") != 0:
            continue                       # skip the partial "today" row
        total = day.get("totalPwrCsp")
        if not total:
            continue
        rows.append({
            "date": datetime.strptime(day["drivingDate"], "%Y%m%d").date().isoformat(),
            "distance_km": kia_common.normalise_distance_km(
                day.get("calculativeOdo"), distance_unit
            ),
            "kwh_used": float(total) / 1000.0,
            "engine_kwh": float(day.get("motorPwrCsp") or 0.0) / 1000.0,
            "climate_kwh": float(day.get("climatePwrCsp") or 0.0) / 1000.0,
            "electronics_kwh": float(day.get("eDPwrCsp") or 0.0) / 1000.0,
            "battery_care_kwh": float(day.get("batteryMgPwrCsp") or 0.0) / 1000.0,
            "regen_kwh": float(day.get("regenPwr") or 0.0) / 1000.0,
            "collected_at": collected_at,
            "source": "api",
            "distance_range_km": "",
        })
    return rows


def read_daily_rows(path: Path) -> dict[str, dict]:
    """Read daily.csv into a {date: row} dict (empty when absent).

    Interface: takes the CSV Path; returns normalised rows (missing
    columns added as empty strings) so older files remain usable.
    """
    rows: dict[str, dict] = {}
    if not path.exists():
        return rows
    with open(path, "r", newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            normalised = {column: row.get(column, "") or ""
                          for column in collect.DAILY_COLUMNS}
            if normalised["date"]:
                rows[normalised["date"]] = normalised
    return rows


def merge_daily_rows(existing: dict[str, dict],
                     new_rows: list[dict]) -> int:
    """Upsert backfilled rows into the existing date -> row mapping.

    Interface: takes the mapping from read_daily_rows and the rows from
    rows_from_detail; returns the number of days written.  A backfilled
    row replaces the same date but keeps any non-empty
    ``distance_range_km`` already present (the spreadsheet import's
    period-consistent distance share).  Existing dates NOT in the
    backfill are left untouched.
    """
    written = 0
    for row in new_rows:
        key = row["date"]
        previous = existing.get(key)
        merged = dict(row)
        if previous and previous.get("distance_range_km"):
            merged["distance_range_km"] = previous["distance_range_km"]
        existing[key] = merged
        written += 1
    return written


def write_daily_rows(path: Path, rows: dict[str, dict]) -> None:
    """Rewrite daily.csv from the date -> row mapping, sorted by date."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=collect.DAILY_COLUMNS)
        writer.writeheader()
        for key in sorted(rows):
            writer.writerow(rows[key])


def main(argv: list[str] | None = None) -> int:
    """Entry point: fetch the long-window detail and merge it into daily.csv.

    Interface: takes optional argv; supports --config and --credentials.
    Returns 0 on success, 1 on failure (logged).  Writes only the daily
    CSV; samples and the graph are untouched.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", help="path to config.toml")
    parser.add_argument("--credentials", help="path to credentials.json")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    try:
        config = kia_common.load_config(args.config)
        credentials = kia_common.load_credentials(args.credentials)
        paths = kia_common.paths_from_config(config)
        vm = kia_common.connect_vehicle_manager(credentials)
        kia_common.refresh_vehicles(
            vm,
            retries=int(config["api"]["retry_count"]),
            delay_seconds=float(config["api"]["retry_delay_seconds"]),
        )
        _, vehicle = kia_common.select_vehicle(vm, config["vehicle"]["vin"])
        detail = fetch_all_time_detail(vm, vehicle)
        if not detail:
            logger.warning("API returned no daily detail; nothing to do")
            return 1
        collected_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        new_rows = rows_from_detail(detail, vehicle.odometer_unit, collected_at)
        daily_path = paths["daily"]
        existing = read_daily_rows(daily_path)
        written = merge_daily_rows(existing, new_rows)
        write_daily_rows(daily_path, existing)
        dates = sorted(row["date"] for row in new_rows)
        logger.info(
            "Backfilled %d day(s) (%s to %s) into %s",
            written, dates[0], dates[-1], daily_path,
        )
        return 0
    except Exception:  # noqa: BLE001 - top-level guard, like collect.py
        logger.exception("API history backfill failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
