"""Tests for publish.py: static-site generation from the analysis data.

Builds the same deterministic synthetic driving history used by
test_pipeline.py (true full-charge range 356 km, efficiency 5.0 km/kWh),
runs publish.py against a temporary config, and asserts that:

  * index.html, the graph PNG and the CSV download copies are written to
    the configured output folder
  * the page reports both estimation methods, the rolling average and the
    planning range in the configured units
  * --units miles converts every figure on the page
  * the "recent days" table honours [publish].recent_days
  * dynamic text (the page title) is HTML-escaped
  * an empty data directory exits non-zero with a clear error

Run from the repository root with:
    venv/bin/python -m unittest discover -s tests -v
or directly:
    venv/bin/python tests/test_publish.py
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from datetime import date
from pathlib import Path

try:
    import support  # noqa: F401  (sets up sys.path)
except ImportError:  # loaded as tests.test_x package member
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import support  # noqa: E402,F401

import kia_common  # noqa: E402  (available via support's sys.path setup)
import publish  # noqa: E402
from support import (build_synthetic_dataset, run_publish, write_daily_csv,
                     write_samples_csv)

# Ground truth of the synthetic dataset (same as test_pipeline.py).
TRUE_RANGE_KM = 356.0
TRUE_CAPACITY_KWH = 71.2
EFFICIENCY = 5.0
DISTANCES = [0.0, 40.0, 60.0, 2.0, 80.0, 50.0, 45.0, 70.0, 30.0, 0.0, 55.0, 48.0]


class PublishSiteTest(unittest.TestCase):
    """Full offline run of publish.py over generated data."""

    def setUp(self):
        """Create a temp data dir, config file and site output dir."""
        self.tmp = Path(tempfile.mkdtemp(prefix="kia_publish_",
                                         dir="/tmp/opencode"))
        self.data_dir = self.tmp / "data"
        self.site_dir = self.tmp / "site"
        self.data_dir.mkdir()
        self.config_path = self.tmp / "config.toml"
        samples, daily = build_synthetic_dataset(
            start_day=date(2026, 11, 1),
            distances_km=DISTANCES,
            efficiency_km_per_kwh=EFFICIENCY,
            full_range_km=TRUE_RANGE_KM,
            capacity_kwh=TRUE_CAPACITY_KWH,
        )
        write_samples_csv(self.data_dir / "samples.csv", samples)
        write_daily_csv(self.data_dir / "daily.csv", daily)
        self.write_config()

    def tearDown(self):
        """Remove the temp tree."""
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_config(self, title: str = "Test PV5 report",
                     units: str = "km", recent_days: int = 5,
                     data_dir: Path | None = None,
                     output_dir: Path | None = None,
                     correction: float | None = None) -> None:
        """Write a minimal config.toml pointing at the temp folders."""
        data_dir = data_dir or self.data_dir
        output_dir = output_dir or self.site_dir
        correction_line = (
            f"api_energy_correction = {correction}\n"
            if correction is not None else ""
        )
        self.config_path.write_text(
            "[paths]\n"
            f'data_dir = "{data_dir}"\n'
            "\n[publish]\n"
            f'output_dir = "{output_dir}"\n'
            f"title = '{title}'\n"
            f"recent_days = {recent_days}\n"
            "\n[analysis]\n"
            "rolling_window_days = 7\n"
            f'output_units = "{units}"\n'
            f"{correction_line}"
            "\n[battery]\n"
            f"capacity_kwh = {TRUE_CAPACITY_KWH}\n",
            encoding="utf-8",
        )

    def publish_run(self, *extra: str):
        """Run publish.py against the temp config and return the process."""
        return run_publish(self.config_path, list(extra))

    def read_page(self) -> str:
        """Return the generated index.html text."""
        return (self.site_dir / "index.html").read_text(encoding="utf-8")

    def test_publishes_page_graph_and_downloads(self):
        """index.html, the PNG and both CSV copies are written."""
        result = self.publish_run()
        self.assertEqual(result.returncode, 0, result.stderr)
        page = self.read_page()
        self.assertTrue(page.startswith("<!doctype html>"))
        self.assertIn('src="range_analysis.png"', page)
        self.assertIn('href="samples.csv"', page)
        self.assertIn('href="daily.csv"', page)

        graph = self.site_dir / "range_analysis.png"
        self.assertTrue(graph.exists())
        self.assertGreater(graph.stat().st_size, 10_000)
        with open(graph, "rb") as handle:
            self.assertEqual(handle.read(8)[1:4], b"PNG")

        for name in ("samples.csv", "daily.csv"):
            published = self.site_dir / name
            self.assertTrue(published.exists(), name)
            self.assertEqual(published.read_bytes(),
                             (self.data_dir / name).read_bytes(), name)

    def test_page_reports_methods_rolling_and_planning(self):
        """The page shows both methods in km with the expected values."""
        result = self.publish_run()
        self.assertEqual(result.returncode, 0, result.stderr)
        page = self.read_page()
        for text in ("Method 1: battery % change",
                     "Method 2: kWh used",
                     "7-day rolling average"):
            self.assertIn(text, page)
        self.assertIn(f"{TRUE_RANGE_KM:,.0f} km", page)          # rolling avg
        self.assertIn(f"{TRUE_RANGE_KM * 0.8:,.0f} km", page)    # planning
        self.assertIn(f"{EFFICIENCY:.2f} km/kWh", page)          # efficiency
        self.assertIn("Implied usable battery capacity", page)
        self.assertIn("Energy accounting", page)
        self.assertIn("unaccounted", page)

    def test_method2_correction_is_applied_and_shown(self):
        """A configured factor scales method 2 and is noted on the page."""
        self.write_config(correction=1.25)
        result = self.publish_run()
        self.assertEqual(result.returncode, 0, result.stderr)
        page = self.read_page()
        self.assertIn("API kWh &times;1.25", page)
        self.assertIn("Method 2 correction", page)
        self.assertIn(f"{TRUE_RANGE_KM / 1.25:,.0f} km", page)

    def test_recent_days_limit(self):
        """[publish].recent_days controls the table row count."""
        result = self.publish_run()
        self.assertEqual(result.returncode, 0, result.stderr)
        # One header row plus five data rows.
        self.assertEqual(self.read_page().count("<tr>"), 6)

    def test_units_override_converts_every_figure(self):
        """--units miles converts ranges, planning and efficiency."""
        result = self.publish_run("--units", "miles")
        self.assertEqual(result.returncode, 0, result.stderr)
        page = self.read_page()
        expected_rolling = TRUE_RANGE_KM / 1.609344
        self.assertIn(f"{expected_rolling:,.0f} mi", page)
        self.assertIn(f"{expected_rolling * 0.8:,.0f} mi", page)
        self.assertIn("mi/kWh", page)
        self.assertNotIn(f"{TRUE_RANGE_KM:,.0f} km", page)

    def test_title_is_html_escaped(self):
        """Dynamic title text cannot inject markup into the page."""
        self.write_config(title='<script>alert("x")</script>')
        result = self.publish_run()
        self.assertEqual(result.returncode, 0, result.stderr)
        page = self.read_page()
        self.assertIn("&lt;script&gt;", page)
        self.assertNotIn("<script>", page)

    def test_no_data_exits_nonzero(self):
        """An empty data dir must fail with a clear error."""
        empty = self.tmp / "empty"
        empty.mkdir()
        self.write_config(data_dir=empty)
        result = self.publish_run()
        self.assertEqual(result.returncode, 1)
        self.assertIn("No data found", result.stderr)

    def test_build_context_rejects_empty_data(self):
        """build_context raises ValueError when no estimate can be made."""
        config = kia_common.load_config(self.config_path)
        with self.assertRaises(ValueError):
            publish.build_context(config, [], {}, "graph.png")


if __name__ == "__main__":
    unittest.main()
