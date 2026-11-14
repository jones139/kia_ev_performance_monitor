#!/usr/bin/env python3
"""Analyse collected Kia EV data and graph the estimated vehicle range.

Reads the CSV files written by collect.py (data/samples.csv and
data/daily.csv) and produces:

  * a printed summary: latest spot estimates, rolling averages, planning
    ranges, period totals, an "implied battery capacity" diagnostic, and an
    energy-accounting check comparing the API's daily kWh with the battery
    energy implied by the battery-% drop (a ratio below 1 means the API
    under-reports consumption and method 2 reads high)
  * a PNG graph with two panels, one per estimation method:

      Method 1 (battery % change):  each day, the drop in battery % between
          the morning (post_charge) and evening (pre_charge) samples is
          compared with the distance driven that day:
              full range (km) = distance / pct_drop * 100

      Method 2 (kWh used):  the vehicle-reported energy use for the day is
          compared with the distance driven, and scaled by the usable
          battery capacity (from config.toml / the API, see kia_common):
              full range (km) = distance / (kwh_used * correction) * capacity_kwh
          ``correction`` is [analysis].api_energy_correction ("auto" by
          default): the car's energy meter reports motor-side energy and
          omits drivetrain losses, so its kWh explain only ~79% of the
          battery energy implied by the % drop (see energy_accounting).

    Each panel shows the daily spot estimate, a rolling average over the
    previous N days (default 7), and a "planning range" line: the rolling
    average scaled to the usable state-of-charge window, i.e. assuming the
    battery is only run from plan_max_pct down to plan_min_pct
    (default 100% -> 20%, a factor of 0.8).

Days are only used for an estimate when they pass sanity thresholds from
config.toml [analysis] (minimum distance, minimum % drop, minimum kWh),
which filters out noise from days with little or no driving, and from days
when the car was charged during the day.

Command line interface:
    analyze.py [--config FILE] [--data-dir DIR] [--samples FILE]
               [--daily FILE] [--output FILE.png] [--window DAYS]
               [--capacity-kwh KWH] [--units {km,miles}] [--show]
"""

from __future__ import annotations

import argparse
import csv
import logging
import statistics
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import kia_common
from kia_common import logger

# Rolling-average / planning-line colours (shared by both panels).
COLOR_SPOT = "#9aa5b1"      # daily spot estimates (thin, grey-blue)
COLOR_ROLLING = "#1f77b4"   # rolling average (solid blue)
COLOR_PLANNING = "#e67e22"  # planning range (dashed orange)

# A charge ending more than this long after the morning sample counts as a
# mid-day top-up.  Duplicate records of the SAME overnight charge (the
# collector and the spreadsheet import both anchor it) differ by minutes,
# so they must not be mistaken for a second charge.
MIDDAY_CHARGE_GAP = timedelta(hours=2)


@dataclass
class DayRecord:
    """All collected data for one calendar day, ready for range estimation.

    Fields: ``date`` (datetime.date); ``pct_start`` / ``pct_end`` are the
    battery % from the first post_charge and last pre_charge sample of that
    day (None when missing); ``pair_measured`` is True when both bounding
    samples are real anchors (source not "manual_derived"), i.e. the %
    drop was measured rather than apportioned from a charge period;
    ``charged_during_day`` is True when the car charged again between the
    morning and evening samples, which makes that day's % drop an
    understatement of consumption (the top-up refilled part of it);
    ``distance_km`` / ``kwh_used`` are the API's per-day driving stats
    (None when the day is absent from daily.csv); ``distance_range_km``
    is the spreadsheet import's period-consistent distance share for the
    day (None when absent), used as method 1's distance basis so that
    apportioned % drops pair with matching distances; ``capacity_kwh`` is
    the usable battery capacity in effect that day (forward-filled from
    samples.csv, else the config value).
    """

    date: date
    pct_start: float | None = None
    pct_end: float | None = None
    pair_measured: bool = False
    charged_during_day: bool = False
    distance_km: float | None = None
    distance_range_km: float | None = None
    kwh_used: float | None = None
    capacity_kwh: float | None = None


def _to_float(value) -> float | None:
    """Convert a CSV cell to float, returning None for empty/invalid data."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def load_samples(path: Path) -> list[dict]:
    """Load samples.csv into a list of parsed sample dicts.

    Interface: takes the Path to samples.csv (may not exist yet) and
    returns a list sorted by timestamp, each dict containing: ts
    (datetime), date (date), sample_type (str), battery_pct (float|None),
    capacity_kwh (float|None), vehicle_name (str) and source (str, the
    provenance tag written by collect.py / the spreadsheet import; ""
    when absent).  Malformed rows are skipped with a warning.
    """
    samples: list[dict] = []
    if not path.exists():
        return samples
    with open(path, "r", newline="", encoding="utf-8") as handle:
        for line_no, row in enumerate(csv.DictReader(handle), start=2):
            try:
                ts = datetime.fromisoformat(row["timestamp_local"])
                if ts.tzinfo is None:
                    # Rows written without an offset (e.g. the spreadsheet
                    # import's nominal local times) are read as local time.
                    ts = ts.astimezone()
                sample_date = date.fromisoformat(row["date_local"])
            except (KeyError, ValueError) as error:
                logger.warning("Skipping malformed row %d in %s (%s)",
                               line_no, path, error)
                continue
            samples.append({
                "ts": ts,
                "date": sample_date,
                "sample_type": row.get("sample_type", ""),
                "battery_pct": _to_float(row.get("battery_pct")),
                "capacity_kwh": _to_float(row.get("capacity_kwh_used")),
                "vehicle_name": row.get("vehicle_name", ""),
                "source": row.get("source", "") or "",
            })
    samples.sort(key=lambda sample: sample["ts"])
    return samples


def load_daily(path: Path) -> dict[date, dict]:
    """Load daily.csv into a dict keyed by calendar date.

    Interface: takes the Path to daily.csv (may not exist yet) and returns
    {date: {"distance_km": float|None, "kwh_used": float|None,
    "source": str, "distance_range_km": float|None}} where
    distance_range_km is the period-consistent distance share written by
    the spreadsheet import (None when absent).  Rows with an unparseable
    date are skipped with a warning.
    """
    daily: dict[date, dict] = {}
    if not path.exists():
        return daily
    with open(path, "r", newline="", encoding="utf-8") as handle:
        for line_no, row in enumerate(csv.DictReader(handle), start=2):
            try:
                day = date.fromisoformat(row["date"])
            except (KeyError, ValueError) as error:
                logger.warning("Skipping malformed row %d in %s (%s)",
                               line_no, path, error)
                continue
            daily[day] = {
                "distance_km": _to_float(row.get("distance_km")),
                "kwh_used": _to_float(row.get("kwh_used")),
                "source": row.get("source", "") or "api",
                "distance_range_km": _to_float(row.get("distance_range_km")),
            }
    return daily


def _capacity_on(day: date, capacity_points: list[tuple[date, float]],
                 default: float) -> float:
    """Return the battery capacity in effect on a given date.

    Interface: takes the date, a sorted list of (sample_date, capacity_kwh)
    points taken from samples.csv, and a default capacity.  Returns the
    most recent recorded capacity at or before ``day``, or ``default`` when
    no sample is that old (forward fill).
    """
    value = default
    for point_date, capacity in capacity_points:
        if point_date <= day:
            value = capacity
        else:
            break
    return value


def build_day_records(
    samples: list[dict],
    daily: dict[date, dict],
    config: dict,
    default_capacity: float,
) -> list[DayRecord]:
    """Combine samples and daily stats into one DayRecord per calendar day.

    Interface: takes the parsed samples (load_samples), parsed daily stats
    (load_daily), the config dict and the fallback capacity in kWh.
    Returns DayRecords sorted by date covering every date present in
    either source.

    Pairing rules: ``pct_start`` is the battery % of the FIRST post_charge
    sample of the day (just after the overnight charge finished) and
    ``pct_end`` the battery % of the LAST pre_charge sample taken AFTER
    that morning sample (just before the next charge starts); the time
    rule keeps days with a mid-day en-route charge from pairing the
    post-charge level with the pre-charge level of the same charge.
    Per-day capacity is forward-filled from the capacities recorded by
    collect.py, falling back to ``default_capacity``.
    """
    capacity_points = [
        (sample["date"], sample["capacity_kwh"])
        for sample in samples
        if sample["capacity_kwh"] is not None
    ]
    by_date: dict[date, list[dict]] = {}
    for sample in samples:
        by_date.setdefault(sample["date"], []).append(sample)

    records: list[DayRecord] = []
    for day in sorted(set(daily) | set(by_date)):
        day_samples = by_date.get(day, [])
        morning = [s for s in day_samples
                   if s["sample_type"] == "post_charge" and s["battery_pct"] is not None]
        pct_start = morning[0]["battery_pct"] if morning else None
        morning_ts = morning[0]["ts"] if morning else None
        evening = [s for s in day_samples
                   if s["sample_type"] == "pre_charge" and s["battery_pct"] is not None
                   and (morning_ts is None or s["ts"] > morning_ts)]
        stats = daily.get(day, {})
        pair_measured = bool(morning and evening) and all(
            s["source"] != "manual_derived" for s in (morning[0], evening[-1])
        )
        # A second charge ENDING well after the morning sample means the
        # car was topped up during the day: the morning/evening % pair no
        # longer measures that day's consumption.
        charged_during_day = bool(morning_ts) and any(
            sample["sample_type"] == "post_charge"
            and sample["ts"] - morning_ts > MIDDAY_CHARGE_GAP
            for sample in day_samples
        )
        records.append(DayRecord(
            date=day,
            pct_start=pct_start,
            pct_end=evening[-1]["battery_pct"] if evening else None,
            pair_measured=pair_measured,
            charged_during_day=charged_during_day,
            distance_km=stats.get("distance_km"),
            distance_range_km=stats.get("distance_range_km"),
            kwh_used=stats.get("kwh_used"),
            capacity_kwh=_capacity_on(day, capacity_points, default_capacity),
        ))
    return records


def estimate_series(
    records: list[DayRecord], config: dict, kwh_correction: float = 1.0
) -> tuple[dict[str, list[tuple[date, float]]], dict[str, int]]:
    """Compute the daily range estimates for both methods.

    Interface: takes the DayRecords (build_day_records), the config dict
    and ``kwh_correction``, the factor applied to the API's reported kWh
    before method 2 (1.0 = raw API values; see resolve_kwh_correction).
    Returns (series, skipped) where ``series`` maps names to sorted
    lists of (date, value):
        "range_pct"        - full-charge range (km) from battery-% change
        "range_kwh"        - full-charge range (km) from kWh used x capacity
        "implied_capacity" - kWh implied by kWh-used vs %-drop (diagnostic)
        "distance" / "kwh" - raw per-day distance (km) / energy (kWh)
    and ``skipped`` counts days rejected by each sanity rule, for the
    summary.  Thresholds come from config [analysis]: min_pct_delta,
    min_distance_km, min_kwh_used, capacity_check_min_pct_delta, plus a
    physical plausibility band (min_range_km / max_range_km) applied to
    the finished estimates: values outside it are discarded as glitches
    (typically days where the API's per-day distance attribution and the
    manual charging log's odometer deltas disagree about when km were
    driven).
    """
    analysis = config["analysis"]
    min_pct = float(analysis["min_pct_delta"])
    min_km = float(analysis["min_distance_km"])
    min_kwh = float(analysis["min_kwh_used"])
    min_pct_capacity = float(analysis["capacity_check_min_pct_delta"])
    band_lo = float(analysis["min_range_km"])
    band_hi = float(analysis["max_range_km"])

    series: dict[str, list[tuple[date, float]]] = {
        "range_pct": [], "range_kwh": [], "implied_capacity": [],
        "distance": [], "kwh": [],
    }
    skipped = {"charged_during_day": 0, "pct_delta_too_small": 0,
               "usage_too_small": 0, "missing_sample_pair": 0,
               "implausible_estimate": 0}

    for record in records:
        if record.distance_km is not None:
            series["distance"].append((record.date, record.distance_km))
        if record.kwh_used is not None:
            series["kwh"].append((record.date, record.kwh_used))

        pct_delta = None
        if record.pct_start is not None and record.pct_end is not None:
            pct_delta = record.pct_start - record.pct_end
            if record.charged_during_day:
                # A mid-day top-up refilled part of the drop, so the %-based
                # estimate would read far too high; method 2 is unaffected.
                skipped["charged_during_day"] += 1
                pct_delta = None
        elif record.distance_km is not None:
            # A driving day with no morning/evening sample pair: the
            # %-based method cannot be applied.
            skipped["missing_sample_pair"] += 1

        distance_ok = (record.distance_km is not None
                       and record.distance_km >= min_km)
        # Method 1's distance basis: the period-consistent share when the
        # day came from the spreadsheet import (its % drop was apportioned
        # with the same weights), otherwise the API's per-day distance.
        km_pct = (record.distance_range_km
                  if record.distance_range_km is not None
                  else record.distance_km)
        distance_ok_pct = km_pct is not None and km_pct >= min_km

        # Method 1: battery-% change vs distance driven.
        accepted_pct = accepted_kwh = False
        if pct_delta is not None and distance_ok_pct:
            if pct_delta < 0:
                skipped["charged_during_day"] += 1
            elif pct_delta < min_pct:
                skipped["pct_delta_too_small"] += 1
            else:
                value = 100.0 * km_pct / pct_delta
                if band_lo <= value <= band_hi:
                    series["range_pct"].append((record.date, value))
                    accepted_pct = True
                else:
                    skipped["implausible_estimate"] += 1

        # Method 2: reported kWh used x battery capacity.  The API's kWh are
        # scaled by kwh_correction first (the car's energy meter omits
        # drivetrain losses; see resolve_kwh_correction).
        kwh = (record.kwh_used * kwh_correction
               if record.kwh_used is not None else None)
        if (distance_ok and kwh is not None and kwh >= min_kwh
                and record.capacity_kwh):
            value = record.capacity_kwh * record.distance_km / kwh
            if band_lo <= value <= band_hi:
                series["range_kwh"].append((record.date, value))
                accepted_kwh = True
            else:
                skipped["implausible_estimate"] += 1
        elif distance_ok and kwh is not None:
            skipped["usage_too_small"] += 1

        # Diagnostic: usable capacity implied by the day's own data, only
        # on days where both estimates passed every sanity check AND the
        # % drop was measured between two anchored samples (apportioned
        # drops do not align day-by-day with the API's kWh attribution).
        if (accepted_pct and accepted_kwh and record.pair_measured
                and pct_delta >= min_pct_capacity):
            series["implied_capacity"].append(
                (record.date, kwh / (pct_delta / 100.0))
            )

    return series, skipped


def energy_accounting(records: list[DayRecord]) -> dict:
    """Compare API-reported energy use with battery energy from the % drop.

    Interface: takes the DayRecords.  Over every day that has BOTH a
    positive battery-% drop and API kWh data, sums the battery energy
    implied by the drop (pct_delta / 100 x that day's capacity) and the
    energy the API reported.  Returns a dict with:
        "days"        - number of days included
        "soc_kwh"     - battery energy implied by the % drops (kWh)
        "api_kwh"     - energy reported by the API (kWh)
        "ratio"       - api_kwh / soc_kwh, or None when unavailable
        "gap_kwh"     - soc_kwh - api_kwh (unaccounted energy, kWh)
        "gap_per_day" - gap_kwh / days, or None when there are no days
        "distance_km" - distance driven over the included days (km)
        "gap_per_100km" - gap_kwh per 100 km, or None when no distance
    Days are excluded when the car charged during the day (the resulting
    % drop understates consumption, which would bias the ratio upwards),
    when the drop is zero/negative, and when either quantity is missing.
    Individual days can be misleading when the % drop was apportioned
    across a charge period, but sums over consecutive days reproduce the
    measured period totals, so the aggregate ratio is meaningful.  A ratio
    below 1 means the API's daily stats under-report the energy that
    actually left the battery, which makes method 2's range estimate
    optimistic.  Comparing gap_per_day with gap_per_100km shows whether
    the missing energy is time-based (parked/standby) or distance-based
    (used while driving): measured data show a stable per-100 km gap and
    no per-day component, i.e. a metering shortfall proportional to
    traction energy (drivetrain losses), not a parked drain.
    """
    days = [
        record for record in records
        if (record.kwh_used is not None and record.capacity_kwh
            and not record.charged_during_day
            and record.pct_start is not None and record.pct_end is not None
            and record.pct_start > record.pct_end)
    ]
    soc_kwh = sum((record.pct_start - record.pct_end) / 100.0
                  * record.capacity_kwh for record in days)
    api_kwh = sum(record.kwh_used for record in days)
    distance_km = sum(record.distance_km or 0.0 for record in days)
    ratio = api_kwh / soc_kwh if soc_kwh > 0 else None
    gap = soc_kwh - api_kwh
    return {
        "days": len(days),
        "soc_kwh": soc_kwh,
        "api_kwh": api_kwh,
        "ratio": ratio,
        "gap_kwh": gap,
        "gap_per_day": gap / len(days) if days else None,
        "distance_km": distance_km,
        "gap_per_100km": gap / distance_km * 100.0 if distance_km else None,
    }


def resolve_kwh_correction(
    config: dict, records: list[DayRecord]
) -> tuple[float, str]:
    """Resolve the factor applied to the API's kWh inside method 2.

    Interface: takes the config dict and the DayRecords.  Returns
    (factor, note) where ``factor`` is >= 1.0 and ``note`` is a short
    human-readable explanation for the summary/page.

    [analysis].api_energy_correction may be "auto" (default) or a number.
    "auto" derives the factor from energy_accounting(): the API's daily
    kWh are consistently a fraction of the battery energy implied by the
    % drop because the car meters motor-side energy and omits drivetrain
    losses, so factor = 1 / ratio.  It needs at least 10 qualifying days
    and a plausible ratio (0.5-0.98); otherwise 1.0 is used and the note
    says so.  A configured number is used as-is (clamped to 1.0-2.0).
    The factor never shrinks range: values below 1.0 are clamped to 1.0.
    """
    setting = config["analysis"].get("api_energy_correction", "auto")
    if isinstance(setting, str):
        text = setting.strip().lower()
        if text in ("", "auto"):
            energy = energy_accounting(records)
            ratio = energy["ratio"]
            if energy["days"] >= 10 and ratio and 0.5 <= ratio < 0.98:
                return min(1.0 / ratio, 2.0), (
                    f"auto: API reports {ratio:.0%} of battery energy over "
                    f"{energy['days']} day(s)"
                )
            return 1.0, (
                f"auto: only {energy['days']} usable day(s); using 1.00"
            )
        try:
            setting = float(text)
        except ValueError:
            logger.warning(
                "Invalid [analysis].api_energy_correction %r; using 1.0",
                text,
            )
            return 1.0, "invalid setting; using 1.00"
    try:
        factor = float(setting)
    except (TypeError, ValueError):
        logger.warning(
            "Invalid [analysis].api_energy_correction %r; using 1.0", setting
        )
        return 1.0, "invalid setting; using 1.00"
    clamped = min(max(factor, 1.0), 2.0)
    if clamped != factor:
        logger.warning(
            "api_energy_correction %.2f clamped to %.2f", factor, clamped
        )
    return clamped, "configured in config.toml"


def rolling_average(
    series: list[tuple[date, float]], window_days: int
) -> list[tuple[date, float]]:
    """Compute a rolling mean of a dated value series.

    Interface: takes a sorted list of (date, value) and the window size in
    calendar days.  Returns a sorted list of (date, mean) for EVERY
    calendar date between the first and last input date where the window
    [date - window_days + 1, date] contains at least one value, so the
    plotted line stays continuous across days without estimates.
    """
    if not series:
        return []
    first, last = series[0][0], series[-1][0]
    span = timedelta(days=window_days - 1)
    result: list[tuple[date, float]] = []
    day = first
    while day <= last:
        window = [value for value_date, value in series
                  if day - span <= value_date <= day]
        if window:
            result.append((day, sum(window) / len(window)))
        day += timedelta(days=1)
    return result


def scale_series(
    series: list[tuple[date, float]], factor: float
) -> list[tuple[date, float]]:
    """Multiply every value in a dated series by a constant factor.

    Interface: takes a sorted list of (date, value) and the factor;
    returns a new list of (date, value * factor).  Used to derive the
    planning-range line from the rolling average.
    """
    return [(day, value * factor) for day, value in series]


def output_units(config: dict, override: str | None = None) -> tuple[str, float]:
    """Resolve the reporting units for the summary and graph.

    Interface: takes the config dict and an optional CLI override string;
    returns (unit_label, km_to_unit_factor) where unit_label is "km" or
    "mi" and the factor converts internally-stored km values into the
    reporting unit ("miles"/"mi" -> 1/1.609344).  Unknown values log a
    warning and fall back to km.  All stored data, sanity thresholds and
    the plausibility band remain km regardless of this setting.
    """
    raw = str(override or config["analysis"].get("output_units", "km"))
    raw = raw.strip().lower()
    if raw in ("miles", "mile", "mi"):
        return "mi", 1.0 / kia_common.MILES_TO_KM
    if raw not in ("km", "kilometres", "kilometers"):
        logger.warning("Unknown output units %r; reporting in km", raw)
    return "km", 1.0


def make_graph(
    output_path: Path,
    panels: list[tuple[str, list, list, list]],
    window_days: int,
    plan_max_pct: float,
    plan_min_pct: float,
    vehicle_name: str,
    show: bool = False,
    unit_label: str = "km",
) -> None:
    """Render the two-panel range graph and save it as a PNG file.

    Interface: takes the output Path, a list of panels where each panel is
    (title, spot_series, rolling_series, planning_series) of (date, value)
    tuples, the rolling window size and planning SOC limits (for legend
    labels), the vehicle name (figure title), ``show`` to also display
    the figure interactively (requires a desktop session) and
    ``unit_label`` ("km"/"mi") for the y-axis labels; the series values
    must already be in that unit.  Uses the non-interactive Agg backend
    unless ``show`` is set, so it is safe to run headless on a server.
    """
    import matplotlib

    if not show:
        matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(
        2, 1, figsize=(12, 8.5), sharex=True, constrained_layout=True
    )
    planning_label = (
        f"Planning range ({plan_max_pct:g}%\u2192{plan_min_pct:g}%)"
    )
    for ax, (title, spot, rolling, planning) in zip(axes, panels):
        if spot or rolling or planning:
            if spot:
                ax.plot([d for d, _ in spot], [v for _, v in spot],
                        color=COLOR_SPOT, marker=".", markersize=5,
                        linewidth=0.8, label="Daily estimate")
            if rolling:
                ax.plot([d for d, _ in rolling], [v for _, v in rolling],
                        color=COLOR_ROLLING, linewidth=2.2,
                        label=f"{window_days}-day rolling average")
            if planning:
                ax.plot([d for d, _ in planning], [v for _, v in planning],
                        color=COLOR_PLANNING, linewidth=2.2, linestyle="--",
                        label=planning_label)
            ax.legend(loc="best", fontsize=9)
        else:
            # Panel with no estimates yet (typical for method 1 until the
            # first morning+evening sample pair exists): annotate instead
            # of showing a meaningless empty axis.
            ax.text(0.5, 0.5, "No usable estimates yet",
                    transform=ax.transAxes, ha="center", va="center",
                    fontsize=11, color="#666666")
            ax.set_yticks([])
        ax.set_title(title, fontsize=11)
        ax.set_ylabel(f"Estimated range ({unit_label})")
        ax.grid(True, alpha=0.3)
    axes[-1].set_xlabel("Date")
    fig.suptitle(
        f"{vehicle_name}: estimated range on a full charge (100%\u21920%)",
        fontsize=13,
    )
    fig.autofmt_xdate()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, dpi=150)
    logger.info("Graph saved to %s", output_path)
    if show:
        plt.show()
    plt.close(fig)


def print_summary(
    records: list[DayRecord],
    series: dict[str, list[tuple[date, float]]],
    skipped: dict[str, int],
    rolling_pct: list[tuple[date, float]],
    rolling_kwh: list[tuple[date, float]],
    window_days: int,
    plan_factor: float,
    unit_label: str = "km",
    unit_factor: float = 1.0,
    kwh_correction: float = 1.0,
    correction_note: str = "",
) -> None:
    """Print a text summary of the range analysis to stdout.

    Interface: takes the day records, the estimate series (estimate_series),
    the skipped-day counters, the two rolling-average series, the planning
    factor and the reporting units (``unit_label`` plus ``unit_factor``
    converting the internally-stored km values).  ``kwh_correction`` and
    ``correction_note`` describe the factor already applied to method 2's
    kWh (resolve_kwh_correction).  Range series passed in must already be
    in the reporting unit; distances taken from records are converted
    here.  Prints: data period, the method-2 correction, per-method latest
    spot value / rolling average / planning range, period and
    recent-window totals, average efficiency, the implied-capacity
    diagnostic and rejected-day counts.  Returns None.
    """
    driving_days = [r for r in records if r.distance_km is not None]
    line = "-" * 62
    print(line)
    print("Kia EV range analysis")
    print(line)
    if driving_days:
        print(f"Period: {driving_days[0].date} to {driving_days[-1].date} "
              f"({len(driving_days)} day(s) with API driving stats)")

    def describe(name: str,
                 spot: list[tuple[date, float]],
                 rolling: list[tuple[date, float]]) -> None:
        """Print latest spot / rolling / planning values for one method."""
        if not spot:
            print(f"{name}: no usable daily estimates yet")
            return
        spot_date, spot_value = spot[-1]
        print(f"{name}:")
        print(f"  latest daily estimate : {spot_value:7.0f} {unit_label}"
              f"  ({spot_date})")
        if rolling:
            roll_value = rolling[-1][0], rolling[-1][1]
            print(f"  {window_days}-day rolling avg : {roll_value[1]:7.0f} "
                  f"{unit_label}  ({roll_value[0]})")
            print(f"  planning range        : {roll_value[1] * plan_factor:7.0f} "
                  f"{unit_label}  (rolling avg x {plan_factor:.2f})")

    correction = f"x{kwh_correction:.2f}"
    if correction_note:
        correction += f" ({correction_note})"
    print(f"Method 2 energy correction: {correction}")
    describe("Method 1 (battery % change)", series["range_pct"], rolling_pct)
    describe("Method 2 (kWh used x capacity)", series["range_kwh"], rolling_kwh)

    # Period and recent-window totals.  Efficiency is computed only over
    # days that have BOTH distance and kWh (manual_derived rows carry
    # distance only), so the distance/kWh figures stay meaningful.
    both_days = [r for r in driving_days if r.kwh_used is not None]
    total_dist = sum(r.distance_km for r in driving_days) * unit_factor
    total_kwh = sum(r.kwh_used or 0.0 for r in driving_days)
    if both_days:
        eff_dist = sum(r.distance_km for r in both_days) * unit_factor
        eff_kwh = sum(r.kwh_used for r in both_days)
        print(f"Total: {total_dist:.0f} {unit_label} over "
              f"{len(driving_days)} day(s); "
              f"{eff_kwh:.1f} kWh over the {len(both_days)} day(s) with "
              f"energy data -> average {eff_dist / eff_kwh:.2f} "
              f"{unit_label}/kWh")
    cutoff = max((r.date for r in driving_days), default=None)
    if cutoff:
        recent = [r for r in both_days
                  if r.date > cutoff - timedelta(days=window_days)]
        recent_dist = sum(r.distance_km for r in recent) * unit_factor
        recent_kwh = sum(r.kwh_used or 0.0 for r in recent)
        if recent_kwh > 0:
            print(f"Last {window_days} days: {recent_dist:.0f} {unit_label} "
                  f"using {recent_kwh:.1f} kWh -> "
                  f"{recent_dist / recent_kwh:.2f} {unit_label}/kWh")

    # Implied battery capacity diagnostic.
    implied = [value for _, value in series["implied_capacity"]]
    if len(implied) >= 3:
        print(f"Implied usable battery capacity: median "
              f"{statistics.median(implied):.1f} kWh from {len(implied)} day(s) "
              f"(min {min(implied):.1f}, max {max(implied):.1f}) - compare "
              f"with [battery].capacity_kwh in config.toml")

    # Energy accounting: does the API's daily kWh explain the battery's
    # actual depletion?  A ratio below 1 means method 2 reads high.
    energy = energy_accounting(records)
    if energy["days"] >= 3 and energy["ratio"] is not None:
        print(f"Energy accounting: over {energy['days']} day(s) with both "
              f"battery-% and kWh data, the battery used "
              f"{energy['soc_kwh']:.1f} kWh but the API reported "
              f"{energy['api_kwh']:.1f} kWh ({energy['ratio']:.0%}); "
              f"{energy['gap_kwh']:.1f} kWh unaccounted "
              f"({energy['gap_per_day']:.1f} kWh/day, "
              f"{energy['gap_per_100km']:.1f} kWh/100km) -> raw method 2 "
              f"over-reads by ~{1 / energy['ratio'] - 1:.0%}")

    rejected = {reason: count for reason, count in skipped.items() if count}
    if rejected:
        print("Days excluded: " + ", ".join(
            f"{count} ({reason.replace('_', ' ')})"
            for reason, count in rejected.items()
        ))
    print(line)


def main(argv: list[str] | None = None) -> int:
    """Entry point: load collected data, analyse it and write the graph.

    Interface: takes optional argv (defaults to sys.argv[1:]); supports
    --config, --data-dir, --samples, --daily, --output, --window,
    --capacity-kwh and --show (see module docstring).  Returns 0 on
    success, 1 when there is no data or not enough data to estimate range.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", help="path to config.toml")
    parser.add_argument("--data-dir", help="override the data directory")
    parser.add_argument("--samples", help="override the samples CSV path")
    parser.add_argument("--daily", help="override the daily stats CSV path")
    parser.add_argument("--output", help="override the output PNG path")
    parser.add_argument("--window", type=int,
                        help="rolling-average window in days (default: config)")
    parser.add_argument("--capacity-kwh", type=float,
                        help="override the battery capacity used by method 2")
    parser.add_argument("--units", choices=("km", "miles"),
                        help="override [analysis].output_units for this run")
    parser.add_argument("--show", action="store_true",
                        help="also display the graph (needs a desktop session)")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )

    config = kia_common.load_config(args.config)
    paths = kia_common.paths_from_config(config)
    samples_path = Path(args.samples) if args.samples else paths["samples"]
    daily_path = Path(args.daily) if args.daily else paths["daily"]
    graph_path = Path(args.output) if args.output else paths["graph"]
    if args.data_dir and not (args.samples or args.daily):
        samples_path = Path(args.data_dir) / samples_path.name
        daily_path = Path(args.data_dir) / daily_path.name
    if args.data_dir and not args.output:
        graph_path = Path(args.data_dir) / graph_path.name

    samples = load_samples(samples_path)
    daily = load_daily(daily_path)
    if not samples and not daily:
        logger.error(
            "No data found (%s and %s missing or empty). Run collect.py first.",
            samples_path, daily_path,
        )
        return 1

    # Capacity used when samples.csv has none recorded: CLI override, else
    # the config value (offline analysis cannot query the API).
    default_capacity, capacity_source, capacity_desc = (
        kia_common.resolve_battery_capacity_kwh(None, config)
    )
    if args.capacity_kwh is not None:
        default_capacity, capacity_source = args.capacity_kwh, "cli"
        capacity_desc = f"--capacity-kwh override {args.capacity_kwh:g} kWh"
    window_days = args.window or int(config["analysis"]["rolling_window_days"])
    plan_factor = ((float(config["analysis"]["plan_max_pct"])
                    - float(config["analysis"]["plan_min_pct"])) / 100.0)

    records = build_day_records(samples, daily, config, default_capacity)
    if args.capacity_kwh is not None:
        # A CLI capacity override applies to EVERY day (including days with
        # a capacity recorded by collect.py), so "what-if" experiments
        # rescale the whole history rather than only sample-less days.
        for record in records:
            record.capacity_kwh = args.capacity_kwh
    kwh_correction, correction_note = resolve_kwh_correction(config, records)
    series, skipped = estimate_series(records, config, kwh_correction)
    if not series["range_pct"] and not series["range_kwh"]:
        logger.error(
            "Not enough data yet to estimate range. Method 1 needs morning "
            "and evening samples on days with driving; method 2 needs "
            "daily distance/kWh stats. Keep running collect.py twice daily."
        )
        return 1

    rolling_pct = rolling_average(series["range_pct"], window_days)
    rolling_kwh = rolling_average(series["range_kwh"], window_days)
    planning_pct = scale_series(rolling_pct, plan_factor)
    planning_kwh = scale_series(rolling_kwh, plan_factor)

    # Convert the finished range estimates (and everything derived from
    # them) from the internal km representation into the reporting units.
    unit_label, unit_factor = output_units(config, args.units)
    if unit_factor != 1.0:
        series["range_pct"] = scale_series(series["range_pct"], unit_factor)
        series["range_kwh"] = scale_series(series["range_kwh"], unit_factor)
        rolling_pct = scale_series(rolling_pct, unit_factor)
        rolling_kwh = scale_series(rolling_kwh, unit_factor)
        planning_pct = scale_series(planning_pct, unit_factor)
        planning_kwh = scale_series(planning_kwh, unit_factor)

    vehicle_name = next(
        (s["vehicle_name"] for s in samples if s["vehicle_name"]), "Kia EV"
    )
    make_graph(
        graph_path,
        panels=[
            ("Method 1: battery % change vs distance driven",
             series["range_pct"], rolling_pct, planning_pct),
            ("Method 2: reported kWh used x battery capacity "
             f"(default: {capacity_desc}"
             + (f", API kWh x{kwh_correction:.2f}" if kwh_correction != 1.0
                else "") + ")",
             series["range_kwh"], rolling_kwh, planning_kwh),
        ],
        window_days=window_days,
        plan_max_pct=float(config["analysis"]["plan_max_pct"]),
        plan_min_pct=float(config["analysis"]["plan_min_pct"]),
        vehicle_name=vehicle_name,
        show=args.show,
        unit_label=unit_label,
    )
    print_summary(records, series, skipped, rolling_pct, rolling_kwh,
                  window_days, plan_factor, unit_label, unit_factor,
                  kwh_correction, correction_note)
    return 0


if __name__ == "__main__":
    sys.exit(main())
