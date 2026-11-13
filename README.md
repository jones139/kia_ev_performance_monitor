# Kia EV daily data collector & range analyser

Collects battery level and driving data from the Kia Connect API twice a
day, and analyses it to estimate the vehicle's range over time.

Built around `info.py` (the original API example). Python 3.11+ required
(uses the standard-library `tomllib`); tested with the `venv` in this
folder.

## Files

| File | Purpose |
|---|---|
| `collect.py` | Run twice a day (cron). Records a battery-level snapshot and refreshes per-day driving stats. |
| `analyze.py` | Run any time. Computes range estimates, prints a summary and writes the range graph. |
| `kia_common.py` | Shared helpers (config/credentials loading, API connection, battery-capacity resolution). |
| `config.toml` | All settings: battery capacity, file paths, sanity thresholds, analysis options. |
| `credentials.json` | Kia Connect login. **Keep private** (`chmod 600`). Not referenced by any code except via `kia_common.load_credentials`. |
| `credentials.example.json` | Template for the credentials file. |
| `data/samples.csv` | One appended row per `collect.py` run (timestamp, sample type, battery %, odometer, capacity used). |
| `data/daily.csv` | Per-calendar-day driving stats from the API (distance, kWh used + breakdown), refreshed on every run. |
| `data/range_analysis.png` | The range graph produced by `analyze.py`. |
| `one_off/import_manual_charges.py` | One-off (re-runnable) converter: pre-populates the CSVs from the manual charging spreadsheet `PV5_Efficiency.xlsx`. |
| `one_off/manual_charges_parsed.csv` | Normalised record of every charge event / driving period the converter read (future reference). |
| `tests/` | Offline regression tests (stdlib `unittest`, no extra deps). See `tests/README.md`. |

CSV was chosen over JSON for storage: the data is tabular time-series,
CSV stays human-readable, opens in spreadsheets and is easy to inspect.

## Setup

```bash
cd /home/graham/kia
cp credentials.example.json credentials.json   # then edit (already done here)
chmod 600 credentials.json                     # protect the credentials
venv/bin/pip install matplotlib openpyxl       # analyze.py graphs / spreadsheet import
```

Edit `config.toml` if needed — in particular `[battery].capacity_kwh`
(see "Battery capacity" below).

## Scheduling (cron)

Run the collector just **before** the overnight charge starts and just
**after** it finishes, assuming the car is not driven overnight:

```cron
# end-of-day sample, just before midnight / before charging starts
45 23 * * * cd /home/graham/kia && venv/bin/python collect.py --sample-type pre_charge >> data/collect.log 2>&1
# start-of-day sample, after charging finishes
30 6  * * * cd /home/graham/kia && venv/bin/python collect.py --sample-type post_charge >> data/collect.log 2>&1
# regenerate the graph + summary every morning
45 6  * * * cd /home/graham/kia && venv/bin/python analyze.py >> data/analyze.log 2>&1
```

(Install with `crontab -e`. Passing `--sample-type` explicitly makes the
labelling independent of the run time; omit it and the script infers the
type from the local hour vs `[schedule].morning_cutoff_hour`.)

## Pre-populating from the manual charging log (one-off)

`PV5_Efficiency.xlsx` ("Charging" sheet) holds one row per charge event
since the car was new: date, odometer **in miles**, battery % at charge
start/end, energy supplied (charger side, kWh) and location.  It has no
vehicle-consumption kWh, so only battery-% samples can come from it.

`one_off/import_manual_charges.py` converts it once and merges it into
the CSV data (all distances/odometers converted miles -> km, matching
the API's km storage):

```bash
venv/bin/python one_off/import_manual_charges.py --dry-run   # preview
venv/bin/python one_off/import_manual_charges.py             # import
```

* per charge event: a `pre_charge` sample at the charge start % and a
  `post_charge` sample at the charge end % (+ odometer km);
* per inter-charge driving period: the measured % drop (and, for days
  before the API's ~30-day window, the odometer distance) apportioned
  across the covered calendar days, producing `manual_derived`
  day-boundary samples and distance-only daily rows.  Within periods the
  API fully covers, the API's per-day distance *shape* is rescaled to the
  period's measured odometer total (rest days inside the API window count
  as 0 km) and stored alongside each row as `distance_range_km`, so
  method 1 always pairs each apportioned % drop with the matching
  distance share;
* provenance is recorded in a `source` column (`collect`/`api` for the
  collector, `manual`/`manual_derived` for the import), and the raw
  conversion inputs are kept in `one_off/manual_charges_parsed.csv`
  (including both miles and km odometer values for audit).

The script is idempotent: re-running regenerates every `manual*` row
from the spreadsheet while preserving `collect`/`api` rows, so run it
again any time the spreadsheet gains new charge entries. Because the
sheet has no times of day, home charges are assumed to run overnight
(start ~23:30 previous evening, end ~07:30) and away charges mid-day
(~11:00-12:00); `analyze.py` pairs each day's first morning sample with
the last evening sample *after it*, so mid-day charges cannot invert a
pair.

## How the range is estimated

Each day provides up to two battery samples: `post_charge` (start of day,
after charging) and `pre_charge` (end of day, before charging), plus the
API's own per-day figures for distance (km) and energy used (kWh).

* **Method 1 — battery % change:**
  `range = distance / (morning % − evening %) × 100`
  Needs both samples and a % drop of at least `[analysis].min_pct_delta`.
  If the battery went *up* during the day (daytime charging) the day is
  skipped. Note the % is integer-resolution, so short days are noisy —
  hence the rolling average.

* **Method 2 — kWh used × capacity:**
  `range = distance / kWh used × usable capacity (kWh)`
  Needs the day's distance and kWh above small thresholds.

For each method the graph shows the daily **spot estimate**, a **rolling
average over the previous 7 days** (window configurable), and a
**planning range** = rolling average × 0.8, i.e. assuming the battery is
only run from 100 % down to 20 % (`[analysis].plan_max_pct` /
`plan_min_pct`).

Finished estimates outside a physically plausible full-charge band
(`[analysis].min_range_km` / `max_range_km`, default 150-650 km) are
discarded as glitches — typically days where the API's per-day distance
attribution and the charging log's odometer deltas disagree about when
km were driven — and counted in the summary as "implausible estimate".

## Battery capacity

Method 2 needs the usable battery capacity. The API field
`ev_battery_capacity` returns `256320` for the PV5; the unit is
undocumented, but `256320 / 3600 = 71.2 kWh` exactly, which matches the
PV5 long-range pack — so the default `capacity_source = "auto"`
interprets the raw API value as kilojoules, sanity-checks it against
`[battery].capacity_kwh` (±25 %) and falls back to the config value if it
looks wrong. Both the raw and resolved values are stored in
`samples.csv`.

To verify over time, `analyze.py` prints an **implied capacity** computed
from your real driving data (`kWh used ÷ % drop × 100`, days with ≥5 %
drop). Once a couple of weeks of data agree with 71.2 kWh you can trust
it; if they disagree, set `capacity_kwh` accordingly (or set
`capacity_source = "config"` to ignore the API value entirely).

## Notes & limitations

* Driving between the evening sample and midnight (or between midnight
  and the morning sample) lands in the API's day distance but not fully
  inside the % window — usually negligible.
* Daytime (en-route) charging invalidates method 1 for that day; it is
  detected (negative % delta) and skipped.
* `daily.csv` is refreshed from the API on every run, so "today's"
  partial figures self-correct later, and history older than the API's
  ~30-day window is preserved permanently in the CSV.
* `manual_derived` daily rows carry distance only (the spreadsheet has no
  consumption kWh), so efficiency totals in the summary are computed over
  days that have both distance and kWh.
* Both CSVs have a `source` provenance column; rows without one (files
  created before it existed) are treated as `collect`/`api`.
* `info.py` still contains the plaintext password; consider deleting it
  now that `credentials.json` holds the login.
* The Kia API occasionally fails; `collect.py` retries
  (`[api].retry_count`) and logs errors to stdout (redirected to
  `data/collect.log` by cron).

## Tests

Offline regression tests (no network / Kia account needed, stdlib
`unittest` only) live in `tests/` — see `tests/README.md`:

```bash
cd /home/graham/kia
venv/bin/python -m unittest discover -s tests -v
```

They cover the config/credentials/capacity helpers, the CSV writers and
upsert semantics, every accept/skip rule of both range-estimation methods,
the rolling average and graph rendering, plus an end-to-end run of
`analyze.py` over a synthetic driving history with known ground truth
(range, efficiency, capacity).

## Manual use

```bash
venv/bin/python collect.py                       # sample now (type inferred)
venv/bin/python collect.py --sample-type pre_charge
venv/bin/python analyze.py                       # summary + data/range_analysis.png
venv/bin/python analyze.py --window 14 --show    # custom window, display graph
venv/bin/python analyze.py --capacity-kwh 68     # test a different capacity
```
