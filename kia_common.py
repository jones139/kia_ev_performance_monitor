"""Shared helpers for the Kia EV data collection and range analysis scripts.

This module centralises everything used by both collect.py (the twice-daily
data collector) and analyze.py (the range analysis / graphing script):

  * loading the TOML configuration file (config.toml)
  * loading the JSON credentials file (credentials.json), which is kept
    separate from code and configuration for security
  * connecting to the Kia Connect API and refreshing vehicle state
  * resolving the usable battery capacity (config value vs. API value)
  * small unit / path / timestamp helpers

Public interface (functions):
    load_config(path=None)                    -> dict
    load_credentials(path=None)               -> dict
    get_timezone(config)                      -> datetime.tzinfo
    paths_from_config(config)                 -> dict[str, Path]
    refresh_vehicles(vm, retries, delay)      -> None
    select_vehicle(vm, vin="")                -> (str, Vehicle)
    resolve_battery_capacity_kwh(vehicle, config) -> (float, str, str)
    normalise_distance_km(value, unit)        -> float | None
    vehicle_data_timestamp(vehicle)           -> str

The Kia API library (hyundai_kia_connect_api) is imported lazily inside
connect_vehicle_manager() so that analyze.py can run without it installed.
"""

from __future__ import annotations

import json
import logging
import os
import stat
import time
import tomllib
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

logger = logging.getLogger("kia")

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = SCRIPT_DIR / "config.toml"
DEFAULT_CREDENTIALS_PATH = SCRIPT_DIR / "credentials.json"

# Built-in defaults, mirroring config.toml.  Any key missing from the user's
# config file falls back to the value here, so config.toml may be minimal.
DEFAULTS: dict = {
    "vehicle": {
        "vin": "",
    },
    "paths": {
        "data_dir": "data",
        "samples_file": "samples.csv",
        "daily_file": "daily.csv",
        "graph_file": "range_analysis.png",
    },
    "battery": {
        "capacity_kwh": 71.2,
        "capacity_source": "auto",
        "api_capacity_kwh_per_unit": 1.0 / 3600.0,
        "capacity_sanity_tolerance": 0.25,
    },
    "schedule": {
        "morning_cutoff_hour": 12,
        "timezone": "",
    },
    "api": {
        "retry_count": 2,
        "retry_delay_seconds": 30,
    },
    "publish": {
        "output_dir": "publish",
        "title": "Kia EV range report",
        "recent_days": 14,
    },
    "analysis": {
        "rolling_window_days": 7,
        "output_units": "km",
        # Method 2 scales the API's kWh by this factor before computing
        # range.  "auto" derives it from the energy accounting (the API's
        # daily kWh are consistently lower than the battery energy implied
        # by the % drop, because the car meters motor-side energy and omits
        # drivetrain losses).  A number >= 1 forces a fixed factor.
        "api_energy_correction": "auto",
        "plan_max_pct": 100,
        "plan_min_pct": 20,
        "min_pct_delta": 2,
        "min_distance_km": 1.0,
        "min_kwh_used": 0.2,
        "capacity_check_min_pct_delta": 5,
        "min_range_km": 150,      # plausibility band for full-charge range
        "max_range_km": 500,      # estimates (rejects cross-source glitches)
    },
}

# Miles -> kilometres conversion factor (the API may report either unit).
MILES_TO_KM = 1.609344


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge ``override`` into a copy of ``base``.

    Interface: takes two (possibly nested) dicts and returns a new dict in
    which values from ``override`` win; nested dicts are merged key-by-key
    rather than replaced wholesale.  Inputs are not modified.
    """
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config(path: str | Path | None = None) -> dict:
    """Load the TOML configuration file, merged over the built-in defaults.

    Interface: ``path`` is optional (default: config.toml next to this file).
    Returns a nested dict with sections [vehicle], [paths], [battery],
    [schedule], [api], [analysis].  Keys absent from the file take their
    values from DEFAULTS.  Raises FileNotFoundError / TOMLDecodeError on a
    missing or malformed file.
    """
    config_path = Path(path) if path else DEFAULT_CONFIG_PATH
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")
    with open(config_path, "rb") as handle:
        user_config = tomllib.load(handle)
    return _deep_merge(DEFAULTS, user_config)


def load_credentials(path: str | Path | None = None) -> dict:
    """Load the Kia Connect credentials from a JSON file.

    Interface: ``path`` is optional (default: credentials.json next to this
    file).  Returns a dict with keys region, brand, username, password and
    pin (pin defaults to "").  Raises FileNotFoundError if the file is
    missing, or ValueError if a required key is absent.  Logs a warning if
    the file is readable by other users (suggests ``chmod 600``).
    """
    cred_path = Path(path) if path else DEFAULT_CREDENTIALS_PATH
    if not cred_path.exists():
        raise FileNotFoundError(
            f"Credentials file not found: {cred_path}\n"
            f"Copy credentials.example.json to {cred_path.name} and fill it in."
        )
    with open(cred_path, "r", encoding="utf-8") as handle:
        credentials = json.load(handle)
    for required in ("region", "brand", "username", "password"):
        if required not in credentials:
            raise ValueError(f"credentials file is missing required key: {required}")
    credentials.setdefault("pin", "")
    # Security check: warn if group/other permissions are set.
    mode = stat.S_IMODE(os.stat(cred_path).st_mode)
    if mode & 0o077:
        logger.warning(
            "%s is readable by other users (mode %03o). Run: chmod 600 %s",
            cred_path, mode, cred_path,
        )
    return credentials


def connect_vehicle_manager(credentials: dict):
    """Create a Kia Connect VehicleManager from the given credentials.

    Interface: takes the dict returned by load_credentials() and returns an
    unauthenticated hyundai_kia_connect_api.VehicleManager instance.  The
    API library is imported here (lazily) so that scripts which only read
    CSV data do not need it installed.
    """
    from hyundai_kia_connect_api import VehicleManager

    return VehicleManager(
        region=int(credentials["region"]),
        brand=int(credentials["brand"]),
        username=credentials["username"],
        password=credentials["password"],
        pin=credentials["pin"],
    )


def get_timezone(config: dict):
    """Return the tzinfo used to interpret 'the day' for samples.

    Interface: takes the dict from load_config(); returns a datetime.tzinfo.
    Uses [schedule].timezone (an IANA name such as "Europe/London") when set,
    otherwise the server's local timezone.
    """
    tz_name = config["schedule"].get("timezone", "")
    if tz_name:
        return ZoneInfo(tz_name)
    return datetime.now().astimezone().tzinfo


def paths_from_config(config: dict) -> dict[str, Path]:
    """Resolve the data file locations from the [paths] config section.

    Interface: takes the dict from load_config(); returns a dict with keys
    "data_dir", "samples", "daily" and "graph", each a pathlib.Path.
    Relative paths are resolved against the directory containing this file.
    """
    paths_cfg = config["paths"]
    data_dir = Path(paths_cfg["data_dir"])
    if not data_dir.is_absolute():
        data_dir = SCRIPT_DIR / data_dir
    return {
        "data_dir": data_dir,
        "samples": data_dir / paths_cfg["samples_file"],
        "daily": data_dir / paths_cfg["daily_file"],
        "graph": data_dir / paths_cfg["graph_file"],
    }


def refresh_vehicles(vm, retries: int = 2, delay_seconds: float = 30) -> None:
    """Refresh tokens and vehicle state, retrying transient API failures.

    Interface: takes a VehicleManager (from connect_vehicle_manager), the
    number of EXTRA attempts (``retries``) and the pause between attempts.
    Calls check_and_refresh_token() then update_all_vehicles_with_cached_state().
    Raises the last exception if every attempt fails.
    """
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            vm.check_and_refresh_token()
            vm.update_all_vehicles_with_cached_state()
            return
        except Exception as error:  # noqa: BLE001 - API raises assorted errors
            last_error = error
            if attempt < retries:
                logger.warning(
                    "Kia API attempt %d/%d failed (%s); retrying in %ss",
                    attempt + 1, retries + 1, error, delay_seconds,
                )
                time.sleep(delay_seconds)
    raise RuntimeError(f"Kia API failed after {retries + 1} attempts") from last_error


def select_vehicle(vm, vin: str = ""):
    """Pick the vehicle to monitor from a refreshed VehicleManager.

    Interface: takes the VehicleManager and an optional VIN (or API vehicle
    id) from [vehicle].vin in the config.  With an empty ``vin`` the first
    vehicle is used.  Returns a (vehicle_id, Vehicle) tuple.  Raises
    ValueError if no vehicles are present or the VIN cannot be matched.
    """
    if not vm.vehicles:
        raise ValueError("No vehicles returned by the Kia Connect API")
    if vin:
        for vehicle_id, vehicle in vm.vehicles.items():
            if vin in (vehicle_id, getattr(vehicle, "VIN", None),
                       getattr(vehicle, "id", None)):
                return vehicle_id, vehicle
        raise ValueError(f"No vehicle matches VIN/id '{vin}'")
    return next(iter(vm.vehicles.items()))


def resolve_battery_capacity_kwh(
    vehicle, config: dict
) -> tuple[float, str, str]:
    """Determine the usable battery capacity (kWh) for range estimates.

    Interface: takes a Vehicle object (may be None, e.g. when analyzing
    offline) and the dict from load_config().  Returns a tuple of
    (capacity_kwh, source_code, description) where source_code is "api" or
    "config" and description is a human-readable explanation for logging.

    Behaviour is controlled by [battery].capacity_source:
      "api"    - use vehicle.ev_battery_capacity scaled by
                 api_capacity_kwh_per_unit (the raw value is undocumented;
                 for the PV5 it appears to be kilojoules, hence 1/3600).
      "config" - always use [battery].capacity_kwh.
      "auto"   - use the API value when present and within
                 capacity_sanity_tolerance of the config value, otherwise
                 fall back to the config value (with a warning).
    """
    battery_cfg = config["battery"]
    config_kwh = float(battery_cfg["capacity_kwh"])
    source_mode = str(battery_cfg["capacity_source"]).lower()
    tolerance = float(battery_cfg["capacity_sanity_tolerance"])
    scale = float(battery_cfg["api_capacity_kwh_per_unit"])

    api_raw = getattr(vehicle, "ev_battery_capacity", None) if vehicle else None
    api_kwh = float(api_raw) * scale if api_raw else None

    if source_mode == "config" or api_kwh is None:
        note = "" if source_mode == "config" else " (API value unavailable)"
        return config_kwh, "config", f"config value {config_kwh:g} kWh{note}"

    if source_mode == "api":
        return api_kwh, "api", f"API value raw {api_raw} x {scale:g} = {api_kwh:g} kWh"

    # "auto" mode: sanity-check the API value against the config value.
    if abs(api_kwh - config_kwh) <= tolerance * config_kwh:
        return (
            api_kwh,
            "api",
            f"API value raw {api_raw} x {scale:g} = {api_kwh:g} kWh "
            f"(within {tolerance:.0%} of config value {config_kwh:g})",
        )
    logger.warning(
        "API battery capacity (raw %s -> %.2f kWh) differs from config "
        "value %.2f kWh by more than %.0f%%; using config value",
        api_raw, api_kwh, config_kwh, tolerance * 100,
    )
    return (
        config_kwh,
        "config",
        f"config value {config_kwh:g} kWh (API value raw {api_raw} -> "
        f"{api_kwh:g} kWh rejected by sanity check)",
    )


def normalise_distance_km(value: float | None, unit: str | None) -> float | None:
    """Convert an API distance to kilometres.

    Interface: takes a numeric distance (or None) and the API unit string
    ("km" or "mi"); returns the distance in km as a float, or None when the
    input is None.  Unknown units are assumed to be km (the API default).
    """
    if value is None:
        return None
    if unit and str(unit).lower() in ("mi", "mile", "miles"):
        return float(value) * MILES_TO_KM
    return float(value)


def vehicle_data_timestamp(vehicle) -> str:
    """Return an ISO timestamp describing how fresh the vehicle data is.

    Interface: takes a Vehicle object and returns the vehicle's own
    last-updated time as an ISO string, or "" when unavailable.  Used in
    the samples CSV to spot stale cached vehicle state.
    """
    for attribute in ("last_updated_at", "_last_updated_at"):
        value = getattr(vehicle, attribute, None)
        if value is not None:
            return str(value)
    return ""
