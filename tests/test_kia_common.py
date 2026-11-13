"""Unit tests for kia_common.py (config, credentials, capacity, units).

Run from the repository root with:
    venv/bin/python -m unittest discover -s tests -v
or directly:
    venv/bin/python tests/test_kia_common.py
All tests are offline and filesystem-only (temp dirs).
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

try:
    import support  # noqa: F401  (sets up sys.path)
except ImportError:  # loaded as tests.test_x package member
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import support  # noqa: E402,F401

import kia_common


def config_with(**sections) -> dict:
    """Return the built-in defaults merged with the given section overrides."""
    return kia_common._deep_merge(kia_common.DEFAULTS, sections)


class DeepMergeTest(unittest.TestCase):
    """Tests for kia_common._deep_merge nesting behaviour."""

    def test_nested_override_keeps_sibling_keys(self):
        """Overriding one nested key must preserve the other defaults."""
        merged = config_with(battery={"capacity_kwh": 60.0})
        self.assertEqual(merged["battery"]["capacity_kwh"], 60.0)
        self.assertEqual(
            merged["battery"]["capacity_source"],
            kia_common.DEFAULTS["battery"]["capacity_source"],
        )
        self.assertEqual(merged["analysis"]["rolling_window_days"],
                         kia_common.DEFAULTS["analysis"]["rolling_window_days"])

    def test_inputs_not_modified(self):
        """Neither input dict may be mutated by the merge."""
        base = {"a": {"b": 1}}
        override = {"a": {"c": 2}}
        kia_common._deep_merge(base, override)
        self.assertEqual(base, {"a": {"b": 1}})
        self.assertEqual(override, {"a": {"c": 2}})


class LoadConfigTest(unittest.TestCase):
    """Tests for kia_common.load_config (TOML loading + default filling)."""

    def test_minimal_file_gets_defaults(self):
        """A file with one key still yields every section and default."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text('[battery]\ncapacity_kwh = 64.0\n', encoding="utf-8")
            config = kia_common.load_config(path)
        self.assertEqual(config["battery"]["capacity_kwh"], 64.0)
        for section in ("vehicle", "paths", "schedule", "api", "analysis"):
            self.assertIn(section, config)
        self.assertEqual(config["analysis"]["plan_min_pct"], 20)

    def test_missing_file_raises(self):
        """A non-existent config path must raise FileNotFoundError."""
        with self.assertRaises(FileNotFoundError):
            kia_common.load_config("/nonexistent/config.toml")


class LoadCredentialsTest(unittest.TestCase):
    """Tests for kia_common.load_credentials (JSON + permissions warning)."""

    def write_creds(self, tmp, perms=0o600, **payload):
        """Write a credentials JSON file with the given mode and keys."""
        data = {"region": 1, "brand": 1, "username": "u", "password": "p"}
        data.update(payload)
        path = Path(tmp) / "credentials.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        os.chmod(path, perms)
        return path

    def test_loads_and_defaults_pin(self):
        """Valid file loads; a missing 'pin' key defaults to ""."""
        with tempfile.TemporaryDirectory() as tmp:
            creds = kia_common.load_credentials(self.write_creds(tmp))
        self.assertEqual(creds["username"], "u")
        self.assertEqual(creds["pin"], "")

    def test_missing_key_raises_value_error(self):
        """Omitting a required key must raise ValueError."""
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_creds(tmp)
            data = json.loads(path.read_text())
            del data["password"]
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(ValueError):
                kia_common.load_credentials(path)

    def test_missing_file_raises(self):
        """A non-existent credentials path must raise FileNotFoundError."""
        with self.assertRaises(FileNotFoundError):
            kia_common.load_credentials("/nonexistent/credentials.json")

    def test_world_readable_file_warns(self):
        """Mode 0644 credentials must trigger a warning log."""
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_creds(tmp, perms=0o644)
            with self.assertLogs(kia_common.logger, level="WARNING"):
                kia_common.load_credentials(path)


class TimezoneTest(unittest.TestCase):
    """Tests for kia_common.get_timezone selection rules."""

    def test_configured_timezone_used(self):
        """[schedule].timezone must return the matching ZoneInfo."""
        tz = kia_common.get_timezone(config_with(schedule={"timezone": "Europe/London"}))
        self.assertIsInstance(tz, ZoneInfo)
        self.assertEqual(tz.key, "Europe/London")

    def test_empty_timezone_falls_back_to_local(self):
        """An empty [schedule].timezone must yield the server's local tz."""
        tz = kia_common.get_timezone(config_with())
        self.assertEqual(tz, datetime.now().astimezone().tzinfo)


class PathsTest(unittest.TestCase):
    """Tests for kia_common.paths_from_config resolution rules."""

    def test_relative_paths_resolve_under_repo(self):
        """Relative [paths] entries resolve relative to the repo root."""
        paths = kia_common.paths_from_config(config_with())
        self.assertEqual(paths["data_dir"], kia_common.SCRIPT_DIR / "data")
        self.assertEqual(paths["samples"].name, "samples.csv")

    def test_absolute_paths_kept(self):
        """Absolute [paths] entries must be used verbatim."""
        paths = kia_common.paths_from_config(
            config_with(paths={"data_dir": "/srv/kia", "samples_file": "s.csv",
                               "daily_file": "d.csv", "graph_file": "g.png"})
        )
        self.assertEqual(paths["samples"], Path("/srv/kia/s.csv"))


class DistanceTest(unittest.TestCase):
    """Tests for kia_common.normalise_distance_km unit conversion."""

    def test_none_and_km_and_miles(self):
        """None passes through; km unchanged; mi converted by 1.609344."""
        self.assertIsNone(kia_common.normalise_distance_km(None, "km"))
        self.assertEqual(kia_common.normalise_distance_km(10, "km"), 10.0)
        self.assertAlmostEqual(kia_common.normalise_distance_km(10, "mi"),
                               16.09344, places=5)
        self.assertEqual(kia_common.normalise_distance_km(10, None), 10.0)


class CapacityResolutionTest(unittest.TestCase):
    """Tests for kia_common.resolve_battery_capacity_kwh source rules."""

    def vehicle(self, raw=256320):
        """Minimal vehicle stand-in exposing only ev_battery_capacity."""
        return SimpleNamespace(ev_battery_capacity=raw)

    def test_auto_accepts_sane_api_value(self):
        """Raw 256320 (kilojoules) must resolve to ~71.2 kWh from the API."""
        kwh, source, desc = kia_common.resolve_battery_capacity_kwh(
            self.vehicle(), config_with()
        )
        self.assertEqual(source, "api")
        self.assertAlmostEqual(kwh, 71.2, places=1)
        self.assertIn("71.2", desc)

    def test_auto_rejects_nonsense_api_value(self):
        """An API value far from config must fall back to the config value."""
        with self.assertLogs(kia_common.logger, level="WARNING"):
            kwh, source, _ = kia_common.resolve_battery_capacity_kwh(
                self.vehicle(raw=100), config_with()
            )
        self.assertEqual(source, "config")
        self.assertEqual(kwh, 71.2)

    def test_config_source_ignores_api(self):
        """capacity_source=config must always use the config value."""
        kwh, source, _ = kia_common.resolve_battery_capacity_kwh(
            self.vehicle(),
            config_with(battery={"capacity_source": "config", "capacity_kwh": 64.0}),
        )
        self.assertEqual((kwh, source), (64.0, "config"))

    def test_api_source_always_api(self):
        """capacity_source=api must use the scaled API value even if odd."""
        kwh, source, _ = kia_common.resolve_battery_capacity_kwh(
            self.vehicle(raw=3600),
            config_with(battery={"capacity_source": "api"}),
        )
        self.assertEqual((round(kwh, 3), source), (1.0, "api"))

    def test_no_vehicle_falls_back_to_config(self):
        """A None vehicle (offline analysis) must use the config value."""
        kwh, source, _ = kia_common.resolve_battery_capacity_kwh(None, config_with())
        self.assertEqual((kwh, source), (71.2, "config"))


class VehicleTimestampTest(unittest.TestCase):
    """Tests for kia_common.vehicle_data_timestamp attribute lookup."""

    def test_reads_last_updated_at_and_missing(self):
        """Present attribute is stringified; absent yields ""."""
        stamp = datetime(2026, 11, 13, 18, 0, 0)
        self.assertEqual(
            kia_common.vehicle_data_timestamp(SimpleNamespace(last_updated_at=stamp)),
            str(stamp),
        )
        self.assertEqual(kia_common.vehicle_data_timestamp(SimpleNamespace()), "")


if __name__ == "__main__":
    unittest.main()
