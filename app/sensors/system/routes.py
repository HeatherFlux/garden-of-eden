from flask import Blueprint, jsonify

import config
from app.lib.hardware import detect_model

system_blueprint = Blueprint("system", __name__)


@system_blueprint.route("", methods=["GET"])
def get_system():
    """Report identity, version, and the detected hardware model/profile."""
    model = detect_model()
    return jsonify(
        {
            "identifier": config.IDENTIFIER,
            "version": config.VERSION,
            "model": model,
            "profile": config.MODELS.get(model, {}),
            "sensor_type": config.SENSOR_TYPE,
        }
    )
