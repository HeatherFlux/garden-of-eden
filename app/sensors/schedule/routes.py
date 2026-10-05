import logging

from flask import Blueprint, jsonify, request

from app.lib import presets as presets_lib

from . import schedule as sched

logger = logging.getLogger(__name__)

schedule_blueprint = Blueprint("schedule", __name__)


@schedule_blueprint.route("", methods=["GET"])
def get_schedule():
    return jsonify(sched.load_schedule())


@schedule_blueprint.route("", methods=["POST"])
def set_schedule():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(error="expected a JSON schedule object"), 400
    try:
        applied = sched.apply_schedule(data)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    except FileNotFoundError:
        # crontab binary missing (e.g. off-Pi) — schedule is still saved.
        return jsonify(error="crontab not available on this host"), 503
    return jsonify(applied)


# --- Presets: built-in stage presets + custom ones saved from the UI/API ---


@schedule_blueprint.route("/presets", methods=["GET"])
def list_presets():
    presets = presets_lib.load_presets()
    active = presets_lib.active_preset(sched.load_schedule(), presets)
    return jsonify(presets=presets, active=active)


@schedule_blueprint.route("/presets", methods=["POST"])
def save_preset():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(error="expected a JSON preset object"), 400
    try:
        return jsonify(presets_lib.save_preset(data))
    except ValueError as exc:
        return jsonify(error=str(exc)), 400


@schedule_blueprint.route("/presets/<name>", methods=["GET"])
def get_preset(name):
    preset = presets_lib.get_preset(name)
    if preset is None:
        return jsonify(error=f"no preset named {name!r}"), 404
    return jsonify(preset)


@schedule_blueprint.route("/presets/<name>", methods=["DELETE"])
def delete_preset(name):
    try:
        presets_lib.delete_preset(name)
    except KeyError:
        return jsonify(error=f"no preset named {name!r}"), 404
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    return jsonify(status="deleted", name=name)


@schedule_blueprint.route("/presets/<name>/apply", methods=["POST"])
def apply_preset(name):
    """Load the preset into the live schedule (lights + pump enabled) and
    rewrite the crontab. Vacation mode is left as it was."""
    try:
        applied = presets_lib.apply_preset(name)
    except KeyError:
        return jsonify(error=f"no preset named {name!r}"), 404
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    except FileNotFoundError:
        return jsonify(error="crontab not available on this host"), 503
    return jsonify(applied)
