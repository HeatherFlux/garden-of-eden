"""Capture stills from the Gardyn USB cameras via fswebcam.

Shared by the REST camera endpoints and the MQTT image publisher so capture
behavior lives in one place.
"""

import logging
import subprocess

import config

logger = logging.getLogger(__name__)


def capture(device, output_path, resolution=None):
    """Capture a single frame from ``device`` to ``output_path``.

    Returns the output path on success, or raises CalledProcessError/OSError.
    """
    resolution = resolution or config.CAMERA_RESOLUTION
    cmd = [
        "fswebcam",
        "-d",
        device,
        "--no-banner",
        "-r",
        resolution,
        "-S",
        "2",  # skip initial frames so exposure settles
        output_path,
    ]
    logger.info("Capturing image from %s -> %s", device, output_path)
    subprocess.run(cmd, capture_output=True, check=True)
    return output_path


def capture_upper():
    return capture(config.UPPER_CAMERA_DEVICE, config.UPPER_IMAGE_PATH)


def capture_lower():
    return capture(config.LOWER_CAMERA_DEVICE, config.LOWER_IMAGE_PATH)
