"""Water-level helpers shared by the MQTT service and REST API.

The distance sensor reports the gap (cm) from the sensor down to the water
surface, so a *larger* distance means a *lower* tank.
"""


def is_water_low(distance_cm, threshold_cm):
    """Return True if the tank is low.

    ``threshold_cm`` of ``None``/0 means alerting is disabled -> never low.
    A ``distance_cm`` of ``None`` (failed reading) is treated as "not low" so a
    sensor glitch doesn't raise a false alarm.
    """
    if not threshold_cm:
        return False
    if distance_cm is None:
        return False
    return distance_cm > threshold_cm
