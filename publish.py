#!/usr/bin/env python3
"""Publish the range analysis as a small static web page.

Runs analyze.py (so the canonical summary and graph are produced exactly as
cron would) and writes a self-contained folder that can be uploaded to any
static web server:

    index.html          - the summary page, with the graph embedded
    range_analysis.png  - the two-panel range graph (written by analyze.py)
    samples.csv         - copy of the raw battery samples (download link)
    daily.csv           - copy of the raw per-day driving stats

The page shows, for each estimation method: the latest daily estimate, the
rolling average and the planning range, followed by period totals,
efficiency, the implied-capacity diagnostic, the list of excluded days and
a table of the most recent days.  All figures use the same units as
analyze.py ([analysis].output_units, or --units).

Command line interface:
    publish.py [--config FILE] [--data-dir DIR] [--output-dir DIR]
               [--units {km,miles}] [--recent-days N]
"""

from __future__ import annotations

import argparse
import html
import logging
import shutil
import statistics
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

import analyze
import kia_common
from kia_common import logger

REPO_ROOT = Path(__file__).resolve().parent

# Stylesheet embedded in index.html, so the published folder has no external
# dependencies beyond the graph image and the CSV downloads.
PAGE_CSS = """
:root { --ink:#1d232b; --muted:#5b6673; --accent:#1f4e79; --line:#e7ebf0;
        --bg:#f5f7fa; --card:#ffffff; }
* { box-sizing: border-box; }
body { margin:0; font-family: system-ui, -apple-system, "Segoe UI", Roboto,
       sans-serif; color:var(--ink); background:var(--bg); line-height:1.45; }
header { background:var(--accent); color:#fff; padding:20px 24px; }
header .wrap, main, footer { max-width:1040px; margin:0 auto; }
header h1 { margin:0; font-size:1.45rem; }
header p { margin:6px 0 0; font-size:.85rem; opacity:.85; }
main { padding:22px 16px 8px; }
h2.sec { font-size:1.1rem; margin:26px 0 10px; color:var(--accent); }
.cards { display:grid; grid-template-columns:repeat(auto-fit, minmax(300px, 1fr));
         gap:16px; }
.card { background:var(--card); border-radius:10px; padding:16px 18px;
        box-shadow:0 1px 2px rgba(16,24,40,.06), 0 1px 3px rgba(16,24,40,.1); }
.card h2 { margin:0; font-size:.98rem; color:var(--accent); }
.card .note { margin:2px 0 10px; font-size:.78rem; color:var(--muted); }
.big { font-size:2rem; font-weight:650; font-variant-numeric:tabular-nums; }
.sub { font-size:.8rem; color:var(--muted); margin-top:-2px; }
dl { display:grid; grid-template-columns:1fr auto; gap:5px 14px;
     margin:12px 0 0; font-size:.88rem; }
dt { color:var(--muted); }
dd { margin:0; text-align:right; font-variant-numeric:tabular-nums; }
.empty { color:var(--muted); font-size:.9rem; }
figure { margin:0; }
figure img { width:100%; height:auto; display:block; border-radius:10px;
             background:var(--card); box-shadow:0 1px 2px rgba(16,24,40,.06),
             0 1px 3px rgba(16,24,40,.1); }
ul.stats { list-style:none; margin:0; padding:12px 18px; background:var(--card);
           border-radius:10px; font-size:.9rem;
           box-shadow:0 1px 2px rgba(16,24,40,.06), 0 1px 3px rgba(16,24,40,.1); }
ul.stats li { padding:6px 0; border-bottom:1px solid var(--line); }
ul.stats li:last-child { border-bottom:0; }
.table-wrap { overflow-x:auto; }
table { width:100%; border-collapse:collapse; background:var(--card);
        border-radius:10px; overflow:hidden; font-size:.85rem;
        box-shadow:0 1px 2px rgba(16,24,40,.06), 0 1px 3px rgba(16,24,40,.1); }
th, td { padding:7px 10px; text-align:right; white-space:nowrap;
         font-variant-numeric:tabular-nums; }
th { background:#e9eef4; font-weight:600; font-size:.8rem; }
td:first-child, th:first-child { text-align:left; }
tbody tr:nth-child(even) { background:#f8fafc; }
footer { padding:14px 16px 26px; color:var(--muted); font-size:.8rem; }
footer a { color:var(--accent); }
@media (max-width:640px) { .big { font-size:1.6rem; } }
"""


@dataclass
class MethodSummary:
    """Display-ready estimates for one range-estimation method.

    Interface: ``title`` names the method, ``description`` explains its
    formula, ``spot_value`` / ``spot_date`` are the latest accepted daily
    estimate, ``rolling_value`` / ``rolling_date`` the latest rolling
    average and ``planning_value`` the planning range derived from it.  All
    range values are already converted to the reporting units; any value
    may be None when the method has no estimates yet.
    """

    title: str
    description: str
    spot_value: float | None = None
    spot_date: date | None = None
    rolling_value: float | None = None
    rolling_date: date | None = None
    planning_value: float | None = None


@dataclass
class RecentDay:
    """One row of the "recent days" table, in reporting units.

    Interface: ``distance`` is the day's distance (reporting units),
    ``kwh`` the reported energy use, ``pct_start`` / ``pct_end`` the
    bounding battery percentages, and ``range_pct`` / ``range_kwh`` the
    day's accepted estimates from each method (None when the day was
    rejected or has no estimate).
    """

    day: date
    distance: float | None = None
    kwh: float | None = None
    pct_start: float | None = None
    pct_end: float | None = None
    range_pct: float | None = None
    range_kwh: float | None = None


@dataclass
class SiteContext:
    """Everything the HTML template needs, already in reporting units.

    Interface: built by build_context() and consumed by render_html().  The
    range/distance figures are converted to ``unit_label``; ``efficiency``
    is in units/kWh; the implied-capacity fields stay in kWh (capacity is
    not a distance and is never converted).
    """

    title: str
    vehicle_name: str
    generated_at: str
    unit_label: str
    window_days: int
    plan_max_pct: float
    plan_min_pct: float
    period_start: date | None
    period_end: date | None
    driving_days: int
    methods: list[MethodSummary] = field(default_factory=list)
    total_distance: float = 0.0
    efficiency: float | None = None
    efficiency_kwh: float = 0.0
    efficiency_days: int = 0
    recent_distance: float | None = None
    recent_kwh: float = 0.0
    recent_efficiency: float | None = None
    implied_median: float | None = None
    implied_min: float | None = None
    implied_max: float | None = None
    implied_count: int = 0
    excluded: dict[str, int] = field(default_factory=dict)
    recent_rows: list[RecentDay] = field(default_factory=list)
    graph_file: str = "range_analysis.png"
    samples_file: str = "samples.csv"
    daily_file: str = "daily.csv"
    energy: dict = field(default_factory=dict)
    kwh_correction: float = 1.0
    kwh_correction_note: str = ""


def resolve_output_dir(config: dict, override: str | None = None) -> Path:
    """Resolve the static-site output folder.

    Interface: takes the config dict and an optional CLI override; returns
    the output directory as a Path.  Relative paths are resolved against
    the repository folder (the same rule as [paths].data_dir).  The folder
    is not created here.
    """
    raw = override or config["publish"]["output_dir"]
    output_dir = Path(raw)
    if not output_dir.is_absolute():
        output_dir = kia_common.SCRIPT_DIR / output_dir
    return output_dir


def build_context(
    config: dict,
    samples: list[dict],
    daily: dict[date, dict],
    graph_file: str = "range_analysis.png",
    samples_file: str = "samples.csv",
    daily_file: str = "daily.csv",
    units: str | None = None,
    recent_days: int | None = None,
    now: datetime | None = None,
) -> SiteContext:
    """Compute every value shown on the published page.

    Interface: takes the config dict, parsed samples (analyze.load_samples),
    parsed daily stats (analyze.load_daily), the file names to link to, an
    optional units override and an optional number of recent-day rows.
    Returns a SiteContext.  Uses exactly the same loaders / estimators /
    thresholds as analyze.py, then converts the range and distance figures
    into the reporting units.  Raises ValueError when there are no usable
    estimates at all (analyze.py rejects that case too).
    """
    analysis_cfg = config["analysis"]
    window_days = int(analysis_cfg["rolling_window_days"])
    plan_max_pct = float(analysis_cfg["plan_max_pct"])
    plan_min_pct = float(analysis_cfg["plan_min_pct"])
    plan_factor = (plan_max_pct - plan_min_pct) / 100.0
    unit_label, unit_factor = analyze.output_units(config, units)

    default_capacity, _, _ = kia_common.resolve_battery_capacity_kwh(None, config)
    records = analyze.build_day_records(samples, daily, config, default_capacity)
    kwh_correction, kwh_correction_note = analyze.resolve_kwh_correction(
        config, records
    )
    series, skipped = analyze.estimate_series(
        records, config, kwh_correction
    )
    if not series["range_pct"] and not series["range_kwh"]:
        raise ValueError(
            "Not enough data to estimate range (run collect.py twice daily, "
            "or import the charging spreadsheet first)."
        )

    rolling_pct = analyze.rolling_average(series["range_pct"], window_days)
    rolling_kwh = analyze.rolling_average(series["range_kwh"], window_days)

    def convert(values: list[tuple[date, float]]) -> list[tuple[date, float]]:
        """Scale a (date, km) series into the reporting units."""
        return analyze.scale_series(values, unit_factor)

    display_pct = convert(series["range_pct"])
    display_kwh = convert(series["range_kwh"])
    display_roll_pct = convert(rolling_pct)
    display_roll_kwh = convert(rolling_kwh)
    planning_pct = analyze.scale_series(display_roll_pct, plan_factor)
    planning_kwh = analyze.scale_series(display_roll_kwh, plan_factor)

    def latest(values: list[tuple[date, float]]) -> tuple[date | None, float | None]:
        """Return the last (date, value) of a series, or (None, None)."""
        return values[-1] if values else (None, None)

    spot_pct_date, spot_pct = latest(display_pct)
    roll_pct_date, roll_pct = latest(display_roll_pct)
    plan_pct_date, plan_pct = latest(planning_pct)
    spot_kwh_date, spot_kwh = latest(display_kwh)
    roll_kwh_date, roll_kwh = latest(display_roll_kwh)
    plan_kwh_date, plan_kwh = latest(planning_kwh)

    methods = [
        MethodSummary(
            title="Method 1: battery % change",
            description="Distance driven &divide; battery-% drop &times; 100",
            spot_value=spot_pct, spot_date=spot_pct_date,
            rolling_value=roll_pct, rolling_date=roll_pct_date,
            planning_value=plan_pct,
        ),
        MethodSummary(
            title="Method 2: kWh used &times; capacity",
            description=(
                "Distance &divide; kWh used &times; usable capacity"
                + (f" (API kWh &times;{kwh_correction:.2f} to allow for "
                   "drivetrain losses)" if kwh_correction != 1.0 else "")
            ),
            spot_value=spot_kwh, spot_date=spot_kwh_date,
            rolling_value=roll_kwh, rolling_date=roll_kwh_date,
            planning_value=plan_kwh,
        ),
    ]

    driving_days = [r for r in records if r.distance_km is not None]
    both_days = [r for r in driving_days if r.kwh_used is not None]
    total_distance = sum(r.distance_km for r in driving_days) * unit_factor
    efficiency = None
    efficiency_kwh = 0.0
    if both_days:
        effort_dist = sum(r.distance_km for r in both_days) * unit_factor
        efficiency_kwh = sum(r.kwh_used for r in both_days)
        if efficiency_kwh > 0:
            efficiency = effort_dist / efficiency_kwh

    cutoff = max((r.date for r in driving_days), default=None)
    recent_distance = recent_efficiency = None
    recent_kwh = 0.0
    if cutoff is not None:
        window = [r for r in both_days
                  if r.date > cutoff - timedelta(days=window_days)]
        recent_kwh = sum(r.kwh_used for r in window)
        if recent_kwh > 0:
            recent_distance = sum(r.distance_km for r in window) * unit_factor
            recent_efficiency = recent_distance / recent_kwh

    implied = [value for _, value in series["implied_capacity"]]
    implied_median = implied_min = implied_max = None
    if len(implied) >= 3:
        implied_median = statistics.median(implied)
        implied_min, implied_max = min(implied), max(implied)

    row_limit = int(recent_days if recent_days is not None
                    else config["publish"]["recent_days"])
    pct_by_date = dict(display_pct)
    kwh_by_date = dict(display_kwh)
    selected = records[-row_limit:] if row_limit > 0 else []
    recent_rows = [
        RecentDay(
            day=record.date,
            distance=(record.distance_km * unit_factor
                      if record.distance_km is not None else None),
            kwh=record.kwh_used,
            pct_start=record.pct_start,
            pct_end=record.pct_end,
            range_pct=pct_by_date.get(record.date),
            range_kwh=kwh_by_date.get(record.date),
        )
        for record in selected
    ]
    recent_rows.reverse()

    vehicle_name = next(
        (sample["vehicle_name"] for sample in samples if sample["vehicle_name"]),
        "Kia EV",
    )
    generated = (now or datetime.now().astimezone()).strftime("%Y-%m-%d %H:%M %Z")

    return SiteContext(
        title=str(config["publish"]["title"]),
        vehicle_name=vehicle_name,
        generated_at=generated,
        unit_label=unit_label,
        window_days=window_days,
        plan_max_pct=plan_max_pct,
        plan_min_pct=plan_min_pct,
        period_start=driving_days[0].date if driving_days else None,
        period_end=driving_days[-1].date if driving_days else None,
        driving_days=len(driving_days),
        methods=methods,
        total_distance=total_distance,
        efficiency=efficiency,
        efficiency_kwh=efficiency_kwh,
        efficiency_days=len(both_days),
        recent_distance=recent_distance,
        recent_kwh=recent_kwh,
        recent_efficiency=recent_efficiency,
        implied_median=implied_median,
        implied_min=implied_min,
        implied_max=implied_max,
        implied_count=len(implied),
        excluded={reason: count for reason, count in skipped.items() if count},
        recent_rows=recent_rows,
        graph_file=graph_file,
        samples_file=samples_file,
        daily_file=daily_file,
        energy=analyze.energy_accounting(records),
        kwh_correction=kwh_correction,
        kwh_correction_note=kwh_correction_note,
    )


def _fmt_distance(value: float | None, unit_label: str) -> str:
    """Format a distance/range value with its unit, or an em dash if None."""
    if value is None:
        return "&mdash;"
    return f"{value:,.0f} {html.escape(unit_label)}"


def _fmt_kwh(value: float | None) -> str:
    """Format an energy value in kWh, or an em dash if None."""
    if value is None:
        return "&mdash;"
    return f"{value:,.1f} kWh"


def _fmt_pct(value: float | None) -> str:
    """Format a battery percentage, or an em dash if None."""
    if value is None:
        return "&mdash;"
    return f"{value:.0f}%"


def _render_method_card(method: MethodSummary, context: SiteContext) -> str:
    """Render one method's summary card as an HTML fragment.

    Interface: takes a MethodSummary (values already in reporting units)
    and the SiteContext (for the unit label, window and planning SOC
    limits); returns an ``<article>`` string.  Shows the rolling average as
    the headline figure with the latest spot estimate and planning range
    underneath, or a short "no estimates" note when the method has none.
    """
    unit = context.unit_label
    if method.spot_value is None:
        body = ('<p class="empty">No usable daily estimates yet &mdash; '
                'keep collecting morning and evening samples.</p>')
    else:
        headline = ""
        if method.rolling_value is not None:
            headline = (
                f'<div class="big">{_fmt_distance(method.rolling_value, unit)}'
                f'</div><div class="sub">{context.window_days}-day rolling '
                f'average</div>'
            )
        rows = [("Latest daily estimate",
                 f"{_fmt_distance(method.spot_value, unit)}"
                 f" <span class=\"sub\">({method.spot_date})</span>")]
        if method.rolling_value is not None:
            rows.append((
                f"Rolling average ({context.window_days} days)",
                _fmt_distance(method.rolling_value, unit),
            ))
        if method.planning_value is not None:
            rows.append((
                f"Planning range ({context.plan_max_pct:g}&rarr;"
                f"{context.plan_min_pct:g}%)",
                _fmt_distance(method.planning_value, unit),
            ))
        body = headline + "<dl>" + "".join(
            f"<dt>{label}</dt><dd>{value}</dd>" for label, value in rows
        ) + "</dl>"
    return (
        f'<article class="card"><h2>{method.title}</h2>'
        f'<p class="note">{method.description}</p>{body}</article>'
    )


def _render_recent_table(context: SiteContext) -> str:
    """Render the recent-days table as an HTML fragment.

    Interface: takes the SiteContext; returns a table with one row per
    recent day (newest first) showing distance, energy, the bounding
    battery percentages and each method's estimate for that day.  Missing
    values appear as em dashes.
    """
    unit = context.unit_label
    if not context.recent_rows:
        return '<p class="empty">No days recorded yet.</p>'
    header = (
        "<tr><th>Date</th><th>Distance</th><th>Energy</th><th>Battery</th>"
        "<th>Range (battery %)</th><th>Range (kWh)</th></tr>"
    )
    body = []
    for row in context.recent_rows:
        battery = (f"{_fmt_pct(row.pct_start)} &rarr; {_fmt_pct(row.pct_end)}"
                   if row.pct_start is not None or row.pct_end is not None
                   else "&mdash;")
        body.append(
            f"<tr><td>{row.day}</td><td>{_fmt_distance(row.distance, unit)}</td>"
            f"<td>{_fmt_kwh(row.kwh)}</td><td>{battery}</td>"
            f"<td>{_fmt_distance(row.range_pct, unit)}</td>"
            f"<td>{_fmt_distance(row.range_kwh, unit)}</td></tr>"
        )
    return ("<div class=\"table-wrap\"><table><thead>" + header
            + "</thead><tbody>" + "".join(body)
            + "</tbody></table></div>")


def render_html(context: SiteContext) -> str:
    """Render the complete index.html document for a SiteContext.

    Interface: takes a SiteContext (build_context) whose values are already
    in the reporting units; returns the HTML as a string.  All dynamic text
    is HTML-escaped.  The page contains: a header, one card per estimation
    method, the embedded graph, a summary list (period, totals, efficiency,
    implied capacity, excluded days) and the recent-days table.
    """
    unit = html.escape(context.unit_label)
    title = html.escape(context.title)
    vehicle = html.escape(context.vehicle_name)
    cards = "".join(_render_method_card(method, context)
                    for method in context.methods)

    stats = []
    if context.period_start and context.period_end:
        stats.append(
            f"Period: {context.period_start} to {context.period_end} "
            f"({context.driving_days} day(s) with driving stats)"
        )
    if context.efficiency is not None:
        stats.append(
            f"Total: {context.total_distance:,.0f} {unit} over "
            f"{context.driving_days} day(s); {context.efficiency_kwh:,.1f} kWh "
            f"over the {context.efficiency_days} day(s) with energy data "
            f"&rarr; average {context.efficiency:.2f} {unit}/kWh"
        )
    if context.recent_efficiency is not None:
        stats.append(
            f"Last {context.window_days} days: {context.recent_distance:,.0f} "
            f"{unit} using {context.recent_kwh:,.1f} kWh &rarr; "
            f"{context.recent_efficiency:.2f} {unit}/kWh"
        )
    if context.implied_count and context.implied_median is not None:
        stats.append(
            f"Implied usable battery capacity: median "
            f"{context.implied_median:.1f} kWh from {context.implied_count} "
            f"day(s) (min {context.implied_min:.1f}, max "
            f"{context.implied_max:.1f}) &mdash; compare with "
            f"[battery].capacity_kwh in config.toml"
        )
    energy = context.energy
    if energy.get("days", 0) >= 3 and energy.get("ratio") is not None:
        stats.append(
            f"Energy accounting: over {energy['days']} day(s) with both "
            f"battery-% and kWh data, the battery used "
            f"{energy['soc_kwh']:,.1f} kWh but the API reported "
            f"{energy['api_kwh']:,.1f} kWh ({energy['ratio']:.0%}) &mdash; "
            f"{energy['gap_kwh']:,.1f} kWh unaccounted "
            f"({energy['gap_per_day']:,.1f} kWh/day, "
            f"{energy['gap_per_100km']:,.1f} kWh/100km), so raw method 2 "
            f"over-reads by ~{1 / energy['ratio'] - 1:.0%}"
        )
    if context.kwh_correction != 1.0 or context.kwh_correction_note:
        note = html.escape(context.kwh_correction_note)
        stats.append(
            f"Method 2 correction: &times;{context.kwh_correction:.2f}"
            + (f" ({note})" if note else "")
        )
    if context.excluded:
        stats.append("Days excluded: " + ", ".join(
            f"{count} ({html.escape(reason.replace('_', ' '))})"
            for reason, count in context.excluded.items()
        ))
    stats.append(f"Page generated {html.escape(context.generated_at)}")
    stats_html = "".join(f"<li>{item}</li>" for item in stats)

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>{PAGE_CSS}</style>
</head>
<body>
<header><div class="wrap">
<h1>{title}</h1>
<p>{vehicle} &middot; estimated range report &middot; units: {unit} &middot;
   {context.window_days}-day rolling window</p>
</div></header>
<main>
<section class="cards">
{cards}
</section>
<h2 class="sec">Range history</h2>
<figure>
<img src="{html.escape(context.graph_file)}"
     alt="Estimated range over time: daily estimates, rolling average and
          planning range for both estimation methods">
</figure>
<h2 class="sec">Summary</h2>
<ul class="stats">
{stats_html}
</ul>
<h2 class="sec">Recent days</h2>
{_render_recent_table(context)}
</main>
<footer>
Auto-generated by <code>publish.py</code>. Raw data:
<a href="{html.escape(context.samples_file)}">battery samples</a> &middot;
<a href="{html.escape(context.daily_file)}">daily driving stats</a>.
</footer>
</body>
</html>
"""


def run_analyze(
    config_path: str | None,
    data_dir: str | None,
    output_path: Path,
    units: str | None = None,
) -> subprocess.CompletedProcess:
    """Run analyze.py as a subprocess to produce the graph and summary.

    Interface: takes the optional --config path, optional --data-dir
    override, the PNG path to write and the optional units override (all
    forwarded to analyze.py); returns the CompletedProcess with captured
    stdout/stderr.  Running the real script keeps the published graph
    identical to what cron produces.
    """
    command = [sys.executable, str(REPO_ROOT / "analyze.py"),
               "--output", str(output_path)]
    if config_path:
        command += ["--config", str(config_path)]
    if data_dir:
        command += ["--data-dir", str(data_dir)]
    if units:
        command += ["--units", units]
    return subprocess.run(command, capture_output=True, text=True,
                          cwd=str(REPO_ROOT))


def write_site(
    context: SiteContext,
    output_dir: Path,
    data_files: list[Path],
) -> list[Path]:
    """Write index.html and copy the raw-data downloads into the site folder.

    Interface: takes the SiteContext, the destination folder and the CSV
    files to make available as downloads.  Creates the folder if needed,
    writes ``index.html`` (overwriting any previous page) and copies each
    CSV under its own file name.  Returns the list of written paths.  The
    graph PNG is not copied here: analyze.py writes it straight into this
    folder.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    index_path = output_dir / "index.html"
    index_path.write_text(render_html(context), encoding="utf-8")
    written = [index_path]
    for source in data_files:
        if not source.exists():
            logger.warning("Data file %s missing; not copied", source)
            continue
        target = output_dir / source.name
        shutil.copy2(source, target)
        written.append(target)
    return written


def main(argv: list[str] | None = None) -> int:
    """Entry point: analyse the data and publish the static site.

    Interface: takes optional argv (defaults to sys.argv[1:]); supports
    --config, --data-dir, --output-dir, --units and --recent-days (see the
    module docstring).  Returns 0 on success, 1 when analyze.py fails or no
    data is available.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", help="path to config.toml")
    parser.add_argument("--data-dir", help="override the data directory")
    parser.add_argument("--output-dir", help="override [publish].output_dir")
    parser.add_argument("--units", choices=("km", "miles"),
                        help="override [analysis].output_units for this run")
    parser.add_argument("--recent-days", type=int,
                        help="override [publish].recent_days")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )

    try:
        config = kia_common.load_config(args.config)
    except (FileNotFoundError, ValueError) as error:
        logger.error("%s", error)
        return 1

    paths = kia_common.paths_from_config(config)
    samples_path = paths["samples"]
    daily_path = paths["daily"]
    if args.data_dir:
        samples_path = Path(args.data_dir) / samples_path.name
        daily_path = Path(args.data_dir) / daily_path.name
    output_dir = resolve_output_dir(config, args.output_dir)
    graph_path = output_dir / paths["graph"].name

    result = run_analyze(args.config, args.data_dir, graph_path, args.units)
    if result.returncode != 0:
        logger.error("analyze.py failed (exit %d): %s", result.returncode,
                     (result.stderr or result.stdout).strip())
        return 1
    if result.stdout:
        # Keep the canonical analysis summary visible when publish.py is run
        # manually (and in the cron log).
        print(result.stdout, end="")

    samples = analyze.load_samples(samples_path)
    daily = analyze.load_daily(daily_path)
    if not samples and not daily:
        logger.error("No data found (%s and %s missing or empty). "
                     "Run collect.py first.", samples_path, daily_path)
        return 1

    try:
        context = build_context(
            config, samples, daily,
            graph_file=graph_path.name,
            samples_file=samples_path.name,
            daily_file=daily_path.name,
            units=args.units,
            recent_days=args.recent_days,
        )
    except ValueError as error:
        logger.error("%s", error)
        return 1

    written = write_site(context, output_dir, [samples_path, daily_path])
    logger.info("Published %d file(s) to %s", len(written), output_dir)
    for path in written:
        logger.info("  %s", path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
