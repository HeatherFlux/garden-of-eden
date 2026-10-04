import logging

from flask import Blueprint, jsonify, request

import config
from app.lib.hardware import get_pin_factory
from app.lib.lib import check_sensor_guard
from app.lib.water import gallons_remaining

from .distance import Distance as DistanceControl

logger = logging.getLogger(__name__)

distance_blueprint = Blueprint("distance", __name__)

try:
    distance_control = DistanceControl(pin_factory=get_pin_factory())
except Exception as exc:
    logger.error("Failed to initialize Distance: %s", exc)
    distance_control = None

check_sensor = check_sensor_guard(sensor=distance_control, sensor_name="Distance")


@distance_blueprint.route("", methods=["GET"])
@distance_blueprint.route("/measure", methods=["GET"])
@check_sensor
def get_distance():
    # ?avg=1 takes the median of several pings: slower, but steady enough to
    # calibrate tank geometry (e.g. cm per quart added) from the web UI.
    if request.args.get("avg") in ("1", "true", "yes"):
        distance_value = distance_control.measure()
    else:
        distance_value = distance_control.measure_once()
    gallons = gallons_remaining(
        distance_value,
        config.WATER_FULL_CM,
        config.WATER_EMPTY_CM,
        config.TANK_CAPACITY_GALLONS,
    )
    return jsonify(distance=distance_value, gallons=gallons), 200
