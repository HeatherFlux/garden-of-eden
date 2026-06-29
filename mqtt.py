import json
import logging
import signal
import subprocess
import sys
import threading
from threading import Timer

# import picamera
# import cv2
from time import sleep

import paho.mqtt.client as mqtt
from gpiozero import Button  # Import gpiozero Button

from app.lib import grow as grow_lib
from app.lib import state as state_lib
from app.lib.hardware import detect_model, get_pin_factory
from app.lib.logging_config import configure_logging
from app.lib.water import is_water_low
from app.sensors.distance.distance import MeasurementError
from app.sensors.distance.routes import distance_control
from app.sensors.humidity.humidity import humidity_sensor
from app.sensors.light.routes import light_control
from app.sensors.pcb_temp.pcb_temp import get_pcb_temperature
from app.sensors.pump.routes import pump_control
from app.sensors.temperature.temperature import temperature_sensor
from config import (
    BASE_TOPIC,
    BROKER,
    BUTTON_PIN,
    CAMERA_RESOLUTION,
    IDENTIFIER,
    IMAGE_INTERVAL_SECONDS,
    KEEP_ALIVE_INTERVAL,
    LOWER_CAMERA_DEVICE,
    LOWER_IMAGE_PATH,
    MAX_PUMP_RUN_SECONDS,
    MODEL,
    PASSWORD,
    PORT,
    UPPER_CAMERA_DEVICE,
    UPPER_IMAGE_PATH,
    USERNAME,
    VERSION,
    WATER_LOW_CM,
)

# Configure logging (shared config; level via LOG_LEVEL env). Use a dedicated
# file so the MQTT service and the REST API don't write the same log.
configure_logging(log_file="mqtt.log")
logger = logging.getLogger(__name__)


class MqttLogHandler(logging.Handler):
    """Publish WARNING+ log records to MQTT so Home Assistant can surface recent
    issues (exposed as the 'Last Log' sensor). Best-effort: silently skips when
    the client isn't connected yet."""

    def __init__(self, mqtt_client, topic):
        super().__init__(level=logging.WARNING)
        self._client = mqtt_client
        self._topic = topic

    def emit(self, record):
        try:
            if self._client.is_connected():
                # HA sensor states are capped at 255 characters.
                self._client.publish(self._topic, self.format(record)[:255])
        except Exception:
            pass


# Reuse the singleton drivers the route modules already created at import time.
# Importing `app` instantiates pump/light/distance on their GPIO pins; creating a
# second copy of each here raised GPIOPinInUse and crash-looped the service.
pin_factory = get_pin_factory()

pump = pump_control
light = light_control
distance_sensor = distance_control

# default on brightness
brightness = 50
speed = 100
sec_per_min = 60
min_per_hr = 60

# publish twice an hour
publish_frequency = sec_per_min * min_per_hr / 2

# Button GPIO setup using gpiozero
button_pin = BUTTON_PIN
button = Button(
    button_pin, pin_factory=pin_factory, bounce_time=0.2, hold_time=2
)  # hold_time = 2 seconds for long press detection

# Variables to track the state of the light and pump
light_state = False
pump_state = False
double_press_time = 1  # Time to detect a double press (in seconds)
press_count = 0
double_press_timer = None

# MQTT topics for device availability (LWT) and a 'last log' diagnostic feed.
AVAILABILITY_TOPIC = BASE_TOPIC + "/availability"
LOG_TOPIC = BASE_TOPIC + "/log"

# Pump safety: never let the pump run longer than the hard cap, no matter how it
# was turned on (HA command or physical button). Mirrors the REST API watchdog.
_pump_off_timer = None
_pump_timer_lock = threading.Lock()


def _safety_pump_off():
    global pump_state
    logger.warning("Pump safety cap (%ss) reached; forcing pump OFF", MAX_PUMP_RUN_SECONDS)
    try:
        pump.off()
        pump_state = False
        client.publish(BASE_TOPIC + "/pump/state", "OFF")
        state_lib.save_state(pump_on=False, speed=speed)
    except Exception as exc:
        logger.error("Safety pump-off failed: %s", exc)


def _arm_pump_safety():
    """(Re)arm the auto-off timer whenever the pump is energized."""
    global _pump_off_timer
    with _pump_timer_lock:
        if _pump_off_timer is not None:
            _pump_off_timer.cancel()
        _pump_off_timer = Timer(MAX_PUMP_RUN_SECONDS, _safety_pump_off)
        _pump_off_timer.daemon = True
        _pump_off_timer.start()


def _cancel_pump_safety():
    global _pump_off_timer
    with _pump_timer_lock:
        if _pump_off_timer is not None:
            _pump_off_timer.cancel()
            _pump_off_timer = None


# Button press callbacks
def toggle_light():
    global light_state
    light_state = not light_state
    if light_state:
        logger.info("Toggling Light ON")
        light.set_duty_cycle(brightness)
        client.publish(BASE_TOPIC + "/light/state", "ON")
    else:
        logger.info("Toggling Light OFF")
        light.off()
        client.publish(BASE_TOPIC + "/light/state", "OFF")
    state_lib.save_state(light_on=light_state, brightness=brightness)


def toggle_pump():
    global pump_state
    pump_state = not pump_state
    if pump_state:
        logger.info("Toggling Pump ON")
        pump.set_speed(speed)
        _arm_pump_safety()
        client.publish(BASE_TOPIC + "/pump/state", "ON")
    else:
        logger.info("Toggling Pump OFF")
        pump.off()
        _cancel_pump_safety()
        client.publish(BASE_TOPIC + "/pump/state", "OFF")
    state_lib.save_state(pump_on=pump_state, speed=speed)


def handle_button_press():
    global press_count, double_press_timer

    press_count += 1

    if press_count == 1:
        # Start a timer to detect if a second press occurs within the double press time window
        double_press_timer = Timer(double_press_time, handle_single_press)
        double_press_timer.start()
    elif press_count == 2:
        # If a second press occurs, cancel the single press action and trigger the double press action
        if double_press_timer:
            double_press_timer.cancel()
        handle_double_press()
        press_count = 0


def publish_button_event(event):
    """Publish a physical-button event so Home Assistant can trigger automations
    on the gardyn button (issue #78). Values: "single", "double", "long"."""
    try:
        # HA's MQTT event platform expects a JSON payload with "event_type".
        client.publish(BASE_TOPIC + "/button/event", json.dumps({"event_type": event}))
        logger.info("Published button event: %s", event)
    except Exception as exc:
        logger.error("Failed to publish button event: %s", exc)


def handle_single_press():
    global press_count
    publish_button_event("single")
    toggle_light()  # Single press toggles the light
    press_count = 0


def handle_double_press():
    publish_button_event("double")
    toggle_pump()  # Double press toggles the pump


def handle_long_press():
    # Long press is exposed to HA as an event; no local actuator change.
    publish_button_event("long")


# Set button events for press detection
button.when_pressed = handle_button_press
button.when_held = handle_long_press


# helpers
def flash_lights(times=3, delay=0.3):
    original_brightness = light.get_brightness()  # Save the brightness (0–100 scale)
    was_on = original_brightness > 0  # If >0%, we consider it "on"

    logger.info(f"Flashing lights {times} times. Original brightness: {original_brightness}%")

    for _ in range(times):
        light.off()
        sleep(delay)
        light.set_brightness(100)  # Flash full brightness for maximum visibility
        sleep(delay)
    # Restore original state
    if was_on:
        light.set_brightness(original_brightness)
    else:
        light.off()


def safe_distance_measure():
    # Reuses the shared distance driver; re-instantiating would raise
    # GPIOPinInUse against the copy the API already holds.
    if distance_sensor is None:
        return None
    try:
        return distance_sensor.measure_once()
    except MeasurementError as e:
        logger.warning(f"Distance measure failed: {e}, retrying once")
        try:
            return distance_sensor.measure_once()
        except Exception as e2:
            logger.error(f"Distance recovery failed: {e2}")
            return None


def publish_water_low_mode(client):
    if WATER_LOW_CM not in (None, 0):
        mode = "Enabled"
    else:
        mode = "Disabled"
    logger.info(f"Publishing water low mode: {mode}")
    client.publish(BASE_TOPIC + "/water/low/mode", mode, retain=True)


def update_water_low_state(client):
    if WATER_LOW_CM in (None, 0):
        # Checking disabled: report not-low.
        client.publish(BASE_TOPIC + "/water/low/state", "OFF", retain=True)
        logger.info("Water low checking disabled, setting water low state to OFF")
        return

    distance = safe_distance_measure()
    if distance is None:
        logger.warning("Could not update water low state because distance reading failed")
        return

    low = is_water_low(distance, WATER_LOW_CM)
    state = "ON" if low else "OFF"
    client.publish(BASE_TOPIC + "/water/low/state", state, retain=True)
    logger.info(
        "Updated water low state to %s (distance %.2fcm, threshold %.2fcm)",
        state,
        distance,
        WATER_LOW_CM,
    )


# https://www.home-assistant.io/integrations/mqtt/#discovery-messages
#  Note: homeassistant/<component>/[<node_id>/]<object_id>/config.
#  User device_class for auto suggestion on HA card picks
def send_discovery_messages(client):
    device_info = {
        "identifiers": [IDENTIFIER],
        "name": BASE_TOPIC,
        "manufacturer": "gardyn-of-eden",
        "model": detect_model() or MODEL,
        "sw_version": VERSION,
    }

    # Every entity shares the device availability topic so HA greys the whole
    # device out when the Pi/service is down.
    avail = {
        "availability_topic": AVAILABILITY_TOPIC,
        "payload_available": "online",
        "payload_not_available": "offline",
    }

    def pub(topic, payload):
        client.publish(topic, json.dumps({**payload, **avail}), retain=True)

    # Config for Light
    TEMP_CONFIG_TOPIC = "homeassistant/light/gardyn/" + IDENTIFIER + "_light/config"
    temp_config_payload = {
        "name": "Light",
        "unique_id": IDENTIFIER + "_light",
        "platform": "mqtt",
        "state_topic": BASE_TOPIC + "/light/state",
        "command_topic": BASE_TOPIC + "/light/command",
        "brightness_state_topic": BASE_TOPIC + "/light/brightness/state",
        "brightness_command_topic": BASE_TOPIC + "/light/brightness/set",
        "brightness_scale": 100,
        "device": device_info,
    }
    pub(TEMP_CONFIG_TOPIC, temp_config_payload)

    # Config for Pump (as a light with speed control, for example)
    # todo: maybe use fan instead....
    TEMP_CONFIG_TOPIC = "homeassistant/light/gardyn/" + IDENTIFIER + "_pump/config"
    temp_config_payload = {
        "name": "Pump",
        "unique_id": IDENTIFIER + "_pump",
        "platform": "mqtt",
        "device_class": "fan",
        "state_topic": BASE_TOPIC + "/pump/state",
        "command_topic": BASE_TOPIC + "/pump/command",
        "brightness_state_topic": BASE_TOPIC + "/pump/speed/state",
        "brightness_command_topic": BASE_TOPIC + "/pump/speed/set",
        "brightness_scale": 100,
        # if using fan....
        # "percentage_state_topic": BASE_TOPIC + "/pump/speed/state",
        # "percentage_command_topic": BASE_TOPIC + "/pump/speed/set",
        # "speed_range_min": 1,
        # "speed_range_max": 100,
        "icon": "mdi:water-pump",
        "device": device_info,
    }
    pub(TEMP_CONFIG_TOPIC, temp_config_payload)

    # Config for Temperature from PCB
    TEMP_CONFIG_TOPIC = "homeassistant/sensor/gardyn/" + IDENTIFIER + "_pcb_temp/config"
    temp_config_payload = {
        "name": "PCB Temperature",
        "unique_id": IDENTIFIER + "_pcb_temp",
        "state_topic": BASE_TOPIC + "/pcb/temperature",
        "unit_of_measurement": "°C",
        "device_class": "temperature",
        "device": device_info,
    }
    pub(TEMP_CONFIG_TOPIC, temp_config_payload)

    # Config for Temperature Sensor
    TEMP_CONFIG_TOPIC = "homeassistant/sensor/gardyn/" + IDENTIFIER + "_temperature/config"
    temp_config_payload = {
        "name": "Temperature",
        "unique_id": IDENTIFIER + "_temperature",
        "state_topic": BASE_TOPIC + "/temperature",
        "command_topic": BASE_TOPIC + "/temperature/get",
        "unit_of_measurement": "°C",
        "device_class": "temperature",
        "device": device_info,
    }
    pub(TEMP_CONFIG_TOPIC, temp_config_payload)

    # Config for Humidity Sensor
    TEMP_CONFIG_TOPIC = "homeassistant/sensor/gardyn/" + IDENTIFIER + "_humidity/config"
    temp_config_payload = {
        "name": "Humidity",
        "unique_id": IDENTIFIER + "_humidity",
        "state_topic": BASE_TOPIC + "/humidity",
        "command_topic": BASE_TOPIC + "/humidity/get",
        "unit_of_measurement": "%",
        "device_class": "humidity",
        "device": device_info,
    }
    pub(TEMP_CONFIG_TOPIC, temp_config_payload)

    # Config for Water Level Sensor
    TEMP_CONFIG_TOPIC = "homeassistant/sensor/gardyn/" + IDENTIFIER + "_water_level/config"

    temp_config_payload = {
        "name": "Water Level",
        "unique_id": IDENTIFIER + "_water_level",
        "state_topic": BASE_TOPIC + "/water/level",
        "command_topic": BASE_TOPIC + "/water/level/get",
        "unit_of_measurement": "cm",
        "device_class": "distance",
        "device": device_info,
    }
    pub(TEMP_CONFIG_TOPIC, temp_config_payload)

    # Config for Water Low Binary Sensor
    TEMP_CONFIG_TOPIC = f"homeassistant/binary_sensor/gardyn/{IDENTIFIER}_water_low/config"
    temp_config_payload = {
        "name": "Water Low",
        "unique_id": IDENTIFIER + "_water_low",
        "platform": "mqtt",
        "state_topic": BASE_TOPIC + "/water/low/state",
        "device_class": "problem",
        "payload_on": "ON",
        "payload_off": "OFF",
        "device": device_info,
    }
    pub(TEMP_CONFIG_TOPIC, temp_config_payload)

    # Config for Water Low Threshold (current value)
    # Config for Water Low CM Set Number
    TEMP_CONFIG_TOPIC = f"homeassistant/number/gardyn/{IDENTIFIER}_water_low_cm/config"
    temp_config_payload = {
        "name": "Set Water Low Threshold",
        "unique_id": IDENTIFIER + "_water_low_cm",
        "platform": "mqtt",
        "state_topic": BASE_TOPIC + "/water/low/cm",
        "command_topic": BASE_TOPIC + "/water/low/cm/set",
        "min": 0,
        "max": 15,
        "step": 0.5,
        "unit_of_measurement": "cm",
        "device_class": "distance",
        "device": device_info,
    }
    pub(TEMP_CONFIG_TOPIC, temp_config_payload)

    # Config for Water Low Mode (Enabled/Disabled)
    TEMP_CONFIG_TOPIC = f"homeassistant/sensor/gardyn/{IDENTIFIER}_water_low_mode/config"
    temp_config_payload = {
        "name": "Water Low Mode",
        "unique_id": IDENTIFIER + "_water_low_mode",
        "platform": "mqtt",
        "state_topic": BASE_TOPIC + "/water/low/mode",
        "icon": "mdi:toggle-switch",  # Optional: or use mdi:alert for dramatic effect
        "device": device_info,
    }
    pub(TEMP_CONFIG_TOPIC, temp_config_payload)

    # Discovery configuration for Camera A (image entity)
    TEMP_CONFIG_TOPIC = "homeassistant/image/gardyn/" + IDENTIFIER + "_upper_camera/config"
    temp_config_payload = {
        "name": "Upper Camera",
        "unique_id": IDENTIFIER + "_upper_camera",
        "image_topic": BASE_TOPIC + "/image/upper_camera",
        "encoding": "b64",
        "content_type": "image/jpeg",
        "object_id": IDENTIFIER + "_upper_camera",
        "device": device_info,
    }
    pub(TEMP_CONFIG_TOPIC, temp_config_payload)

    # Discovery configuration for Camera B (image entity)
    TEMP_CONFIG_TOPIC = "homeassistant/image/gardyn/" + IDENTIFIER + "_lower_camera/config"
    temp_config_payload = {
        "name": "Lower Camera",
        "unique_id": IDENTIFIER + "_lower_camera",
        "image_topic": BASE_TOPIC + "/image/lower_camera",
        "encoding": "b64",
        "content_type": "image/jpeg",
        "object_id": IDENTIFIER + "_lower_camera",
        "device": device_info,
    }
    pub(TEMP_CONFIG_TOPIC, temp_config_payload)

    # Config for the physical button as a Home Assistant event entity (#78).
    # Fires "single"/"double"/"long" so HA automations can react to presses.
    TEMP_CONFIG_TOPIC = f"homeassistant/event/gardyn/{IDENTIFIER}_button/config"
    temp_config_payload = {
        "name": "Button",
        "unique_id": IDENTIFIER + "_button",
        "state_topic": BASE_TOPIC + "/button/event",
        "event_types": ["single", "double", "long"],
        "device_class": "button",
        "device": device_info,
    }
    pub(TEMP_CONFIG_TOPIC, temp_config_payload)

    # Diagnostic 'Last Log' sensor: most recent WARNING/ERROR published by the
    # MQTT log handler, for at-a-glance debugging from Home Assistant.
    TEMP_CONFIG_TOPIC = f"homeassistant/sensor/gardyn/{IDENTIFIER}_log/config"
    temp_config_payload = {
        "name": "Last Log",
        "unique_id": IDENTIFIER + "_log",
        "state_topic": LOG_TOPIC,
        "icon": "mdi:text-box-outline",
        "entity_category": "diagnostic",
        "device": device_info,
    }
    pub(TEMP_CONFIG_TOPIC, temp_config_payload)


def on_connect(client, userdata, flags, rc, properties=None):
    logger.info(f"Connected with result code {rc}")
    client.subscribe(BASE_TOPIC + "/#")
    # client.subscribe(BASE_TOPIC + "/light/brightness/set")
    # Mark the device online (counterpart to the LWT 'offline' set before connect).
    client.publish(AVAILABILITY_TOPIC, "online", retain=True)
    send_discovery_messages(client)
    publish_water_low_mode(client)


def on_message(client, userdata, msg):
    global brightness, speed, WATER_LOW_CM

    # Handle binary payloads (like image topics) — skip decoding
    if msg.topic.endswith("/image/upper_camera") or msg.topic.endswith("/image/lower_camera"):
        logger.debug(f"Received binary image on topic {msg.topic}, skipping decode.")
        return

    try:
        payload = msg.payload.decode("utf-8").strip()
        logger.debug(f"Decoded payload on {msg.topic}: '{payload}'")
    except UnicodeDecodeError:
        logger.error(f"Failed to decode message on topic {msg.topic}. Likely binary.")
        return

    topic_suffix = msg.topic.replace(BASE_TOPIC + "/", "")

    try:
        # === Pump Logic ===
        if topic_suffix == "pump/command":
            if payload.upper() == "ON":
                if WATER_LOW_CM not in (None, 0):
                    distance = safe_distance_measure()
                    if distance is not None and distance > WATER_LOW_CM:
                        logger.warning(
                            f"Water too low ({distance:.2f}cm > {WATER_LOW_CM:.2f}cm), aborting pump"
                        )
                        flash_lights()
                        client.publish(BASE_TOPIC + "/water/low/state", "ON", retain=True)
                        return
                    else:
                        client.publish(BASE_TOPIC + "/water/low/state", "OFF", retain=True)
                pump.set_speed(speed)
                _arm_pump_safety()
                client.publish(BASE_TOPIC + "/pump/state", "ON")
            elif payload.upper() == "OFF":
                pump.off()
                _cancel_pump_safety()
                client.publish(BASE_TOPIC + "/pump/state", "OFF")

        elif topic_suffix == "pump/speed/set" and payload.isdigit():
            speed = int(payload)
            pump.set_speed(speed)
            if speed > 0:
                _arm_pump_safety()
            else:
                _cancel_pump_safety()
            client.publish(BASE_TOPIC + "/pump/speed/state", str(speed))

        # === Light Logic ===
        elif topic_suffix == "light/command":
            if payload.upper() == "ON":
                light.set_duty_cycle(brightness)
                client.publish(BASE_TOPIC + "/light/state", "ON")
            elif payload.upper() == "OFF":
                light.off()
                client.publish(BASE_TOPIC + "/light/state", "OFF")

        elif topic_suffix == "light/brightness/set" and payload.isdigit():
            brightness = int(payload)
            light.set_duty_cycle(brightness)
            client.publish(BASE_TOPIC + "/light/brightness/state", str(brightness))

        # === Water Level ===
        elif topic_suffix == "water/level/get":
            distance = safe_distance_measure()
            if distance is not None:
                client.publish(BASE_TOPIC + "/water/level", f"{distance:.2f}")

        elif topic_suffix == "water/low/cm/set":
            try:
                WATER_LOW_CM = float(payload)
                client.publish(BASE_TOPIC + "/water/low/cm", f"{WATER_LOW_CM:.2f}", retain=True)
                publish_water_low_mode(client)
                update_water_low_state(client)
            except ValueError:
                logger.error(f"Invalid water low cm value: {payload}")

        # === Sensor Data on Request ===
        elif topic_suffix == "pcb/temperature/get":
            pcb_temp = get_pcb_temperature()
            client.publish(BASE_TOPIC + "/pcb/temperature", f"{pcb_temp:.2f}")

        elif topic_suffix == "temperature/get":
            temperature = temperature_sensor.read()
            client.publish(BASE_TOPIC + "/temperature", f"{temperature:.2f}")

        elif topic_suffix == "humidity/get":
            humidity = humidity_sensor.read()
            client.publish(BASE_TOPIC + "/humidity", f"{humidity:.2f}")

    except Exception as e:
        logger.exception(f"Error handling message on topic {msg.topic}: {e}")


def publish_pcb_temperature(client):
    while True:
        try:
            pcb_temp = get_pcb_temperature()
            logger.info(f"Publishing PCB Temperature: {pcb_temp:.2f}°C")
            client.publish(BASE_TOPIC + "/pcb/temperature", f"{pcb_temp:.2f}")
        except Exception as e:
            logger.error(f"Failed to read or publish PCB temperature: {e}")
        sleep(30 * 60)  # Publish frequency, every x seconds


def publish_temperature(client):
    while True:
        try:
            temperature = temperature_sensor.read()
            logger.info(f"Publishing Temperature: {temperature:.2f}°C")
            client.publish(BASE_TOPIC + "/temperature", f"{temperature:.2f}")
        except Exception as e:
            logger.error(f"Failed to read or publish ambient temperature: {e}")
        sleep(30 * 60)  # Publish frequency, every x seconds


def publish_humidity(client):
    while True:
        try:
            humidity = humidity_sensor.read()
            logger.info(f"Publishing Humidity: {humidity:.2f}%")
            client.publish(BASE_TOPIC + "/humidity", f"{humidity:.2f}")
        except Exception as e:
            logger.error(f"Failed to read or publish ambient humidity: {e}")
        sleep(30 * 60)  # Publish frequency, every x seconds


def publish_water_level(client):
    while True:
        distance = safe_distance_measure()
        if distance is not None:
            logger.info(f"Publishing Water Level: {distance:.2f}cm")
            client.publish(BASE_TOPIC + "/water/level", f"{distance:.2f}")
        sleep(30 * 60)


def publish_images(client):
    while True:
        try:
            # Capture upper camera image
            subprocess.check_call(
                [
                    "fswebcam",
                    "-d",
                    UPPER_CAMERA_DEVICE,
                    "-r",
                    CAMERA_RESOLUTION,
                    "-S",
                    "2",
                    "-F",
                    "2",
                    "--no-banner",
                    UPPER_IMAGE_PATH,
                ]
            )
            logger.info(f"Captured image from upper camera ({UPPER_CAMERA_DEVICE})")

            # Capture lower camera image
            subprocess.check_call(
                [
                    "fswebcam",
                    "-d",
                    LOWER_CAMERA_DEVICE,
                    "-r",
                    CAMERA_RESOLUTION,
                    "-S",
                    "2",
                    "-F",
                    "2",
                    "--no-banner",
                    LOWER_IMAGE_PATH,
                ]
            )
            logger.info(f"Captured image from lower camera ({LOWER_CAMERA_DEVICE})")

            # Publish upper camera image
            with open(UPPER_IMAGE_PATH, "rb") as f:
                upper_cam_jpeg_data = f.read()  # Read as raw binary
                client.publish(
                    BASE_TOPIC + "/image/upper_camera",
                    payload=upper_cam_jpeg_data,
                    qos=0,
                    retain=False,
                )
                logger.info("Published image to /image/upper_camera")

            # Publish lower camera image
            with open(LOWER_IMAGE_PATH, "rb") as f:
                lower_cam_jpeg_data = f.read()  # Read as raw binary
                client.publish(
                    BASE_TOPIC + "/image/lower_camera",
                    payload=lower_cam_jpeg_data,
                    qos=0,
                    retain=False,
                )
                logger.info("Published image to /image/lower_camera")

        except subprocess.CalledProcessError as e:
            logger.error(f"Camera capture failed: {e}")
        except Exception:
            logger.exception("Unexpected error during image capture/publish")

        sleep(IMAGE_INTERVAL_SECONDS)


def restore_actuator_state(client):
    """Restore light/pump to their last persisted state after a restart (#3)."""
    global light_state, pump_state, brightness, speed
    saved = state_lib.load_state()
    brightness = saved.get("brightness", brightness)
    speed = saved.get("speed", speed)
    try:
        if saved.get("light_on"):
            light_state = True
            light.set_duty_cycle(brightness)
            client.publish(BASE_TOPIC + "/light/state", "ON")
        if saved.get("pump_on"):
            pump_state = True
            pump.set_speed(speed)
            _arm_pump_safety()
            client.publish(BASE_TOPIC + "/pump/state", "ON")
        logger.info("Restored actuator state: %s", saved)
    except Exception as exc:
        logger.error("Failed to restore actuator state: %s", exc)


def publish_grow_reminders(client):
    """Publish grow stage and any due reminders (thinning/root/harvest/nutrient)."""
    while True:
        try:
            grow_state = grow_lib.load_state()
            client.publish(BASE_TOPIC + "/grow/stage", grow_state.get("stage", ""), retain=True)
            for reminder in grow_lib.due_reminders(grow_state):
                client.publish(BASE_TOPIC + "/grow/reminder", reminder)
                logger.info("Published grow reminder: %s", reminder)
        except Exception:
            logger.exception("Error publishing grow reminders")
        sleep(int(publish_frequency))


def graceful_shutdown(signum, frame):
    """Turn the pump off and release pigpio cleanly on SIGTERM/SIGINT (#3)."""
    logger.info("Received signal %s; shutting down gracefully", signum)
    try:
        pump.off()
        pump.close()
        light.close()
    except Exception as exc:
        logger.error("Error during graceful shutdown: %s", exc)
    sys.exit(0)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, graceful_shutdown)
    signal.signal(signal.SIGINT, graceful_shutdown)

    logger.info(f"Connecting to {BROKER} on port {PORT} with keep alive {KEEP_ALIVE_INTERVAL}")
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.on_connect = on_connect
    client.on_message = on_message
    client.username_pw_set(USERNAME, PASSWORD)
    # Last Will: broker marks us offline in HA if the connection drops.
    client.will_set(AVAILABILITY_TOPIC, "offline", retain=True)

    # Mirror WARNING+ logs to MQTT for the HA 'Last Log' sensor. Skip paho's own
    # logger to avoid any publish/log feedback.
    mqtt_log_handler = MqttLogHandler(client, LOG_TOPIC)
    mqtt_log_handler.addFilter(lambda record: not record.name.startswith("paho"))
    logging.getLogger().addHandler(mqtt_log_handler)

    client.connect(BROKER, PORT, KEEP_ALIVE_INTERVAL)

    pcb_temp_thread = threading.Thread(target=publish_pcb_temperature, args=(client,))
    pcb_temp_thread.daemon = True
    pcb_temp_thread.start()

    temperature_thread = threading.Thread(target=publish_temperature, args=(client,))
    temperature_thread.daemon = True
    temperature_thread.start()

    humidity_thread = threading.Thread(target=publish_humidity, args=(client,))
    humidity_thread.daemon = True
    humidity_thread.start()

    water_level_thread = threading.Thread(target=publish_water_level, args=(client,))
    water_level_thread.daemon = True
    water_level_thread.start()

    publish_images_thread = threading.Thread(target=publish_images, args=(client,))
    publish_images_thread.daemon = True
    publish_images_thread.start()

    grow_thread = threading.Thread(target=publish_grow_reminders, args=(client,))
    grow_thread.daemon = True
    grow_thread.start()

    # Restore last known actuator state after (re)connect.
    client.on_connect = lambda c, u, f, rc, properties=None: (
        on_connect(c, u, f, rc, properties),
        restore_actuator_state(c),
    )

    client.loop_forever()
