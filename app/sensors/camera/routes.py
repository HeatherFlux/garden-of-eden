import logging

from flask import Blueprint, jsonify, send_file

import config

from . import camera

logger = logging.getLogger(__name__)

camera_blueprint = Blueprint("camera", __name__)


def _serve(capture_fn, path):
    try:
        capture_fn()
    except FileNotFoundError:
        return jsonify(error="fswebcam not installed on this host"), 503
    except Exception as exc:
        logger.error("Camera capture failed: %s", exc)
        return jsonify(error=f"camera capture failed: {exc}"), 503
    return send_file(path, mimetype="image/jpeg")


@camera_blueprint.route("/upper", methods=["GET"])
def upper():
    return _serve(camera.capture_upper, config.UPPER_IMAGE_PATH)


@camera_blueprint.route("/lower", methods=["GET"])
def lower():
    return _serve(camera.capture_lower, config.LOWER_IMAGE_PATH)
