"""Constants for the LaCrosse JeeLink Bridge integration."""

DOMAIN = "lacrosse_jeelink"

# ── Setup (config entry data) ────────────────────────────────────────────────
CONF_SERIAL_PORT = "serial_port"
CONF_AUTO_ADD = "auto_add_entities"
CONF_OUTLIER_CONFIRM_COUNT = "outlier_confirm_count"

# ── Options (changeable anytime via "Configure") ─────────────────────────────
CONF_SERIAL_TIMEOUT = "serial_timeout"
CONF_RECONNECT_DELAY = "reconnect_delay"
CONF_BATTERY_REPLACE_TIMEOUT = "battery_replace_timeout"
CONF_DEBUG_TIMEOUT = "debug_timeout"
CONF_NOTIFY_ENABLED = "notify_enabled"
CONF_NOTIFY_ENTITY = "notify_entity"
# Individually switchable notification types (all default: on)
CONF_NOTIFY_CONNECTION = "notify_connection"
CONF_NOTIFY_DATA_TIMEOUT = "notify_data_timeout"
CONF_NOTIFY_NEW_SENSOR = "notify_new_sensor"
CONF_NOTIFY_BATTERY_LOW = "notify_battery_low"
CONF_NOTIFY_BATTERY_REPLACED = "notify_battery_replaced"
# Radio-silence watchdog: warn when no radio packet has been parsed for
# this long despite an open serial connection (minutes, 0 = off)
CONF_DATA_TIMEOUT = "data_timeout"
# Automatic cleanup: remove auto-discovered sensors the user never
# touched when they have not sent data for this long (hours, 0 = off).
# Catches stray/neighbour sensors that briefly reached the receiver.
CONF_STALE_CLEANUP_HOURS = "stale_cleanup_hours"
# Firmware init commands sent on connect (space-separated, FHEM-style
# initCommands). Default cycles all three data rates every 10 seconds.
CONF_INIT_COMMANDS = "init_commands"

# ── Defaults ─────────────────────────────────────────────────────────────────
# No default serial port on purpose: paths like
# /dev/serial/by-id/usb-1a86_USB2.0-Serial-if00-port0 are specific to the
# individual USB adapter. The config flow lists the detected ports instead.
DEFAULT_SERIAL_TIMEOUT = 1.0        # seconds, blocking readline timeout
DEFAULT_RECONNECT_DELAY = 5         # seconds between reconnect attempts
DEFAULT_BATTERY_REPLACE_TIMEOUT = 120  # seconds to swap the battery
DEFAULT_DEBUG_TIMEOUT = 300         # seconds until debug mode auto-disables
DEFAULT_NOTIFY_ENTITY = ""          # empty = no notifications
DEFAULT_DATA_TIMEOUT = 15           # minutes without any parsed packet -> warn (0 = off)
DEFAULT_STALE_CLEANUP_HOURS = 0     # hours before auto-removing silent unnamed sensors (0 = off)
DEFAULT_INIT_COMMANDS = "7m 10t"    # data-rate toggle mask 7, toggle every 10 s

OUTLIER_CONFIRM_COUNT = 5  # default for CONF_OUTLIER_CONFIRM_COUNT

# Global filter thresholds (apply to all sensors)
DEFAULT_TEMP_MIN = -50
DEFAULT_TEMP_MAX = 60
DEFAULT_TEMP_MAX_DELTA = 10
DEFAULT_HUM_MAX_DELTA = 20
