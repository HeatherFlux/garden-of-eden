"""Lights/pump scheduling that survives reboots and PC shutdown by writing the
Pi's crontab (issue #32).

The schedule is persisted as JSON and compiled into crontab lines tagged with a
marker comment so we can rewrite only our own entries. Cron invokes the
``light`` and ``water`` CLI symlinks installed by bin/setup.sh.
"""

import json
import logging
import subprocess

import config

logger = logging.getLogger(__name__)

CRON_MARKER = "# garden-of-eden"
LIGHT_CMD = "/usr/local/bin/light"
WATER_CMD = "/usr/local/bin/water"

DEFAULT_SCHEDULE = {
    "lights": {"enabled": False, "onTime": "08:00", "offTime": "22:00", "brightness": 70},
    "pump": {"enabled": False, "runs": []},
}


def load_schedule():
    """Return the saved schedule, or defaults if none exists."""
    try:
        with open(config.SCHEDULE_FILE) as fh:
            return json.load(fh)
    except (FileNotFoundError, ValueError):
        return dict(DEFAULT_SCHEDULE)


def save_schedule(schedule):
    with open(config.SCHEDULE_FILE, "w") as fh:
        json.dump(schedule, fh, indent=2)


def _hh_mm(value):
    """Parse "HH:MM" -> (minute, hour) for cron, or raise ValueError."""
    hour, minute = value.split(":")
    h, m = int(hour), int(minute)
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ValueError(f"invalid time {value!r}")
    return m, h


def build_cron_lines(schedule):
    """Compile a schedule dict into a list of (marked) crontab lines."""
    lines = []
    lights = schedule.get("lights", {})
    if lights.get("enabled"):
        brightness = int(lights.get("brightness", 70))
        on_m, on_h = _hh_mm(lights.get("onTime", "08:00"))
        off_m, off_h = _hh_mm(lights.get("offTime", "22:00"))
        lines.append(
            f"{on_m} {on_h} * * * {LIGHT_CMD} --on --brightness {brightness} {CRON_MARKER}"
        )
        lines.append(f"{off_m} {off_h} * * * {LIGHT_CMD} --off {CRON_MARKER}")

    pump = schedule.get("pump", {})
    if pump.get("enabled"):
        for run in pump.get("runs", []):
            run_m, run_h = _hh_mm(run.get("time", "12:00"))
            duration = int(run.get("duration", 5)) * 60  # minutes -> seconds
            lines.append(f"{run_m} {run_h} * * * {WATER_CMD} {duration} {CRON_MARKER}")

    return lines


def _read_crontab():
    result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    if result.returncode != 0:
        return []
    return result.stdout.splitlines()


def _write_crontab(lines):
    payload = "\n".join(lines) + "\n"
    subprocess.run(["crontab", "-"], input=payload, text=True, check=True)


def apply_schedule(schedule):
    """Persist the schedule and replace our crontab entries with its compiled form."""
    # Validate/compile first so a bad schedule never touches crontab.
    cron_lines = build_cron_lines(schedule)
    save_schedule(schedule)

    existing = [ln for ln in _read_crontab() if CRON_MARKER not in ln]
    _write_crontab(existing + cron_lines)
    logger.info("Applied schedule with %d cron entries", len(cron_lines))
    return schedule
