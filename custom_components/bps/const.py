DOMAIN = "bps"

# Global diagnostic sensor: receiver self-localization accuracy (CEP95, m).
# Shared by sensor.py (the entity) and __init__.py (the periodic publisher).
ACCURACY_ENTITY_ID = "sensor.bps_position_accuracy"

# Seconds between tracking cycles (every tracker re-solved per cycle). 1 s is the
# historical cadence and stays the default; a slower cadence trades position
# latency for event-loop time on installs with many trackers or receivers.
OPTION_UPDATE_INTERVAL = "update_interval"
DEFAULT_UPDATE_INTERVAL = 1
MIN_UPDATE_INTERVAL = 1
MAX_UPDATE_INTERVAL = 30