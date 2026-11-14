# Tests for the Kia EV collector / analyser

Offline regression tests (no network, no Kia account needed) using the
standard-library `unittest` module — no extra dependencies.

Run everything from the repository root:

```bash
venv/bin/python -m unittest discover -s tests -v
```

or a single module / directly:

```bash
venv/bin/python -m unittest tests.test_analyze -v   # (add tests/__init__? no: use discovery)
venv/bin/python tests/test_pipeline.py
```

(The simplest invocations are `python -m unittest discover -s tests -v`
and running any `tests/test_*.py` file directly.)

## Files

| File | Covers |
|---|---|
| `support.py` | Shared helpers: fake `Vehicle`/`DailyDrivingStats` stand-ins, CSV writers matching `collect.py`'s layouts, `build_synthetic_dataset()` (a fake driving history with *known* true range/efficiency/capacity), subprocess runners for `analyze.py` and `publish.py`. |
| `test_kia_common.py` | Config defaults/merging, credentials loading + permissions warning, token-cache save/load/round-trip/permissions/corruption handling, timezone + path resolution, km/mi conversion, battery-capacity resolution rules (`auto`/`api`/`config`, sanity check). |
| `test_collect.py` | Sample-type labelling (`pre_charge`/`post_charge`, cutoff rule), sample-row construction, samples.csv append/header behaviour, daily.csv upsert semantics (refresh same date, keep dropped history, duplicate-date resolution, Wh→kWh, mi→km), and token-cache reuse/fallback across runs. |
| `test_analyze.py` | CSV loaders + malformed-row handling, morning/evening sample pairing, per-day capacity forward-fill, mid-day top-up detection (without false positives from duplicate anchors), every accept/skip rule of both estimation methods, method-2 kWh correction (`auto`/fixed/clamped/invalid), rolling average windowing, planning-line scaling, output-unit resolution (km/miles/fallback), SOC-vs-API energy accounting (incl. exclusion of top-up days), headless graph rendering. |
| `test_pipeline.py` | End-to-end: synthetic dataset → `analyze.py` subprocess → asserts both methods recover the true 356 km range, planning line = 0.8 × rolling average, implied capacity ≈ 71.2 kWh, the energy-accounting ratio ≈ 100 %, exclusion reasons reported, PNG graph written, `--capacity-kwh` override behaviour, and non-zero exit on empty data. |
| `test_import_manual.py` | The spreadsheet converter: a generated .xlsx is parsed into charge events (home charges start on the recorded evening / end next morning, away charges mid-day, miles→km, registration cell), driving-period derivation incl. rising-battery rejection, weight/even apportioning, day-boundary sample synthesis, derived-daily gating, and merge idempotency. Requires `openpyxl`. |
| `test_publish.py` | The static-site publisher: index.html + graph + CSV downloads written to the output folder, both method cards / rolling / planning / efficiency / energy-accounting values present, method-2 correction applied and shown, `--units miles` conversion, `[publish].recent_days` row limit, HTML escaping of the title, and non-zero exit with empty data. |
| `test_backfill.py` | The API-history backfill helpers (offline): Wh→kWh and mi→km conversion, skipping the partial "today" row and zero-energy days, upsert that preserves `distance_range_km`, and CSV round-tripping. |

## Notes

* Graph tests use matplotlib's headless `Agg` backend, so they pass on a
  server without a display.
* Temp files are created under `/tmp/opencode` (or the system temp dir via
  `tempfile`) and cleaned up automatically.
* The live API path (`collect.py` against the real Kia servers) is
  deliberately *not* tested here — run `collect.py` manually once after
  any change that touches `kia_common.refresh_vehicles` /
  `select_vehicle`.
