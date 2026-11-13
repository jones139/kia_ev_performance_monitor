#!/usr/bin/env python3
"""One-off conversion: pre-populate the collector's CSVs from the manual
charging log spreadsheet (PV5_Efficiency.xlsx, "Charging" sheet).

The spreadsheet records one row PER CHARGE EVENT since the car was new:
date, odometer (miles), battery % at charge start and end, energy
supplied (kWh, charger side), location and notes.  It contains NO
vehicle-consumption kWh figures, so only battery-% samples can be
pre-populated from it; per-day kWh usage continues to come from the API
(collect.py / daily.csv).

What this script produces:

  * one_off/manual_charges_parsed.csv
      a normalised, human-checkable copy of every charge event and every
      inter-charge driving period (the "future reference" record of the
      conversion inputs).

  * data/samples.csv  (merged, idempotent)
      - "manual" rows: one pre_charge sample at each charge start
        (battery % start) and one post_charge sample at each charge end
        (battery % end + odometer in km).
      - "manual_derived" rows: for each inter-charge driving period the
        measured % drop is spread over the calendar days it covers
        (proportional to the API's per-day distances where the whole
        period lies inside the API window, otherwise evenly), producing
        the day-boundary battery levels that analyze.py's method 1 needs
        for days with no charge event.

  * data/daily.csv  (merged, idempotent)
      - "manual_derived" rows for period days OUTSIDE the API's ~30-day
        history window: the period's odometer distance spread over those
        days with the same weights, kwh left empty (not known manually).
        Dates the API already covers are never touched.

Because the spreadsheet has no times of day, nominal times are assumed
(constants below): home charges run overnight (start ~23:30 the previous
evening, end ~07:30); away/en-route charges are placed mid-day
(start ~11:00, end ~12:00 on the recorded date).  analyze.py pairs each
day's first morning sample with the last evening sample AFTER it, so
mid-day away charges cannot invert a day's pair.

Re-running is safe: rows tagged source="manual*"/"manual_derived" are
removed and regenerated from the spreadsheet each time; rows written by
collect.py (source="collect"/"api") are preserved.

Command line interface:
    import_manual_charges.py [--xlsx FILE] [--config FILE] [--dry-run]
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent          # the one_off/ folder
REPO_ROOT = HERE.parent
sys.path.insert(0, str(REPO_ROOT))

import kia_common                               # noqa: E402
from collect import DAILY_COLUMNS, SAMPLE_COLUMNS  # noqa: E402
from kia_common import logger                   # noqa: E402

DEFAULT_XLSX = REPO_ROOT / "PV5_Efficiency.xlsx"

# Column indexes (1-based) of the "Charging" sheet.
COL_DATE, COL_MILES, COL_SOC_START, COL_SOC_END, COL_KWH_SUPPLIED = 1, 2, 3, 4, 5
COL_LOCATION, COL_NOTES = 9, 25
REGISTRATION_CELL = "A2"

# Nominal times of day (the spreadsheet records dates only).
HOME_CHARGE_END = time(7, 30)        # post_charge sample time, home charges
HOME_CHARGE_START = time(23, 30)     # pre_charge sample time, previous evening
AWAY_CHARGE_START = time(11, 0)      # pre_charge sample time, en-route charges
AWAY_CHARGE_END = time(12, 0)        # post_charge sample time, en-route charges
DERIVED_DAY_START = time(0, 1)       # manual_derived post_charge sample time
DERIVED_DAY_END = time(23, 59)       # manual_derived pre_charge sample time

MILES_TO_KM = 1.609344


@dataclass
class ChargeEvent:
    """One charge event parsed from a row of the spreadsheet.

    Fields: ``row`` (sheet row number), ``date`` (recorded charge date),
    ``odometer_km`` (odometer at the charge, converted from miles),
    ``soc_start``/``soc_end`` (battery % before/after charging),
    ``kwh_supplied`` (charger-side energy, reference only), ``location``,
    ``notes``.  Any may be None when the cell is empty.
    """

    row: int
    date: date
    odometer_km: float | None
    soc_start: float | None
    soc_end: float | None
    kwh_supplied: float | None
    location: str
    notes: str

    @property
    def is_home(self) -> bool:
        """True when the charge location indicates a home charge."""
        return (self.location or "").strip().lower() == "home"

    def start_datetime(self) -> datetime:
        """Nominal charge-start datetime (previous evening when at home)."""
        day = self.date - timedelta(days=1) if self.is_home else self.date
        stamp = HOME_CHARGE_START if self.is_home else AWAY_CHARGE_START
        return datetime.combine(day, stamp)

    def end_datetime(self) -> datetime:
        """Nominal charge-end datetime on the recorded date."""
        stamp = HOME_CHARGE_END if self.is_home else AWAY_CHARGE_END
        return datetime.combine(self.date, stamp)


@dataclass
class DrivingPeriod:
    """The driving interval between one charge ending and the next starting.

    Fields: ``start``/``end`` (datetimes of the bounding charge end/start),
    ``soc_from``/``soc_to`` (battery % at those moments), ``distance_km``
    (odometer difference, None when unusable), ``days`` (inclusive list of
    calendar dates spanned, ending on the date of the next charge start).
    """

    start: datetime
    end: datetime
    soc_from: float
    soc_to: float
    distance_km: float | None
    days: list[date]

    @property
    def pct_drop(self) -> float:
        """Battery percentage points consumed over the period."""
        return self.soc_from - self.soc_to


def parse_charge_events(xlsx_path: Path) -> tuple[list[ChargeEvent], str]:
    """Parse every charge-event row of the spreadsheet's Charging sheet.

    Interface: takes the Path to the .xlsx file and returns
    (events, registration) where events is a list of ChargeEvent in sheet
    order (rows whose first column is a date, starting under the header)
    and registration is the number-plate string from cell B2 ("" when
    absent).  Requires openpyxl.  Raises FileNotFoundError when the file
    is missing.
    """
    import openpyxl

    if not xlsx_path.exists():
        raise FileNotFoundError(f"Spreadsheet not found: {xlsx_path}")
    workbook = openpyxl.load_workbook(xlsx_path, data_only=True)
    sheet = workbook["Charging"]
    registration = sheet[REGISTRATION_CELL].value or ""
    events: list[ChargeEvent] = []
    for row in range(8, sheet.max_row + 1):
        raw_date = sheet.cell(row=row, column=COL_DATE).value
        if not isinstance(raw_date, datetime):
            continue  # end of the populated log (template rows follow)
        events.append(ChargeEvent(
            row=row,
            date=raw_date.date(),
            odometer_km=_as_km(sheet.cell(row=row, column=COL_MILES).value),
            soc_start=_as_float(sheet.cell(row=row, column=COL_SOC_START).value),
            soc_end=_as_float(sheet.cell(row=row, column=COL_SOC_END).value),
            kwh_supplied=_as_float(sheet.cell(row=row, column=COL_KWH_SUPPLIED).value),
            location=str(sheet.cell(row=row, column=COL_LOCATION).value or ""),
            notes=str(sheet.cell(row=row, column=COL_NOTES).value or ""),
        ))
    return events, str(registration)


def _as_float(value) -> float | None:
    """Convert a spreadsheet cell to float, None when empty/non-numeric."""
    if value is None or isinstance(value, str) and not value.strip():
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_km(miles) -> float | None:
    """Convert an odometer cell (miles) to km, None when empty."""
    value = _as_float(miles)
    return None if value is None else round(value * MILES_TO_KM, 1)


def build_driving_periods(events: list[ChargeEvent]) -> list[DrivingPeriod]:
    """Derive the inter-charge driving periods from consecutive events.

    Interface: takes the parsed ChargeEvents and returns the list of
    DrivingPeriod for each consecutive pair where the first event has a
    charge-end % and the second a charge-start %, the second charge starts
    no earlier than the first ended, and the battery fell between them
    (a rise would mean unrecorded charging mid-period).  Distance is the
    odometer difference when both readings exist.
    """
    periods: list[DrivingPeriod] = []
    for first, second in zip(events, events[1:]):
        if first.soc_end is None or second.soc_start is None:
            continue
        start_dt, end_dt = first.end_datetime(), second.start_datetime()
        if end_dt < start_dt or second.soc_start >= first.soc_end:
            continue
        distance = None
        if first.odometer_km is not None and second.odometer_km is not None:
            delta = second.odometer_km - first.odometer_km
            distance = round(delta, 1) if delta >= 0 else None
        end_date = end_dt.date()
        days = [start_dt.date() + timedelta(days=n)
                for n in range((end_date - start_dt.date()).days + 1)]
        periods.append(DrivingPeriod(start_dt, end_dt, first.soc_end,
                                     second.soc_start, distance, days))
    return periods


def period_weights(period: DrivingPeriod,
                   daily_distances: dict[date, float]) -> dict[date, float] | None:
    """Per-day apportionment weights for one driving period.

    Interface: takes a DrivingPeriod and the API's per-day distance map.
    Returns a {date: weight} map ONLY when every day of the period has an
    API distance and the period's odometer distance is known; the API
    values are then rescaled so that they sum to the period's odometer
    distance (keeping the API's day-to-day shape while staying consistent
    with the measured charge-period totals).  Returns None otherwise, so
    apportion() falls back to an even split (which is exactly what the
    derived daily rows store for partially-covered periods).
    """
    if period.distance_km is None:
        return None
    if not all(d in daily_distances for d in period.days):
        return None
    api_sum = sum(daily_distances[d] for d in period.days)
    if api_sum <= 0:
        return None
    scale = period.distance_km / api_sum
    return {d: daily_distances[d] * scale for d in period.days}


def apportion(total: float, days: list[date],
              weights: dict[date, float] | None) -> dict[date, float]:
    """Spread a period total over calendar days by weights (or evenly).

    Interface: takes the total (e.g. a % drop or a distance), the list of
    days and an optional {date: weight} map (e.g. API per-day distances).
    Weights are used only when every day has one and their sum is > 0;
    otherwise the total is split evenly.  Returns {date: share} summing
    to ``total`` (the last day absorbs rounding).
    """
    if not days:
        return {}
    if weights and all(d in weights for d in days):
        weight_sum = sum(weights[d] for d in days)
        if weight_sum > 0:
            shares = {d: total * weights[d] / weight_sum for d in days}
            shares[days[-1]] += total - sum(shares.values())
            return shares
    even = total / len(days)
    shares = {d: even for d in days}
    shares[days[-1]] += total - sum(shares.values())
    return shares


def _sample_row(ts: datetime, sample_type: str, pct: float, source: str,
                registration: str, odometer_km: float | None = None,
                capacity_kwh: float | None = None) -> dict:
    """Build one samples.csv row for a manual or derived battery sample.

    Interface: takes the sample datetime, type ("pre_charge"/
    "post_charge"), battery %, provenance tag, registration string and
    optional odometer (km) / capacity (kWh); returns a dict keyed by
    SAMPLE_COLUMNS.
    """
    return {
        "timestamp_local": ts.isoformat(timespec="minutes"),
        "date_local": ts.date().isoformat(),
        "sample_type": sample_type,
        "vehicle_name": "PV5",
        "vin": registration,
        "battery_pct": pct,
        "odometer_km": "" if odometer_km is None else odometer_km,
        "odometer_unit": "km",
        "ev_capacity_raw": "",
        "capacity_kwh_used": "" if capacity_kwh is None else round(capacity_kwh, 3),
        "capacity_source": "",
        "vehicle_updated_at": "",
        "source": source,
    }


def synthesise_samples(
    events: list[ChargeEvent],
    periods: list[DrivingPeriod],
    daily_distances: dict[date, float],
    registration: str,
) -> list[dict]:
    """Create all manual and manual_derived samples.csv rows.

    Interface: takes the parsed events, the derived periods, the API's
    per-day distance map ({date: km}) and the registration string; returns
    sample rows (unsorted).  Within periods fully covered by the API the
    per-day distances are rescaled to the period's odometer total
    (period_weights) so the apportioned % drop stays consistent with the
    measured charge-period figures; other periods split evenly, matching
    the derived daily rows.  Anchors: a post_charge row per charge end and
    a pre_charge row per charge start (source="manual").  Derived: per
    period, day-boundary levels from the apportioned % drop
    (source="manual_derived"); see module docstring.
    """
    rows: list[dict] = []
    for event in events:
        if event.soc_end is not None:
            rows.append(_sample_row(event.end_datetime(), "post_charge",
                                    event.soc_end, "manual", registration,
                                    odometer_km=event.odometer_km))
        if event.soc_start is not None:
            rows.append(_sample_row(event.start_datetime(), "pre_charge",
                                    event.soc_start, "manual", registration))
    for period in periods:
        weights = period_weights(period, daily_distances)
        drops = apportion(period.pct_drop, period.days, weights)
        running = period.soc_from
        first_day, last_day = period.days[0], period.days[-1]
        for day in period.days:
            if day != first_day:
                rows.append(_sample_row(datetime.combine(day, DERIVED_DAY_START),
                                        "post_charge", round(running, 2),
                                        "manual_derived", registration))
            running -= drops[day]
            if day != last_day:
                rows.append(_sample_row(datetime.combine(day, DERIVED_DAY_END),
                                        "pre_charge", round(running, 2),
                                        "manual_derived", registration))
    return rows


def synthesise_daily(
    periods: list[DrivingPeriod],
    existing_dates: set[date],
    daily_distances: dict[date, float],
    collected_at: str,
) -> tuple[list[dict], dict[date, float]]:
    """Create manual_derived daily.csv rows for days the API never covers.

    Interface: takes the periods, the set of dates already present in
    daily.csv, the API per-day distances and a collection timestamp;
    returns (rows, overlay): ``rows`` are daily rows whose dates are
    absent from ``existing_dates`` and whose period distance is known,
    with the distance apportioned over the period days using the same
    weights as the % drops (evenly outside the API window) and kwh_used
    left empty (no manual consumption figures exist); ``overlay`` maps
    every period day to its apportioned distance share so merge_daily can
    stamp it into the distance_range_km column of all rows, keeping
    method 1's distance basis consistent with the apportioned % drops
    (raw distance_km of API-covered days stays untouched).
    """
    rows: list[dict] = []
    overlay: dict[date, float] = {}
    seen: set[date] = set()
    for period in periods:
        if period.distance_km is None:
            continue
        weights = period_weights(period, daily_distances)
        shares = apportion(period.distance_km, period.days, weights)
        overlay.update({day: round(share, 2) for day, share in shares.items()})
        for day in period.days:
            if day in existing_dates or day in seen:
                continue
            seen.add(day)
            rows.append({
                "date": day.isoformat(),
                "distance_km": round(shares[day], 1),
                "kwh_used": "",
                "engine_kwh": "",
                "climate_kwh": "",
                "electronics_kwh": "",
                "battery_care_kwh": "",
                "regen_kwh": "",
                "collected_at": collected_at,
                "source": "manual_derived",
                "distance_range_km": round(shares[day], 2),
            })
    return rows, overlay


def write_reference_csv(path: Path, events: list[ChargeEvent],
                        periods: list[DrivingPeriod]) -> None:
    """Write the normalised conversion-input record for future reference.

    Interface: takes the destination Path (inside the one_off folder), the
    parsed events and periods; writes one CSV with an "event"/"period"
    section listing every charge event (datetimes, %, odometer km,
    supplied kWh, location, notes) and every driving period (bounds,
    distance, % drop, days).  Overwrites any previous file.
    """
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["record", "row", "date", "start_datetime",
                         "end_datetime", "soc_start_pct", "soc_end_pct",
                         "odometer_miles", "odometer_km", "kwh_supplied",
                         "location", "distance_km", "pct_drop", "days",
                         "notes"])
        for event in events:
            writer.writerow([
                "event", event.row, event.date.isoformat(),
                event.start_datetime().isoformat(timespec="minutes"),
                event.end_datetime().isoformat(timespec="minutes"),
                event.soc_start, event.soc_end,
                None if event.odometer_km is None
                else round(event.odometer_km / MILES_TO_KM, 1),
                event.odometer_km,
                event.kwh_supplied, event.location, "", "", "", event.notes,
            ])
        for period in periods:
            writer.writerow([
                "period", "", "",
                period.start.isoformat(timespec="minutes"),
                period.end.isoformat(timespec="minutes"),
                period.soc_from, period.soc_to, "", "", "",
                period.distance_km, round(period.pct_drop, 2),
                period.days[0].isoformat() + ".." + period.days[-1].isoformat(),
                "",
            ])


def merge_samples(path: Path, new_rows: list[dict]) -> int:
    """Replace all manual samples in samples.csv with the regenerated set.

    Interface: takes the samples.csv Path and the new manual/derived rows;
    keeps every row whose source is not "manual"/"manual_derived"
    (normalising rows without a source column to "collect"), appends the
    new rows, sorts by timestamp and rewrites the file.  Returns the
    number of manual rows written.  Idempotent.
    """
    kept: list[dict] = []
    if path.exists() and path.stat().st_size > 0:
        with open(path, "r", newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                if not row.get("source"):
                    row["source"] = "collect"  # file predates the column
                if row.get("source") not in ("manual", "manual_derived"):
                    kept.append(row)
    merged = kept + new_rows
    merged.sort(key=lambda row: row["timestamp_local"])
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=SAMPLE_COLUMNS, restval="")
        writer.writeheader()
        writer.writerows(merged)
    return len(new_rows)


def merge_daily(path: Path, new_rows: list[dict],
                distance_overlay: dict[date, float] | None = None) -> int:
    """Add manual_derived daily rows for dates daily.csv does not have.

    Interface: takes the daily.csv Path and the candidate rows; keeps all
    existing rows EXCEPT previous manual_derived ones (those are
    regenerated from the spreadsheet every run, so re-running the import
    also refreshes their apportioned distances; rows with source "api" /
    "collect" are never touched), adds only candidate rows whose date is
    then absent, sorts by date and rewrites the file.  Returns the number
    of manual_derived rows in the result.  Idempotent.
    """
    kept: list[dict] = []
    existing_dates: set[str] = set()
    if path.exists() and path.stat().st_size > 0:
        with open(path, "r", newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                if not row.get("source"):
                    row["source"] = "api"  # file predates the column
                if row["source"] == "manual_derived":
                    continue  # regenerated below
                kept.append(row)
                existing_dates.add(row["date"])
    added = [row for row in new_rows if row["date"] not in existing_dates]
    merged = kept + added
    merged.sort(key=lambda row: row["date"])
    if distance_overlay:
        by_date = {row["date"]: row for row in merged}
        for day, share in distance_overlay.items():
            row = by_date.get(day.isoformat())
            if row is not None:
                row["distance_range_km"] = share
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=DAILY_COLUMNS, restval="")
        writer.writeheader()
        writer.writerows(merged)
    return len(added)


def main(argv: list[str] | None = None) -> int:
    """Entry point: parse the spreadsheet and merge it into the CSV data.

    Interface: takes optional argv; supports --xlsx, --config and
    --dry-run (report counts without writing data files; the reference
    CSV is still written).  Returns 0 on success, 1 on failure.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--xlsx", default=str(DEFAULT_XLSX),
                        help="path to the manual charging log spreadsheet")
    parser.add_argument("--config", help="path to config.toml")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would be merged without writing")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    try:
        config = kia_common.load_config(args.config)
        paths = kia_common.paths_from_config(config)
        events, registration = parse_charge_events(Path(args.xlsx))
        periods = build_driving_periods(events)
        logger.info("Parsed %d charge events and %d driving periods "
                    "(registration %s)", len(events), len(periods),
                    registration or "unknown")

        write_reference_csv(HERE / "manual_charges_parsed.csv", events, periods)
        logger.info("Reference CSV written to %s",
                    HERE / "manual_charges_parsed.csv")

        # Per-day distances used as apportionment weights: the API's own
        # per-day figures, extended with explicit 0.0 for calendar days
        # inside the API window that have no row (rest days), so that
        # period_weights sees every covered day and can rescale the API
        # shape onto each period's measured odometer total.  Days before
        # the API window keep no weight -> even splits, matching the
        # manual_derived daily rows created below for exactly those days.
        import analyze
        daily = analyze.load_daily(paths["daily"])
        api_rows = {day: info for day, info in daily.items()
                    if info.get("source") != "manual_derived"}
        daily_distances = {day: info["distance_km"]
                           for day, info in api_rows.items()
                           if info["distance_km"]}
        if api_rows:
            window_day = min(api_rows)
            while window_day <= max(api_rows):
                daily_distances.setdefault(window_day, 0.0)
                window_day += timedelta(days=1)
        daily_rows, distance_overlay = synthesise_daily(
            periods, set(daily_distances), daily_distances,
            datetime.now().astimezone().isoformat(timespec="seconds"))

        sample_rows = synthesise_samples(events, periods, daily_distances,
                                         registration)
        if args.dry_run:
            logger.info("[dry-run] would write %d sample rows and "
                        "%d daily rows", len(sample_rows), len(daily_rows))
            return 0
        n_samples = merge_samples(paths["samples"], sample_rows)
        n_daily = merge_daily(paths["daily"], daily_rows, distance_overlay)
        logger.info("Merged %d manual sample rows into %s and %d derived "
                    "daily rows into %s", n_samples, paths["samples"],
                    n_daily, paths["daily"])
        return 0
    except Exception:  # noqa: BLE001 - top-level guard
        logger.exception("Spreadsheet import failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
