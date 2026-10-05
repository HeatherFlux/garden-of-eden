"""Named schedule presets: the built-in grow-stage presets plus any custom ones
saved from the web UI or the REST API (issue #32).

A preset is a whole week of light windows and pump runs in the same per-day
shape the schedule itself uses, so applying one is just copying its days into
the live schedule and recompiling the crontab. The built-ins live here (one
window and one set of pump runs applied to every day) and can't be changed or
deleted; custom presets are persisted as JSON at ``config.PRESETS_FILE``.

    {
      "name": "tomatoes",
      "lights": {"days": {"mon": [{"onTime": "06:00", "offTime": "22:00",
                                   "brightness": 80, "rampMinutes": 20}], ...}},
      "pump":   {"days": {"mon": [{"time": "08:00", "duration": 5}], ...}}
    }

Vacation mode is not a preset: it is a flag on the schedule that overrides
whatever week is loaded, and stays untouched when a preset is applied.
"""

import json
import re

import config
from app.lib.persist import write_json_atomic
from app.sensors.schedule import schedule as sched

MAX_NAME = 40
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _-]*$")

# One light window + pump runs, expanded to all seven days on load. Same values
# the web UI shipped as hardcoded chips before presets moved server side.
BUILTIN_PRESETS = {
    "germinate": {
        "light": {"onTime": "06:00", "offTime": "22:00", "brightness": 30, "rampMinutes": 15},
        "pump": [
            {"time": "06:00", "duration": 3},
            {"time": "12:00", "duration": 3},
            {"time": "18:00", "duration": 3},
        ],
    },
    "seedling": {
        "light": {"onTime": "06:00", "offTime": "22:00", "brightness": 50, "rampMinutes": 15},
        "pump": [
            {"time": "08:00", "duration": 4},
            {"time": "16:00", "duration": 4},
            {"time": "00:00", "duration": 4},
        ],
    },
    "vegetative": {
        "light": {"onTime": "06:00", "offTime": "22:00", "brightness": 80, "rampMinutes": 20},
        "pump": [
            {"time": "08:00", "duration": 5},
            {"time": "16:00", "duration": 5},
            {"time": "00:00", "duration": 5},
        ],
    },
    "flower": {
        "light": {"onTime": "07:00", "offTime": "19:00", "brightness": 100, "rampMinutes": 20},
        "pump": [
            {"time": "08:00", "duration": 5},
            {"time": "14:00", "duration": 5},
            {"time": "20:00", "duration": 5},
        ],
    },
    # Run the garden overnight: lights 11pm-7am (daytime sun does the rest) and
    # watering while you sleep. The window crosses midnight on purpose.
    "night": {
        "light": {"onTime": "23:00", "offTime": "07:00", "brightness": 80, "rampMinutes": 20},
        "pump": [
            {"time": "23:15", "duration": 5},
            {"time": "02:00", "duration": 5},
            {"time": "05:00", "duration": 5},
        ],
    },
}

# Name Home Assistant's preset select shows when the live schedule matches no
# preset (hand-edited week). Selecting it is a no-op.
CUSTOM = "custom"


def clean_name(name):
    """Trim and validate a preset name, or raise ValueError."""
    name = " ".join(str(name or "").split())
    if not name or len(name) > MAX_NAME or not _NAME_RE.match(name):
        raise ValueError(
            f"invalid preset name {name!r}: 1-{MAX_NAME} letters, digits, spaces, - or _"
        )
    if name.lower() == CUSTOM:
        raise ValueError(f"{CUSTOM!r} is reserved")
    return name


def _light_entry(entry):
    return {
        "onTime": str(entry.get("onTime", "08:00")),
        "offTime": str(entry.get("offTime", "22:00")),
        "brightness": int(entry.get("brightness", 70)),
        "rampMinutes": int(entry.get("rampMinutes", 0) or 0),
    }


def _pump_entry(entry):
    return {"time": str(entry.get("time", "12:00")), "duration": int(entry.get("duration", 5))}


def canonical_days(lights_days, pump_days):
    """Return (lights, pump) day maps with every entry reduced to its known keys
    and numeric fields coerced, so two weeks that mean the same thing compare
    equal regardless of which client wrote them."""
    norm = sched.normalize_schedule({"lights": {"days": lights_days}, "pump": {"days": pump_days}})
    lights = {d: [_light_entry(e) for e in norm["lights"]["days"][d]] for d in sched.DAYS}
    pump = {d: [_pump_entry(e) for e in norm["pump"]["days"][d]] for d in sched.DAYS}
    return lights, pump


def normalize_preset(data, builtin=False):
    """Coerce a preset payload into the stored shape, validating the name and
    compiling the week once so a bad time/duration is rejected up front."""
    data = data or {}
    name = clean_name(data.get("name"))
    lights, pump = canonical_days(
        (data.get("lights") or {}).get("days"), (data.get("pump") or {}).get("days")
    )
    # Reuse the cron compiler as the validator (raises ValueError on bad input).
    sched.build_cron_lines(
        {"lights": {"enabled": True, "days": lights}, "pump": {"enabled": True, "days": pump}}
    )
    return {
        "name": name,
        "builtin": bool(builtin),
        "lights": {"days": lights},
        "pump": {"days": pump},
    }


def _expand_builtin(name, spec):
    days_light = {d: [dict(spec["light"])] for d in sched.DAYS}
    days_pump = {d: [dict(r) for r in spec["pump"]] for d in sched.DAYS}
    return normalize_preset(
        {"name": name, "lights": {"days": days_light}, "pump": {"days": days_pump}}, builtin=True
    )


def builtin_presets():
    return [_expand_builtin(name, spec) for name, spec in BUILTIN_PRESETS.items()]


def is_builtin(name):
    return str(name or "").strip().lower() in BUILTIN_PRESETS


def _save_custom(presets):
    write_json_atomic(config.PRESETS_FILE, presets)


def load_custom():
    """Custom presets from disk (normalized). Entries that fail validation or
    shadow a built-in name are dropped so a hand-edited file can't wedge the UI."""
    try:
        with open(config.PRESETS_FILE) as fh:
            raw = json.load(fh)
    except (FileNotFoundError, ValueError):
        return []
    if not isinstance(raw, list):
        return []
    out, seen = [], set()
    for item in raw:
        try:
            preset = normalize_preset(item)
        except (ValueError, AttributeError, TypeError):
            continue
        key = preset["name"].lower()
        if key in BUILTIN_PRESETS or key in seen:
            continue
        seen.add(key)
        out.append(preset)
    return out


def load_presets():
    """Built-ins first, then custom presets in saved order."""
    return builtin_presets() + load_custom()


def get_preset(name):
    """Look a preset up by name (case-insensitive); None if unknown."""
    key = str(name or "").strip().lower()
    for preset in load_presets():
        if preset["name"].lower() == key:
            return preset
    return None


def save_preset(data):
    """Create or replace a custom preset. Built-in names are refused."""
    preset = normalize_preset(data)
    if is_builtin(preset["name"]):
        raise ValueError(f"{preset['name']!r} is a built-in preset; pick another name")
    custom = [p for p in load_custom() if p["name"].lower() != preset["name"].lower()]
    custom.append(preset)
    _save_custom(custom)
    return preset


def delete_preset(name):
    """Remove a custom preset. KeyError if unknown, ValueError for a built-in."""
    if is_builtin(name):
        raise ValueError(f"{name!r} is a built-in preset and can't be deleted")
    key = str(name or "").strip().lower()
    custom = load_custom()
    remaining = [p for p in custom if p["name"].lower() != key]
    if len(remaining) == len(custom):
        raise KeyError(name)
    _save_custom(remaining)


def preset_schedule(preset, schedule=None):
    """Build the schedule that results from loading ``preset``: its week copied
    in with lights and pump enabled, vacation carried over from ``schedule``."""
    base = sched.normalize_schedule(schedule if schedule is not None else sched.load_schedule())
    return {
        "lights": {
            "enabled": True,
            "days": {d: list(preset["lights"]["days"][d]) for d in sched.DAYS},
        },
        "pump": {"enabled": True, "days": {d: list(preset["pump"]["days"][d]) for d in sched.DAYS}},
        "vacation": base["vacation"],
    }


def apply_preset(name):
    """Load a preset into the live schedule and rewrite the crontab.
    KeyError if the preset doesn't exist."""
    preset = get_preset(name)
    if preset is None:
        raise KeyError(name)
    return sched.apply_schedule(preset_schedule(preset))


def active_preset(schedule, presets=None):
    """Name of the preset whose week equals ``schedule``'s, else None."""
    schedule = sched.normalize_schedule(schedule)
    lights, pump = canonical_days(schedule["lights"]["days"], schedule["pump"]["days"])
    for preset in presets if presets is not None else load_presets():
        if preset["lights"]["days"] == lights and preset["pump"]["days"] == pump:
            return preset["name"]
    return None
