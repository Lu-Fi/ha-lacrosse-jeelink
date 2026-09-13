"""JeeLink serial coordinator - dynamic sensor discovery, no hardcoded sensor map.

Protocol reference: FHEM 36_LaCrosse.pm

OK 9 format byte layout (parts[2..6]):
  parts[2]  sensor ID
  parts[3]  flags:
              bit 7 (0x80) = new battery inserted
              bits 4-6 (0x70) = sensor type
              bits 0-3 (0x0F) = channel  (1 = main, 2 = probe2/external)
  parts[4]  temperature MSB  }  temp = (MSB*256 + LSB - 1000) / 10.0
  parts[5]  temperature LSB  }
  parts[6]  humidity byte:
              bit 7 (0x80) = weak/low battery
              bits 0-6 (0x7F) = humidity value (0-100, valid: 1-100)
                  > 100 after masking = no humidity sensor (e.g. 106 = temperature-only)
"""
from __future__ import annotations

import datetime
import logging
import math
import threading
import time
from dataclasses import dataclass

import serial
import subprocess

from homeassistant.core import HomeAssistant, callback
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from homeassistant.util.async_ import run_callback_threadsafe

from .const import (
    DOMAIN,
    CONF_SERIAL_PORT,
    CONF_AUTO_ADD,
    CONF_BATTERY_REPLACE_TIMEOUT,
    CONF_DATA_TIMEOUT,
    CONF_DEBUG_TIMEOUT,
    CONF_DISCOVERY_MIN_PACKETS,
    CONF_DISCOVERY_WINDOW_SEC,
    CONF_INIT_COMMANDS,
    CONF_NOTIFY_BATTERY_LOW,
    CONF_NOTIFY_BATTERY_REPLACED,
    CONF_NOTIFY_CONNECTION,
    CONF_NOTIFY_DATA_TIMEOUT,
    CONF_NOTIFY_ENABLED,
    CONF_NOTIFY_ENTITY,
    CONF_NOTIFY_NEW_SENSOR,
    CONF_OUTLIER_CONFIRM_COUNT,
    CONF_RECONNECT_DELAY,
    CONF_SERIAL_TIMEOUT,
    CONF_STALE_CLEANUP_HOURS,
    DEFAULT_DATA_TIMEOUT,
    DEFAULT_DISCOVERY_MIN_PACKETS,
    DEFAULT_DISCOVERY_WINDOW_SEC,
    DEFAULT_INIT_COMMANDS,
    DEFAULT_STALE_CLEANUP_HOURS,
    DEFAULT_BATTERY_REPLACE_TIMEOUT,
    DEFAULT_DEBUG_TIMEOUT,
    DEFAULT_HUM_MAX_DELTA,
    DEFAULT_NOTIFY_ENTITY,
    DEFAULT_RECONNECT_DELAY,
    DEFAULT_SERIAL_TIMEOUT,
    DEFAULT_TEMP_MAX,
    DEFAULT_TEMP_MAX_DELTA,
    DEFAULT_TEMP_MIN,
    OUTLIER_CONFIRM_COUNT,
)

_LOGGER = logging.getLogger(__name__)

# Level names the logger integration's set_level service accepts, lowest
# first. Used to translate a remembered logging level back into a service
# argument when debug mode is switched off.
_LEVEL_NAMES = (
    (logging.NOTSET, "notset"),
    (logging.DEBUG, "debug"),
    (logging.INFO, "info"),
    (logging.WARNING, "warning"),
    (logging.ERROR, "error"),
    (logging.CRITICAL, "critical"),
)


def _level_name(level: int) -> str:
    """Service-compatible name for a logging level.

    logging.getLevelName() would happily return "Level 25" for a custom
    level, which the logger integration rejects - so a level in between two
    standard ones is rounded down to the nearest name it does accept.
    """
    name = "notset"
    for value, candidate in _LEVEL_NAMES:
        if level >= value:
            name = candidate
    return name


# ── Dew point (Magnus formula, identical to FHEM LaCrosse_CalcDewpoint) ───────

def _dewpoint(temp: float, hum: float) -> float:
    """Magnus formula. Same coefficients as in 36_LaCrosse.pm."""
    a, b = (7.5, 237.3) if temp >= 0 else (7.6, 240.7)
    sdd = 6.1078 * 10 ** ((a * temp) / (b + temp))
    dd  = hum / 100 * sdd
    v   = math.log10(dd / 6.1078)
    return (b * v) / (a - v)


@dataclass
class SensorDiscovery:
    """Describes a newly discovered sensor channel."""
    # int for LaCrosse IT+ sensors (8-bit radio ID); str ("emt_<id>",
    # "ls_<id>") for the other protocols carried by the same LaCrosseITPlusReader
    # sketch (EMT7110, LevelSender) - kept in their own ID namespace so they
    # can never collide with a LaCrosse IT+ radio ID.
    sensor_id: int | str
    channel: str   # "temperature" | "temperature2" | "humidity" | "battery" | ...


class JeeLinkCoordinator:
    """Manages the serial JeeLink connection and distributes sensor updates."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry

        def _opt(key: str, default):
            """Option with fallback to entry.data (older installations)."""
            return entry.options.get(key, entry.data.get(key, default))

        self.serial_port: str = _opt(CONF_SERIAL_PORT, entry.data[CONF_SERIAL_PORT])
        self.auto_add: bool = _opt(CONF_AUTO_ADD, True)
        self.outlier_confirm_count: int = int(
            _opt(CONF_OUTLIER_CONFIRM_COUNT, OUTLIER_CONFIRM_COUNT)
        )
        # Configurable timeouts (options flow)
        self.serial_timeout: float = float(_opt(CONF_SERIAL_TIMEOUT, DEFAULT_SERIAL_TIMEOUT))
        self.reconnect_delay: int = int(_opt(CONF_RECONNECT_DELAY, DEFAULT_RECONNECT_DELAY))
        self.battery_replace_timeout: int = int(
            _opt(CONF_BATTERY_REPLACE_TIMEOUT, DEFAULT_BATTERY_REPLACE_TIMEOUT)
        )
        self.debug_timeout: int = int(_opt(CONF_DEBUG_TIMEOUT, DEFAULT_DEBUG_TIMEOUT))
        # Firmware init commands (space-separated, sent on every connect and
        # after firmware-hang resets; equivalent to FHEM's initCommands
        # attribute). Default "7m 10t" = cycle all three data rates every
        # 10 s so mixed sensor generations are received.
        self.init_commands: str = str(
            _opt(CONF_INIT_COMMANDS, DEFAULT_INIT_COMMANDS)
        ).strip() or DEFAULT_INIT_COMMANDS
        # Notifications (options flow): any notify entity, empty = no
        # messages. notify_enabled is the master switch; each message type
        # can additionally be toggled individually.
        self.notify_enabled: bool = bool(_opt(CONF_NOTIFY_ENABLED, True))
        self.notify_entity: str = _opt(CONF_NOTIFY_ENTITY, DEFAULT_NOTIFY_ENTITY)
        self.notify_types: dict[str, bool] = {
            "connection": bool(_opt(CONF_NOTIFY_CONNECTION, True)),
            "data_timeout": bool(_opt(CONF_NOTIFY_DATA_TIMEOUT, True)),
            "new_sensor": bool(_opt(CONF_NOTIFY_NEW_SENSOR, True)),
            "battery_low": bool(_opt(CONF_NOTIFY_BATTERY_LOW, True)),
            "battery_replaced": bool(_opt(CONF_NOTIFY_BATTERY_REPLACED, True)),
        }
        # Radio-silence watchdog (minutes, 0 = off)
        self.data_timeout_min: int = int(_opt(CONF_DATA_TIMEOUT, DEFAULT_DATA_TIMEOUT))
        self.last_data_ts: float = 0.0
        self._data_timeout_notified = False
        self._unsub_watchdog = None
        # Automatic cleanup of stray auto-discovered sensors (hours, 0 = off)
        self.stale_cleanup_hours: int = int(
            _opt(CONF_STALE_CLEANUP_HOURS, DEFAULT_STALE_CLEANUP_HOURS)
        )
        self._last_stale_check = 0.0
        # Discovery threshold (like FHEM autoCreateThreshold): create a
        # brand-new sensor only after N packets within T seconds (1 = at
        # the first packet, previous behaviour).
        self.discovery_min_packets: int = int(
            _opt(CONF_DISCOVERY_MIN_PACKETS, DEFAULT_DISCOVERY_MIN_PACKETS)
        )
        self.discovery_window_sec: int = int(
            _opt(CONF_DISCOVERY_WINDOW_SEC, DEFAULT_DISCOVERY_WINDOW_SEC)
        )
        # Candidate counter for the threshold: sensor_id -> (first_ts, count).
        # Bounded by the 8-bit radio ID space; entries reset themselves when
        # the window expires.
        self._discovery_candidates: dict[int | str, tuple[float, int]] = {}

        self.debug: bool = False
        # Firmware identification of the stick (banner line of the
        # LaCrosseITPlusReader sketch, e.g. "LaCrosseITPlusReader.10.1s
        # (RFM69 f:868300 r:17241)"). Emitted after every reset and on the
        # "v" command; stored as sw_version on the bridge device (analogous
        # to FHEM's model/settings internals).
        self.firmware: str | None = None
        # Connection state for the connected binary sensor + notifications
        self.connected: bool = False
        self._conn_notified_down = False
        # Battery state cache for the "battery low" notification
        self._battery_notified: set[int] = set()

        # sensor_id -> set of channels already created as entities
        # (sensor_id is int for LaCrosse IT+, str "emt_<id>"/"ls_<id>" for
        # EMT7110/LevelSender - see SensorDiscovery)
        self._discovered: dict[int | str, set[str]] = {}

        # (sensor_id, channel) -> current value
        self.sensor_states: dict[tuple[int | str, str], object] = {}

        # Cache for outlier checking
        self._cache: dict[tuple[int | str, str], object] = {}

        # Outlier confirmation: key -> (value, consecutive_count)
        self._outlier_counter: dict[tuple, tuple] = {}

        # Bridge-level ("broadcast") listeners: connection state, radio
        # silence, debug switch. Fired only by unkeyed _notify_listeners()
        # calls, i.e. on the rare bridge events - not on every radio packet.
        self._state_listeners: list = []
        # Per-sensor listeners: sensor_id -> [callback, ...]. Every entity
        # belonging to a discovered sensor registers here, so a packet only
        # rewrites the state of that one sensor's entities.
        self._keyed_listeners: dict[int | str, list] = {}
        self._discovery_callbacks: list = []

        # Battery replacement: new_id -> old_id (alias after ID change)
        self._id_aliases: dict[int, int] = {}
        # Battery replacement mode active: old_id -> expiry timestamp
        self._replace_battery: dict[int, float] = {}
        # Cancel handles of the expiry refresh timers (old_id -> unsub)
        self._replace_expiry_cancels: dict[int, callable] = {}

        self._entry_id = entry.entry_id
        # Persistence of the ID aliases (battery replacement): without the
        # store, the "new radio ID -> old sensor" mapping would be lost on
        # an HA restart and the new ID would be created as a NEW sensor.
        self._alias_store: Store = Store(
            hass, 1, f"{DOMAIN}_{entry.entry_id}_aliases"
        )
        self._reset_event = threading.Event()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._debug_cancel = None
        # Log level of this integration's logger at the moment debug mode
        # was switched on, so switching it off restores exactly what the
        # user had configured (logger: YAML, logger.set_level, ...) instead
        # of unconditionally forcing "warning" onto them.
        self._debug_prev_level: int | None = None
        self._sensor_device_infos: dict[int | str, DeviceInfo] = {}
        # Registry id of the bridge device. Per-sensor devices reference it
        # as via_device_id (see get_sensor_device_info) - the replacement
        # for the deprecated via_device identifier tuple, which needs a real
        # device id and therefore a registered bridge device.
        self._bridge_device_id: str | None = None

        # Bridge device (for the reset button and debug switch). The model
        # is deliberately generic: what matters is the LaCrosseITPlusReader
        # sketch, not the board - genuine JeeLink v3/v3c and Arduino clones
        # (CH340 + RFM69/RFM12) behave identically on the serial port.
        self.device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="LaCrosse JeeLink Bridge",
            manufacturer="JeeLabs / DIY",
            model="JeeLink v3 / Arduino clone (LaCrosseITPlusReader)",
        )

    # ── Notifications ──────────────────────────────────────────────────────────

    def _lang(self) -> str:
        """'de' for a German HA system language, otherwise 'en' (fallback).

        Notification texts only exist as a DE/EN pair; English is the
        fallback for every other system language (it used to be German,
        which was backwards for everyone but German installations).
        """
        lang = (self.hass.config.language or "en").lower()
        return "de" if lang.startswith("de") else "en"

    def _device_label(self, sensor_id: int) -> str:
        """Human-readable label for an already-known LaCrosse IT+ sensor:
        'LaCrosse Sensor 3 (Heizung Bad)' if the user renamed the device in
        HA, else just 'LaCrosse Sensor 3'. Used in notifications that refer
        to an existing device by its raw radio ID (battery_low,
        battery_replaced) - without this, the user has no way to tell which
        physical sensor the message is about besides looking up the ID in
        the device registry by hand.

        May be called from the serial reader thread. The device registry
        read is marshalled onto the event loop (like every other hass
        interaction in this file) instead of touched directly, to respect
        its loop affinity.

        The lookup is a plain synchronous registry read, so it is scheduled
        with run_callback_threadsafe() rather than wrapped in a coroutine
        and handed to run_coroutine_threadsafe(): the latter needs a full
        task to be created and scheduled before the reader thread's join
        can return, which blocked the reader for noticeably longer (up to
        the 2 s timeout on a busy loop) for what is a dictionary lookup."""

        @callback
        def _lookup() -> str | None:
            dev_reg = dr.async_get(self.hass)
            device = dev_reg.async_get_device_by_identifier(
                (DOMAIN, f"{self._entry_id}_{sensor_id}"), self._entry_id
            )
            return device.name_by_user if device else None

        try:
            name_by_user = run_callback_threadsafe(
                self.hass.loop, _lookup
            ).result(timeout=2)
        except Exception:  # noqa: BLE001
            name_by_user = None

        base = f"LaCrosse Sensor {sensor_id}"
        if name_by_user:
            return f"{base} ({name_by_user})"
        return base

    def _notify_user(self, category: str, message_de: str, message_en: str) -> None:
        """Send a message to the configured notify entity.

        Thread-safe (may be called from the serial reader thread). Nothing
        happens without a configured entity, with notify_enabled=False, or
        with the message type (category) switched off. Delivery errors must
        never disturb operation.

        Uses telegram_bot.send_message with parse_mode=plain_text instead of
        the generic notify.send_message action: the latter sends with
        Markdown parsing by default, and message text here routinely
        contains hyphens/parentheses (e.g. "Funk-ID", "(Verbindung ...)")
        that make Telegram reject the message with "BadRequest: can't parse
        entities" - silently, since the previous blocking=False meant any
        delivery failure happened in a detached background task this
        method's own try/except never saw. blocking=True now makes a real
        failure show up in the log instead of vanishing (same fix already
        applied to the pvsc integration after the identical bug there).
        Trade-off: no longer generic for arbitrary notify platforms, but
        matches how notify_entity is actually used here (Telegram).
        """
        if not self.notify_enabled or not self.notify_entity:
            return
        if not self.notify_types.get(category, True):
            return
        message = message_en if self._lang() == "en" else message_de

        async def _send() -> None:
            try:
                await self.hass.services.async_call(
                    "telegram_bot",
                    "send_message",
                    {
                        "entity_id": self.notify_entity,
                        "message": message,
                        "parse_mode": "plain_text",
                    },
                    blocking=True,
                )
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning(
                    "Notification via %s failed: %s", self.notify_entity, err
                )

        self._call_soon_threadsafe(
            lambda: self.hass.async_create_task(_send())
        )

    # ── Battery replacement ────────────────────────────────────────────────────

    def start_battery_replace(self, sensor_id: int, timeout: int | None = None) -> None:
        """Arm the battery replacement mode for sensor_id for timeout seconds.

        Identical to FHEM's 'set <device> replaceBatteryForSec <sec>'. The
        next unknown sensor carrying the battery_new flag is aliased onto
        this ID. Without an explicit timeout, the configured option
        battery_replace_timeout (default 120 s) applies.
        """
        if timeout is None:
            timeout = self.battery_replace_timeout
        self._replace_battery[sensor_id] = time.time() + timeout
        _LOGGER.info(
            "Battery replacement mode armed for sensor %d (%ds) - "
            "please swap the battery now",
            sensor_id, timeout,
        )
        self._notify_listeners(sensor_id)

        # Refresh entities once the window expires so the button attribute
        # replace_active does not incorrectly stay "true" until the next
        # received packet. The timer target MUST be @callback-decorated:
        # HassJob classifies anything else as HassJobType.Executor and runs
        # it on a worker thread, where _notify_listeners' async_write_ha_state()
        # raises - i.e. the refresh never happened (a bare lambda was used
        # here before).
        @callback
        def _replace_expired(_now=None) -> None:
            self._replace_expiry_cancels.pop(sensor_id, None)
            self._notify_listeners(sensor_id)

        cancel = self._replace_expiry_cancels.pop(sensor_id, None)
        if cancel:
            cancel()  # re-armed before expiry - don't stack timers
        self._replace_expiry_cancels[sensor_id] = async_call_later(
            self.hass, timeout + 1, _replace_expired
        )

    def is_battery_replace_active(self, sensor_id: int) -> bool:
        """True while the battery replacement mode is armed for this sensor."""
        expiry = self._replace_battery.get(sensor_id)
        return expiry is not None and time.time() < expiry

    @property
    def data_stale(self) -> bool:
        """True while the radio-silence watchdog is triggered (no packet for
        longer than data_timeout minutes despite an open connection).
        Exposed as a binary sensor so automations can react (e.g. press the
        stick reset button) without depending on notifications."""
        return self._data_timeout_notified

    def _resolve_id(self, sensor_id: int) -> int:
        """Resolve sensor_id via the alias table (after battery replacement)."""
        return self._id_aliases.get(sensor_id, sensor_id)

    def _schedule_alias_save(self) -> None:
        """Persist the alias table (thread-safe from the reader thread).

        The dict is snapshotted with dict() before being iterated: this runs
        on the serial reader thread, while forget_sensor() (stale cleanup,
        manual device removal) deletes from the very same _id_aliases on the
        event loop - iterating it live could raise "dictionary changed size
        during iteration", which used to tear down the whole serial
        connection instead of dropping one line.
        """
        data = {str(k): v for k, v in dict(self._id_aliases).items()}

        def _save() -> None:
            self._alias_store.async_delay_save(lambda: data, 1.0)

        self._call_soon_threadsafe(_save)

    @staticmethod
    def _sensor_kind(sensor_id: int | str) -> str:
        """Which protocol a discovered sensor_id belongs to. Used to pick the
        right device info (name/manufacturer/model) - EMT7110 and
        LevelSender are carried by the same LaCrosseITPlusReader sketch and
        serial port, but are not LaCrosse IT+ weather sensors and must not
        be presented as such."""
        if isinstance(sensor_id, str):
            if sensor_id.startswith("emt_"):
                return "emt7110"
            if sensor_id.startswith("ls_"):
                return "levelsender"
        return "lacrosse"

    @callback
    def register_bridge_device(self) -> str:
        """Register the bridge device and remember its registry id.

        Per-sensor devices are linked to the bridge through via_device_id,
        which is the bridge's *registry id* - unlike the deprecated
        via_device it cannot be written as an identifier tuple and only
        exists once the bridge device is actually registered. Home Assistant
        rejects a device info whose via_device_id is not a registered device
        id and drops the entity entirely ("Not adding entity with invalid
        device info"), so the bridge is created explicitly here - from
        async_setup_entry, before the platforms are forwarded and therefore
        before any per-sensor device info can be built - instead of relying
        on the bridge entities of the binary_sensor/button/switch platforms
        happening to be registered first.

        Because this runs before the platforms are forwarded, and every
        caller below (per-sensor device info, firmware sw_version) only
        runs after platform setup or after the serial reader thread has
        been started, self._bridge_device_id is guaranteed to be set from
        then on - no lazy fallback lookup is needed anywhere.
        """
        device = dr.async_get(self.hass).async_get_or_create(
            config_entry_id=self._entry_id, **self.device_info
        )
        self._bridge_device_id = device.id
        return device.id

    def get_sensor_device_info(self, sensor_id: int | str) -> DeviceInfo:
        """DeviceInfo for a single discovered sensor, linked to the bridge.

        sensor_id is int for LaCrosse IT+ sensors and str ("emt_<id>",
        "ls_<id>") for EMT7110/LevelSender - each protocol gets its own
        device name/manufacturer/model so they don't show up mislabelled
        as "LaCrosse" sensors.

        The device NAME carries the radio ID in decimal and in hex, because
        FHEM's 36_LaCrosse.pm / 36_EMT7110.pm name their devices by the hex
        form (%02X for LaCrosse, %02X%02X for EMT7110) - users migrating
        from FHEM know their sensors by exactly that. serial_number and the
        identifiers stay decimal: they must not change for installations
        that already exist.
        """
        info = self._sensor_device_infos.get(sensor_id)
        if info is None:
            kind = self._sensor_kind(sensor_id)
            if kind == "emt7110":
                raw_id = sensor_id.split("_", 1)[1]
                info = DeviceInfo(
                    identifiers={(DOMAIN, f"{self._entry_id}_{sensor_id}")},
                    name=f"LaCrosse EMT7110 {raw_id}{self._hex_suffix(raw_id, 4)}",
                    manufacturer="LaCrosse Technology",
                    model="EMT7110 Energy Sensor",
                    serial_number=raw_id,
                    via_device_id=self._bridge_device_id,
                )
            elif kind == "levelsender":
                raw_id = sensor_id.split("_", 1)[1]
                info = DeviceInfo(
                    identifiers={(DOMAIN, f"{self._entry_id}_{sensor_id}")},
                    name=f"LevelSender {raw_id}",
                    manufacturer="DIY / LevelSender",
                    model="Fill-Level Sensor",
                    serial_number=raw_id,
                    via_device_id=self._bridge_device_id,
                )
            else:
                info = DeviceInfo(
                    identifiers={(DOMAIN, f"{self._entry_id}_{sensor_id}")},
                    name=f"LaCrosse Sensor {sensor_id}"
                         f"{self._hex_suffix(sensor_id, 2)}",
                    manufacturer="LaCrosse Technology",
                    model="IT+ Sensor",
                    serial_number=str(sensor_id),
                    via_device_id=self._bridge_device_id,
                )
            self._sensor_device_infos[sensor_id] = info
        return info

    @staticmethod
    def _hex_suffix(raw_id, digits: int) -> str:
        """' (0x2A)' for a numeric radio ID, '' for anything unparseable.

        FHEM names LaCrosse/EMT7110 devices by the hex ID; showing it next
        to the decimal one is what makes a migrated installation
        recognisable. LevelSender has no hex convention in FHEM and is left
        decimal-only.
        """
        try:
            return f" (0x{int(raw_id):0{digits}X})"
        except (TypeError, ValueError):
            return ""

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    async def async_start(self) -> None:
        # Load persisted ID aliases (battery replacement) BEFORE any packet
        # is processed - otherwise an already mapped new radio ID would be
        # created as a new sensor.
        stored = await self._alias_store.async_load()
        if stored:
            self._id_aliases = {int(k): int(v) for k, v in stored.items()}
            _LOGGER.debug("Loaded ID aliases: %s", self._id_aliases)
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._serial_loop, name="jeelink-serial", daemon=True
        )
        self._thread.start()

        # Per-minute maintenance tick: radio-silence watchdog (data_timeout
        # minutes, 0 = off) and cleanup of stray auto-discovered sensors
        # (stale_cleanup_hours, 0 = off, checked every 15 minutes).
        if self.data_timeout_min > 0 or self.stale_cleanup_hours > 0:
            self._unsub_watchdog = async_track_time_interval(
                self.hass, self._async_maintenance, datetime.timedelta(seconds=60)
            )

    def preload_from_registry(self) -> list[SensorDiscovery]:
        """Reconstruct known sensors from the entity registry.

        Called after platform setup (see __init__.py) so that ALL entities
        of a known sensor exist again immediately - even if the sensor
        (e.g. with an empty battery) never sends another packet. Without
        this, especially the "battery replaced" button and the battery
        sensor of a dead sensor would stay unavailable after a restart -
        and the button is needed exactly then.
        """
        registry = er.async_get(self.hass)
        known_entries = er.async_entries_for_config_entry(registry, self._entry_id)
        prefix = self._entry_id + "_"
        # unique_id suffix -> discovery channel. dewpoint is deliberately
        # omitted: its entity is created together with "humidity".
        # "_battery_replace" (button) must be checked before "_battery".
        suffix_to_channel = (
            ("_battery_replace", "replace_battery"),
            ("_temperature2", "temperature2"),
            ("_temperature", "temperature"),
            ("_humidity", "humidity"),
            ("_last_seen", "last_seen"),
            ("_battery", "battery"),
            # EMT7110 / LevelSender channels (see _parse_emt7110_line /
            # _parse_levelsender_line) - their sensor_id is the string
            # "emt_<id>"/"ls_<id>" itself, so the int() below fails and we
            # fall back to keeping it as a string, e.g. rest="emt_1234_power"
            # -> sensor_id "emt_1234".
            ("_voltage", "voltage"),
            ("_current", "current"),
            ("_power", "power"),
            ("_energy", "energy"),
            ("_connected", "connected"),
            ("_level", "level"),
        )
        discoveries: list[SensorDiscovery] = []
        for entity_entry in known_entries:
            uid = entity_entry.unique_id
            if not uid.startswith(prefix):
                continue
            rest = uid[len(prefix):]
            for suffix, channel in suffix_to_channel:
                if rest.endswith(suffix):
                    raw = rest[: -len(suffix)]
                    try:
                        sensor_id = int(raw)
                    except ValueError:
                        if not raw:
                            break
                        sensor_id = raw  # str-namespaced id (emt_/ls_ kinds)
                    known = self._discovered.setdefault(sensor_id, set())
                    if channel not in known:
                        known.add(channel)
                        discoveries.append(SensorDiscovery(sensor_id, channel))
                    break
        return discoveries

    @callback
    def _async_maintenance(self, _now=None) -> None:
        """Per-minute maintenance tick: radio-silence watchdog + stale cleanup."""
        if self.data_timeout_min > 0:
            self._check_data_timeout()
        if self.stale_cleanup_hours > 0 and (
            time.time() - self._last_stale_check > 15 * 60
        ):
            self._last_stale_check = time.time()
            self._cleanup_stale_sensors()

    def _check_data_timeout(self) -> None:
        """Warn when no radio packet has been parsed for longer than
        data_timeout minutes (the all-clear is sent from _parse_line as soon
        as data comes in again)."""
        if not self.connected or not self.last_data_ts:
            # Without a connection the connection-loss message already
            # covers it; without any received data there is no baseline.
            return
        silence = time.time() - self.last_data_ts
        if silence > self.data_timeout_min * 60 and not self._data_timeout_notified:
            self._data_timeout_notified = True
            self._notify_listeners()  # update the "radio silence" binary sensor
            minutes = int(silence // 60)
            _LOGGER.warning(
                "JeeLink: no radio packets received for %d minutes "
                "(connection is up)", minutes,
            )
            self._notify_user(
                "data_timeout",
                f"JeeLink: seit {minutes} Minuten keine Funkdaten mehr "
                "empfangen (Verbindung zum Stick besteht)",
                f"JeeLink: no radio data received for {minutes} minutes "
                "(serial connection is up)",
            )

    def _last_seen_epoch(self, sensor_id: int | str) -> float | None:
        """Timestamp of the last packet as epoch seconds. Depending on the
        source the value may be an epoch (live), datetime or ISO string
        (restored)."""
        val = self.sensor_states.get((sensor_id, "last_seen"))
        if isinstance(val, (int, float)):
            return float(val)
        if isinstance(val, datetime.datetime):
            return val.timestamp()
        if isinstance(val, str):
            parsed = dt_util.parse_datetime(val)
            return parsed.timestamp() if parsed else None
        return None

    @callback
    def _cleanup_stale_sensors(self) -> None:
        """Remove auto-discovered sensors that have not sent data for longer
        than stale_cleanup_hours.

        Safeguards - ONLY a sensor the user has demonstrably never touched
        is removed:
        - Device renamed (name_by_user), assigned to an area (area_id) or
          labelled -> protected.
        - Any entity of the sensor renamed, assigned to an area, labelled
          or aliased -> protected.
        Such an "adopted" sensor is kept even with an empty battery
        (including its battery-replaced button!).
        - Without a known "last received" timestamp nothing is removed
          (no basis for the decision).
        """
        cutoff = time.time() - self.stale_cleanup_hours * 3600
        ent_reg = er.async_get(self.hass)
        dev_reg = dr.async_get(self.hass)
        all_entries = er.async_entries_for_config_entry(ent_reg, self._entry_id)

        for sensor_id in list(self._discovered):
            last = self._last_seen_epoch(sensor_id)
            if last is None:
                _LOGGER.debug(
                    "Stale cleanup: sensor %s kept (no last-seen timestamp)",
                    sensor_id,
                )
                continue
            if last >= cutoff:
                continue
            device = dev_reg.async_get_device_by_identifier(
                (DOMAIN, f"{self._entry_id}_{sensor_id}"), self._entry_id
            )
            if device and (device.name_by_user or device.area_id or device.labels):
                _LOGGER.debug(
                    "Stale cleanup: sensor %s kept (device customised: "
                    "name_by_user=%s, area=%s, labels=%s)",
                    sensor_id, device.name_by_user, device.area_id, device.labels,
                )
                continue  # touched by the user -> never remove automatically

            uid_prefix = f"{self._entry_id}_{sensor_id}_"
            sensor_entities = [
                reg_entry for reg_entry in all_entries
                if reg_entry.unique_id.startswith(uid_prefix)
            ]
            customised = []
            for reg_entry in sensor_entities:
                reason = None
                if reg_entry.name:
                    reason = f"name={reg_entry.name!r}"
                elif reg_entry.area_id:
                    reason = f"area={reg_entry.area_id!r}"
                elif reg_entry.labels:
                    reason = f"labels={reg_entry.labels!r}"
                # Only count real, non-empty string aliases. Recent HA
                # versions pre-fill every entity's alias list with an
                # internal sentinel (ComputedNameType._singleton), which
                # is truthy and would otherwise mark ALL entities as
                # customised and silently disable the cleanup.
                elif any(
                    isinstance(a, str) and a.strip()
                    for a in (reg_entry.aliases or ())
                ):
                    reason = f"aliases={reg_entry.aliases!r}"
                if reason:
                    customised.append(f"{reg_entry.entity_id} ({reason})")
            if customised:
                _LOGGER.debug(
                    "Stale cleanup: sensor %s kept (customised entities: %s)",
                    sensor_id, customised,
                )
                continue  # at least one entity customised -> keep

            for reg_entry in sensor_entities:
                ent_reg.async_remove(reg_entry.entity_id)
            if device:
                dev_reg.async_remove_device(device.id)

            # Clean up internal state as well
            self.forget_sensor(sensor_id)

            _LOGGER.info(
                "Sensor %s removed automatically: no data for more "
                "than %d h and never customised (stale_cleanup_hours)",
                sensor_id, self.stale_cleanup_hours,
            )

    @callback
    def forget_sensor(self, sensor_id: int | str) -> None:
        """Drop every internal trace of a sensor (does NOT touch the
        registries - the caller has already removed, or is about to remove,
        its device/entities).

        Used by the stale cleanup and by the manual device deletion
        (async_remove_config_entry_device). Without this the sensor stays in
        _discovered with all its channels, so the next packet finds nothing
        "new" to discover and the entities are never recreated.

        Known, deliberately unguarded race: this runs on the event loop
        while the serial reader thread may be in the middle of _parse_line()
        for the same sensor, holding a reference to the very channel set
        popped here. If a packet is being processed at that instant, the
        reader can re-add the sensor and schedule a discovery for channels
        whose entities still exist, so async_add_entities() sees a duplicate
        unique_id - Home Assistant logs and drops it, so the outcome is a
        noisy log line, never a wrong state. A lock around _discovered would
        NOT close the window: the discovery is handed to the event loop with
        call_soon_threadsafe and therefore lands after the critical section
        either way. Closing it properly would mean funnelling entity
        creation through a registry check, which costs far more than the
        one log line it saves.
        """
        self._discovered.pop(sensor_id, None)
        self._sensor_device_infos.pop(sensor_id, None)
        self._battery_notified.discard(sensor_id)
        self._replace_battery.pop(sensor_id, None)
        self._discovery_candidates.pop(sensor_id, None)
        cancel = self._replace_expiry_cancels.pop(sensor_id, None)
        if cancel:
            cancel()
        aliases_changed = False
        for new_id, old_id in list(self._id_aliases.items()):
            if sensor_id in (new_id, old_id):
                del self._id_aliases[new_id]
                aliases_changed = True
        if aliases_changed:
            self._schedule_alias_save()
        for key in [k for k in list(self.sensor_states) if k[0] == sensor_id]:
            self.sensor_states.pop(key, None)
            self._cache.pop(key, None)
            self._outlier_counter.pop(key, None)

    async def async_stop(self) -> None:
        if self._unsub_watchdog:
            self._unsub_watchdog()
            self._unsub_watchdog = None
        for cancel in list(self._replace_expiry_cancels.values()):
            cancel()
        self._replace_expiry_cancels.clear()
        self._stop_event.set()
        self._reset_event.set()
        if self._thread:
            # The reader thread can sit in a blocking readline() for up to
            # serial_timeout seconds (configurable up to 10 s), so a fixed
            # 5 s join would return while the old thread still holds the
            # serial device - and the new coordinator of an options-triggered
            # reload would then fail to open it.
            await self.hass.async_add_executor_job(
                self._thread.join, max(5.0, self.serial_timeout + 3)
            )
        # A reload while debug mode is on would otherwise leave the logger
        # pinned at DEBUG forever: the auto-off timer is cancelled here and
        # the new coordinator starts with debug=False.
        if self.debug and not self.hass.is_stopping:
            self.disable_debug()
        if self._debug_cancel:
            self._debug_cancel()

    # ── Listener pattern ───────────────────────────────────────────────────────

    def _call_soon_threadsafe(self, callback, *args) -> None:
        """Wrapper around hass.loop.call_soon_threadsafe() used by every
        thread-to-loop handoff in this file. During an HA shutdown/reload
        the serial reader thread can outrace the event loop closing - the
        raw call would then raise "Event loop is closed" from inside the
        reader thread's except-handler, which itself calls this again,
        producing a scary but harmless ERROR log entry every time. Nothing
        is lost by skipping the call in that case: the coordinator is being
        torn down anyway."""
        loop = self.hass.loop
        try:
            loop.call_soon_threadsafe(callback, *args)
        except RuntimeError:
            pass

    def async_add_listener(self, listener, sensor_id: int | str | None = None) -> callable:
        """Register a state listener; returns the unregister callable.

        Pass the sensor_id of the entity's sensor to be woken ONLY by
        updates of that sensor. sensor_id=None registers a bridge-level
        broadcast listener (connection state, radio silence, debug switch),
        which is fired by the rare unkeyed _notify_listeners() calls.

        Before this was keyed, every radio packet rewrote the state of every
        entity of every sensor: with ~10 sensors and up to three
        _notify_listeners() calls per LaCrosse packet, that was a couple of
        hundred async_write_ha_state() calls several times a second, for a
        single sensor's temperature having moved by 0.1 degC.
        """
        if sensor_id is None:
            self._state_listeners.append(listener)

            def remove_broadcast():
                if listener in self._state_listeners:
                    self._state_listeners.remove(listener)

            return remove_broadcast

        self._keyed_listeners.setdefault(sensor_id, []).append(listener)

        def remove_keyed():
            listeners = self._keyed_listeners.get(sensor_id)
            if listeners and listener in listeners:
                listeners.remove(listener)
                if not listeners:
                    self._keyed_listeners.pop(sensor_id, None)

        return remove_keyed

    @callback
    def _notify_listeners(self, sensor_id: int | str | None = None) -> None:
        """Refresh listeners. With a sensor_id only that sensor's entities,
        without one every registered listener (bridge-level events)."""
        if sensor_id is not None:
            for cb in list(self._keyed_listeners.get(sensor_id, ())):
                cb()
            return
        for cb in list(self._state_listeners):
            cb()
        for listeners in list(self._keyed_listeners.values()):
            for cb in list(listeners):
                cb()

    # ── Discovery callbacks ────────────────────────────────────────────────────

    def register_discovery_callback(self, cb) -> callable:
        self._discovery_callbacks.append(cb)
        def remove():
            if cb in self._discovery_callbacks:
                self._discovery_callbacks.remove(cb)
        return remove

    @callback
    def _fire_discoveries(self, discoveries: list[SensorDiscovery]) -> None:
        for cb in self._discovery_callbacks:
            cb(discoveries)

    # ── Reset ──────────────────────────────────────────────────────────────────

    def request_reset(self) -> None:
        _LOGGER.info("JeeLink: DTR reset requested")
        self._reset_event.set()

    # ── Debug mode ─────────────────────────────────────────────────────────────

    # Logger name whose level the debug switch drives (same as the
    # "loggers" entry in manifest.json).
    _DEBUG_LOGGER = "custom_components.lacrosse_jeelink"

    def _async_set_log_level(self, level: str) -> None:
        """Ask the logger integration to set this integration's log level.

        Guarded by has_service(): without the logger integration loaded the
        service call raises ServiceNotFound inside a detached task, i.e. an
        unhandled-exception traceback in the log instead of a working (or at
        worst silently skipped) debug switch. manifest.json additionally
        declares "logger" as a dependency so Home Assistant loads it for us
        and the guard practically never trips.
        """
        if not self.hass.services.has_service("logger", "set_level"):
            _LOGGER.debug(
                "logger integration not available - log level left unchanged"
            )
            return
        self.hass.async_create_task(
            self.hass.services.async_call(
                "logger", "set_level", {self._DEBUG_LOGGER: level}
            )
        )

    def enable_debug(self) -> None:
        if not self.debug:
            # Remember what the level was BEFORE we forced it to debug, so
            # disable_debug() can put it back instead of overriding a level
            # the user deliberately configured (logger: YAML, logger.set_level).
            self._debug_prev_level = logging.getLogger(self._DEBUG_LOGGER).level
        self.debug = True
        _LOGGER.info(
            "[DEBUG] Debug mode enabled - auto-off in %ds", self.debug_timeout
        )
        if self._debug_cancel:
            self._debug_cancel()
        self._debug_cancel = async_call_later(
            self.hass, self.debug_timeout, self._debug_auto_off
        )
        self._async_set_log_level("debug")
        self._notify_listeners()

    def disable_debug(self) -> None:
        self.debug = False
        if self._debug_cancel:
            self._debug_cancel()
            self._debug_cancel = None
        _LOGGER.info("[DEBUG] Debug mode disabled")
        # Restore the remembered level (NOTSET = "inherit again", which is
        # what an untouched logger has) rather than hardcoding "warning".
        level = logging.NOTSET if self._debug_prev_level is None else self._debug_prev_level
        self._debug_prev_level = None
        self._async_set_log_level(_level_name(level))
        self._notify_listeners()

    @callback
    def _debug_auto_off(self, _now=None) -> None:
        """Auto-off timer: same teardown as the switch, no duplicated body."""
        _LOGGER.info(
            "[DEBUG] Debug mode ended automatically after %ds", self.debug_timeout
        )
        self._debug_cancel = None  # this very timer just fired
        self.disable_debug()

    def _debug_log(self, msg: str) -> None:
        if self.debug:
            _LOGGER.info("[DEBUG] %s", msg)

    # ── Serial reader ──────────────────────────────────────────────────────────

    def _init_command_bytes(self, include_version: bool = True) -> bytes:
        """Build the byte sequence of configured firmware init commands.

        Space-separated commands (equivalent to FHEM's initCommands
        attribute), each terminated with CR. "v" (request the firmware
        banner) is appended for deterministic stick identification.
        """
        commands = [c for c in self.init_commands.split() if c]
        if include_version:
            commands.append("v")
        return "".join(f"{c}\r" for c in commands).encode()

    def _dtr_reset(self, ser: serial.Serial) -> None:
        try:
            ser.setDTR(False)
            time.sleep(0.25)
            ser.setDTR(True)
            time.sleep(2.0)
            _LOGGER.info("JeeLink hardware reset (DTR) performed")
        except Exception as exc:
            _LOGGER.warning("DTR reset failed: %s", exc)

    @callback
    def _async_apply_firmware(self) -> None:
        """Store the detected firmware as sw_version on the bridge device
        (analogous to FHEM's model/settings internals).

        Only ever reached from the serial reader thread, which is started
        after register_bridge_device(), so the bridge's registry id is
        already known and no lookup is needed."""
        if self._bridge_device_id:
            dr.async_get(self.hass).async_update_device(
                self._bridge_device_id, sw_version=self.firmware
            )
        self._notify_listeners()

    def _set_connected(self, connected: bool, reason: str = "") -> None:
        """Maintain the connection state + notify on changes."""
        if connected == self.connected:
            return
        self.connected = connected
        if connected:
            if self._conn_notified_down:
                self._conn_notified_down = False
                self._notify_user(
                    "connection",
                    "JeeLink: Verbindung wiederhergestellt",
                    "JeeLink: connection restored",
                )
        else:
            if not self._conn_notified_down:
                self._conn_notified_down = True
                self._notify_user(
                    "connection",
                    f"JeeLink: Verbindung verloren ({reason})",
                    f"JeeLink: connection lost ({reason})",
                )
        self._call_soon_threadsafe(self._notify_listeners)

    def _serial_loop(self) -> None:
        ser = None
        while not self._stop_event.is_set():
            try:
                # stty is a Linux-specific safety net (raw mode, no echo)
                # for CH340 adapters. Best effort: on systems without stty
                # pyserial sets the parameters itself.
                try:
                    subprocess.run(
                        ["stty", "-F", self.serial_port, "57600", "raw",
                         "-echo", "-echoe", "-echok"],
                        check=True, capture_output=True
                    )
                except (OSError, subprocess.CalledProcessError) as exc:
                    _LOGGER.debug("stty skipped (%s)", exc)
                _LOGGER.info("Connecting to %s...", self.serial_port)
                # write_timeout: a wedged USB-CDC adapter can otherwise block
                # ser.write() forever, and with it the reader thread - which
                # a clean shutdown/reload then waits for in vain.
                ser = serial.Serial(
                    self.serial_port,
                    57600,
                    timeout=self.serial_timeout,
                    write_timeout=2,
                )
                self._dtr_reset(ser)
                # Configured init commands (default "7m 10t" = data-rate
                # toggle) plus "v" to request the firmware identification
                # banner (usually also emitted after the DTR reset - "v"
                # makes it deterministic).
                ser.write(self._init_command_bytes())
                _LOGGER.info(
                    "JeeLink initialised (commands: %s), reading data...",
                    self.init_commands,
                )
                self._set_connected(True)
                # Baseline for the radio-silence watchdog: count from NOW,
                # not from the last packet before a connection loss.
                self.last_data_ts = time.time()
                self._reset_event.clear()

                # try/finally: release the device promptly on the reset break
                # and on stop, instead of leaving the close to pyserial's
                # __del__/GC. The next connect attempt (or the coordinator of
                # a reload) would otherwise find the port still open.
                try:
                    while not self._stop_event.is_set():
                        if self._reset_event.is_set():
                            _LOGGER.info("Reset signal received - closing serial for reconnect")
                            self._reset_event.clear()
                            break

                        line = ser.readline().decode("utf-8", errors="ignore").strip()
                        if not line:
                            continue

                        if "drecvintr exit" in line:
                            _LOGGER.warning("Firmware: drecvintr exit - DTR reset")
                            self._dtr_reset(ser)
                            ser.write(self._init_command_bytes(include_version=False))
                            continue

                        if "RFM12 hang" in line:
                            _LOGGER.warning("Firmware: RFM12 hang - DTR reset")
                            self._dtr_reset(ser)
                            ser.write(self._init_command_bytes(include_version=False))
                            continue

                        if line.startswith("[") and line.endswith("]"):
                            fw = line[1:-1].strip()
                            if fw and fw != self.firmware:
                                self.firmware = fw
                                _LOGGER.info("JeeLink firmware detected: %s", fw)
                                self._call_soon_threadsafe(
                                    self._async_apply_firmware
                                )
                            continue

                        if line.startswith("OK 9"):
                            parser = self._parse_line
                        elif line.startswith("OK EMT7110"):
                            parser = self._parse_emt7110_line
                        elif line.startswith("OK LS"):
                            parser = self._parse_levelsender_line
                        else:
                            continue

                        # Decoding is isolated from the connection: the
                        # parsers handle their own (IndexError, ValueError),
                        # but anything else - a dict mutated from the event
                        # loop while the reader iterates it, a bug in a
                        # future parser - used to escape into the outer
                        # handler below, which tears the port down, notifies
                        # the user about a "lost connection" and reconnects
                        # with a DTR reset. One garbled line is worth one
                        # discarded line, not a full reconnect cycle.
                        try:
                            parser(line)
                        except Exception:  # noqa: BLE001
                            _LOGGER.exception(
                                "Unhandled error decoding line %r", line
                            )
                finally:
                    ser.close()

            except Exception as exc:
                _LOGGER.error(
                    "Serial error: %s - reconnect in %ds", exc, self.reconnect_delay
                )
                self._set_connected(False, str(exc))
                try:
                    if ser:
                        ser.close()
                except Exception:
                    pass
                # Wait in short steps so an unload does not block
                deadline = time.time() + self.reconnect_delay
                while time.time() < deadline and not self._stop_event.is_set():
                    time.sleep(0.5)

    # ── Protocol parsing (as per 36_LaCrosse.pm) ───────────────────────────────
    #
    # The LaCrosseITPlusReader sketch running on the stick also decodes two
    # other telegram families on the same serial port: EMT7110 (LaCrosse
    # power/energy plug) and LevelSender (DIY tank/cistern fill-level
    # sender). Both are parsed below and kept in their own ID namespace
    # ("emt_<id>" / "ls_<id>") - see SensorDiscovery and
    # get_sensor_device_info() - so they never collide with a LaCrosse IT+
    # radio ID and never show up mislabelled as a "LaCrosse" sensor.

    def _mark_packet_received(self) -> None:
        """Feed the radio-silence watchdog and send the all-clear if needed.

        Shared by every protocol parser below: a received packet of ANY kind
        (LaCrosse, EMT7110, LevelSender) counts as radio activity for the
        watchdog - called as soon as the line has a plausible shape (right
        prefix, enough fields), not only after the values were decoded: a
        malformed field is a decode problem, not radio silence.
        """
        self.last_data_ts = time.time()
        if self._data_timeout_notified:
            self._data_timeout_notified = False
            self._call_soon_threadsafe(self._notify_listeners)
            self._notify_user(
                "data_timeout",
                "JeeLink: Funkdaten werden wieder empfangen",
                "JeeLink: radio data is coming in again",
            )

    def _passes_discovery_threshold(self, resolved_id: int | str) -> bool:
        """Like FHEM's autoCreateThreshold: a brand-new sensor is only
        created after discovery_min_packets packets within
        discovery_window_sec seconds. Shared by all protocol parsers.
        Returns False while the candidate hasn't reached the threshold yet
        (caller should stop processing this packet in that case).
        """
        if not (
            self.auto_add
            and self.discovery_min_packets > 1
            and resolved_id not in self._discovered
        ):
            return True
        now = time.time()
        first_ts, count = self._discovery_candidates.get(resolved_id, (now, 0))
        if now - first_ts > self.discovery_window_sec:
            first_ts, count = now, 0  # window expired -> start over
        count += 1
        if count < self.discovery_min_packets:
            self._discovery_candidates[resolved_id] = (first_ts, count)
            self._debug_log(
                f"sensor {resolved_id}: discovery threshold "
                f"{count}/{self.discovery_min_packets} within "
                f"{self.discovery_window_sec}s - not created yet"
            )
            return False
        self._discovery_candidates.pop(resolved_id, None)
        return True

    def _parse_line(self, line: str) -> None:
        """Parse a single OK-9 telegram.

        Byte layout (identical to FHEM 36_LaCrosse.pm lines 213-220):
          parts[2]  sensor ID
          parts[3]  flags: bit7=new_battery, bits4-6=type, bits0-3=channel
          parts[4]  temp MSB
          parts[5]  temp LSB
          parts[6]  hum byte: bit7=battery_low, bits0-6=humidity (valid 1-100)
        """
        try:
            parts = line.split()
            # EXACTLY 7 tokens ("OK", "9", id, flags, tMSB, tLSB, hum) - not
            # "at least". pyserial's readline() returns whatever it has when
            # its timeout expires mid-line, so a truncated packet is a
            # routine event, and a line cut right after the temperature bytes
            # used to be accepted as a complete temperature-only reading with
            # no humidity byte - which also means no battery bit, i.e. a
            # spurious battery_low=False. That flipped the battery sensor off
            # for one packet and made the next complete packet fire the
            # "battery low" notification all over again.
            if len(parts) != 7:
                return

            self._mark_packet_received()

            sensor_id = int(parts[2])
            flags     = int(parts[3])
            t_msb     = int(parts[4])
            t_lsb     = int(parts[5])
            hum_raw   = int(parts[6])

            # Temperature
            temperature = round((t_msb * 256 + t_lsb - 1000) / 10.0, 1)

            # Flags byte (parts[3])
            battery_new = bool(flags & 0x80)          # bit 7: new battery inserted
            channel     = flags & 0x0F                 # bits 0-3: channel (1=main, 2=probe2)
            is_probe2   = (channel == 2)

            # Humidity byte (parts[6])
            # Bit 7 = weak battery (battery_low), bits 0-6 = humidity
            battery_low = bool(hum_raw & 0x80)
            hum_masked  = hum_raw & 0x7F               # mask off the battery bit
            # Valid: 1-100; >100 = no humidity sensor (e.g. 106 = temp-only)
            hum = hum_masked if 1 <= hum_masked <= 100 else None

            # ── Battery replacement detection (like FHEM replaceBatteryForSec) ─
            # Unknown ID with battery_new -> check if a sensor is in replace mode
            is_unknown = (sensor_id not in self._discovered
                          and sensor_id not in self._id_aliases)
            if battery_new and is_unknown:
                now = time.time()
                for old_id, expiry in list(self._replace_battery.items()):
                    if now < expiry:
                        _LOGGER.info(
                            "Battery replacement detected: sensor %d has new ID %d - "
                            "alias stored, existing entities are kept",
                            old_id, sensor_id,
                        )
                        self._id_aliases[sensor_id] = old_id
                        del self._replace_battery[old_id]
                        self._schedule_alias_save()
                        label = self._device_label(old_id)
                        self._notify_user(
                            "battery_replaced",
                            f"{label}: Batteriewechsel erkannt "
                            f"(neue Funk-ID {sensor_id})",
                            f"{label}: battery replacement detected "
                            f"(new radio ID {sensor_id})",
                        )
                        self._call_soon_threadsafe(
                            self._notify_listeners, old_id
                        )
                        break

            # Resolve the ID via the alias table (returns old_id after a swap)
            resolved_id = self._resolve_id(sensor_id)

            self._debug_log(
                f"raw={line} | id={sensor_id} resolved={resolved_id} ch={channel} "
                f"T={temperature} degC hum_raw={hum_raw} "
                f"hum_masked={hum_masked} hum={hum}% "
                f"bat_low={battery_low} bat_new={battery_new} probe2={is_probe2}"
            )

            if battery_new and resolved_id == sensor_id:
                _LOGGER.info(
                    "JeeLink sensor %d: new battery inserted "
                    "(no battery replacement mode armed)",
                    sensor_id,
                )

            # ── Discovery threshold (like FHEM autoCreateThreshold) ────────
            # A brand-new sensor is only created after N packets within T
            # seconds. Supported IT+ sensors transmit every 4-8 s, so any
            # real sensor passes within seconds - one-shot decode flukes and
            # fringe receptions never make it into the registry. Aliased IDs
            # (battery replacement) resolve to a known sensor and are not
            # affected. Shared with EMT7110/LevelSender via
            # _passes_discovery_threshold().
            if not self._passes_discovery_threshold(resolved_id):
                return

            # Which channels are new for this sensor?
            temp_channel = "temperature2" if is_probe2 else "temperature"
            new_discoveries: list[SensorDiscovery] = []
            was_known = resolved_id in self._discovered

            if self.auto_add:
                # _discovered is only touched when discovery is actually
                # enabled: otherwise a single stray packet from a neighbour's
                # sensor would mark it as "known" without ever creating an
                # entity for it - which produced battery-low notifications
                # and "removed automatically" cleanup logs for phantom
                # sensors, and blocked the battery-replacement alias for a
                # real sensor landing on such an ID.
                known = self._discovered.setdefault(resolved_id, set())
                if temp_channel not in known:
                    known.add(temp_channel)
                    new_discoveries.append(SensorDiscovery(resolved_id, temp_channel))
                # Humidity entity only when a real reading was received (1-100%)
                if hum is not None and "humidity" not in known:
                    known.add("humidity")
                    new_discoveries.append(SensorDiscovery(resolved_id, "humidity"))
                if "battery" not in known:
                    known.add("battery")
                    new_discoveries.append(SensorDiscovery(resolved_id, "battery"))
                # "Last received" timestamp per sensor
                if "last_seen" not in known:
                    known.add("last_seen")
                    new_discoveries.append(SensorDiscovery(resolved_id, "last_seen"))
                # Create the battery-replaced button once per sensor
                if "replace_battery" not in known:
                    known.add("replace_battery")
                    new_discoveries.append(SensorDiscovery(resolved_id, "replace_battery"))

                if new_discoveries:
                    self._call_soon_threadsafe(
                        self._fire_discoveries, new_discoveries
                    )
                    if not was_known:
                        self._notify_user(
                            "new_sensor",
                            f"LaCrosse: neuer Sensor {resolved_id} erkannt "
                            f"({temperature} °C"
                            + (f", {hum} %" if hum is not None else "")
                            + ")",
                            f"LaCrosse: new sensor {resolved_id} discovered "
                            f"({temperature} °C"
                            + (f", {hum} %" if hum is not None else "")
                            + ")",
                        )

            if resolved_id not in self._discovered:
                # Not a sensor of ours: auto-discovery is off and this ID was
                # never adopted (no entities exist and none will be created).
                # Everything below only feeds entities, so tracking it would
                # just accumulate state - outlier cache, last-seen, and above
                # all _battery_notified - for a neighbour's sensor.
                return

            # Temperature update
            temp_key = (resolved_id, temp_channel)
            temp_ok = self._check_temperature(temp_key, temperature, line)
            self._debug_log(
                f"sensor {resolved_id} {temp_channel}: {temperature} degC "
                f"-> {'OK' if temp_ok else 'FILTERED'}"
            )
            if temp_ok:
                self._update(temp_key, temperature)

            # Humidity update (real values 1-100% only)
            if hum is not None:
                hum_key = (resolved_id, "humidity")
                hum_ok = self._check_humidity(hum_key, hum, line)
                self._debug_log(
                    f"sensor {resolved_id} humidity: {hum}% "
                    f"-> {'OK' if hum_ok else 'FILTERED'}"
                )
                if hum_ok:
                    self._update(hum_key, hum)

                    # Calculate the dew point (like FHEM doDewpoint)
                    if temp_ok:
                        try:
                            dp = round(_dewpoint(temperature, hum), 1)
                            self._update((resolved_id, "dewpoint"), dp)
                        except (ValueError, ZeroDivisionError):
                            pass

            # "Last received": every parsed packet counts, even if the
            # reading was rejected by the outlier filter - received is
            # received. Quantised to full minutes so that not every
            # 4-second packet creates a new state (database row).
            self._update((resolved_id, "last_seen"), int(time.time() // 60 * 60))

            # Battery update (+ one-time message on transition to "low")
            if battery_low and resolved_id not in self._battery_notified:
                self._battery_notified.add(resolved_id)
                # Only notify for already known sensors (no spam right at
                # the first discovery of a sensor with an empty battery).
                if was_known:
                    label = self._device_label(resolved_id)
                    self._notify_user(
                        "battery_low",
                        f"{label}: Batterie schwach",
                        f"{label}: battery low",
                    )
            elif not battery_low:
                self._battery_notified.discard(resolved_id)
            self._update((resolved_id, "battery"), battery_low)

        except (IndexError, ValueError) as exc:
            _LOGGER.error("Parse error '%s': %s", line, exc)

    # ── EMT7110 (LaCrosse power/energy plug) ────────────────────────────────

    def _parse_emt7110_line(self, line: str) -> None:
        """Parse an EMT7110 telegram (LaCrosseITPlusReader EMT7110.cpp).

        Format:
          OK EMT7110 <idMSB> <idLSB> <voltMSB> <voltLSB> <curMSB> <curLSB>
                     <powMSB> <powLSB> <accMSB> <accLSB> <flags>
            volt  = (MSB*256+LSB) / 10   V
            cur   =  MSB*256+LSB         mA
            pow   =  MSB*256+LSB         W
            acc   = (MSB*256+LSB) / 100  kWh (accumulated energy)
            flags: bit0 = consumer connected, bit1 = pairing telegram
                   (no real measurement data - discarded)

        Not a LaCrosse IT+ weather sensor, so it gets its own "emt_<id>" ID
        namespace and its own device info (see get_sensor_device_info) -
        the 16-bit EMT7110 ID would otherwise be indistinguishable from,
        and could collide with, an 8-bit LaCrosse radio ID.
        """
        try:
            parts = line.split()
            # EXACTLY 13 tokens ("OK", "EMT7110", 10 data bytes, flags) - see
            # _parse_line: a readline() cut short by its timeout must not be
            # decoded as if the missing tail were simply absent.
            if len(parts) != 13:
                return

            self._mark_packet_received()

            raw_id = (int(parts[2]) << 8) | int(parts[3])
            flags = int(parts[12])
            if flags & 0x02:
                # Pairing telegram - no measurement data to update
                return
            connected = bool(flags & 0x01)

            voltage = ((int(parts[4]) << 8) | int(parts[5])) / 10.0
            current = (int(parts[6]) << 8) | int(parts[7])
            power = (int(parts[8]) << 8) | int(parts[9])
            energy = ((int(parts[10]) << 8) | int(parts[11])) / 100.0

            resolved_id = f"emt_{raw_id}"

            self._debug_log(
                f"raw={line} | EMT7110 id={resolved_id} V={voltage} "
                f"mA={current} W={power} kWh={energy} connected={connected}"
            )

            if not self._passes_discovery_threshold(resolved_id):
                return

            new_discoveries: list[SensorDiscovery] = []
            was_known = resolved_id in self._discovered

            if self.auto_add:
                known = self._discovered.setdefault(resolved_id, set())
                for channel in (
                    "voltage", "current", "power", "energy", "connected", "last_seen",
                ):
                    if channel not in known:
                        known.add(channel)
                        new_discoveries.append(SensorDiscovery(resolved_id, channel))

                if new_discoveries:
                    self._call_soon_threadsafe(
                        self._fire_discoveries, new_discoveries
                    )
                    if not was_known:
                        self._notify_user(
                            "new_sensor",
                            f"LaCrosse: neuer EMT7110-Zähler {raw_id} erkannt "
                            f"({power} W, {voltage} V)",
                            f"LaCrosse: new EMT7110 meter {raw_id} discovered "
                            f"({power} W, {voltage} V)",
                        )

            if resolved_id not in self._discovered:
                return  # see _parse_line: unknown and never to be created

            self._update((resolved_id, "voltage"), round(voltage, 1))
            self._update((resolved_id, "current"), current)
            self._update((resolved_id, "power"), power)
            self._update((resolved_id, "energy"), round(energy, 2))
            self._update((resolved_id, "connected"), connected)
            self._update((resolved_id, "last_seen"), int(time.time() // 60 * 60))

        except (IndexError, ValueError) as exc:
            _LOGGER.error("EMT7110 parse error '%s': %s", line, exc)

    # ── LevelSender (DIY tank/cistern fill-level sender) ───────────────────

    def _parse_levelsender_line(self, line: str) -> None:
        """Parse a LevelSender telegram (LaCrosseITPlusReader LevelSenderLib.cpp).

        Format:
          OK LS <id> 0 <levelMSB> <levelLSB> <tempMSB> <tempLSB> <voltByte>
            level = (MSB*256+LSB - 1000) / 10   cm
            temp  = (MSB*256+LSB - 1000) / 10   degC
            volt  =  voltByte / 10               V

        Not a LaCrosse product (a DIY/community sender), and its ID space is
        only 4 bit (0-15) - would otherwise silently collide with a
        LaCrosse IT+ sensor of the same radio ID. Gets its own "ls_<id>" ID
        namespace and device info instead.
        """
        try:
            parts = line.split()
            # EXACTLY 9 tokens ("OK", "LS", id, 0, level MSB/LSB, temp
            # MSB/LSB, voltage). A minimum check let a line truncated right
            # before the voltage byte through with parts[8] being whatever
            # remained of the previous field - turning a healthy sender into
            # a bogus low-battery reading.
            if len(parts) != 9:
                return

            self._mark_packet_received()

            raw_id = int(parts[2])
            level_raw = (int(parts[4]) << 8) | int(parts[5])
            level = (level_raw - 1000) / 10.0
            temp_raw = (int(parts[6]) << 8) | int(parts[7])
            temperature = round((temp_raw - 1000) / 10.0, 1)
            voltage = int(parts[8]) / 10.0

            resolved_id = f"ls_{raw_id}"

            self._debug_log(
                f"raw={line} | LevelSender id={resolved_id} level={level}cm "
                f"T={temperature}degC V={voltage}"
            )

            if not self._passes_discovery_threshold(resolved_id):
                return

            new_discoveries: list[SensorDiscovery] = []
            was_known = resolved_id in self._discovered

            if self.auto_add:
                known = self._discovered.setdefault(resolved_id, set())
                for channel in ("level", "temperature", "voltage", "last_seen"):
                    if channel not in known:
                        known.add(channel)
                        new_discoveries.append(SensorDiscovery(resolved_id, channel))

                if new_discoveries:
                    self._call_soon_threadsafe(
                        self._fire_discoveries, new_discoveries
                    )
                    if not was_known:
                        self._notify_user(
                            "new_sensor",
                            f"LaCrosse: neuer LevelSender {raw_id} erkannt "
                            f"({level} cm, {temperature} °C)",
                            f"LaCrosse: new LevelSender {raw_id} discovered "
                            f"({level} cm, {temperature} °C)",
                        )

            if resolved_id not in self._discovered:
                return  # see _parse_line: unknown and never to be created

            # Reuse the generic temperature outlier filter (absolute +
            # delta bounds) - same physical quantity as LaCrosse sensors.
            temp_key = (resolved_id, "temperature")
            if self._check_temperature(temp_key, temperature, line):
                self._update(temp_key, temperature)

            self._update((resolved_id, "level"), round(level, 1))
            self._update((resolved_id, "voltage"), round(voltage, 1))
            self._update((resolved_id, "last_seen"), int(time.time() // 60 * 60))

        except (IndexError, ValueError) as exc:
            _LOGGER.error("LevelSender parse error '%s': %s", line, exc)

    def _update(self, key: tuple, value) -> None:
        if self._cache.get(key) != value:
            self._cache[key] = value
            self.sensor_states[key] = value
            # Keyed on the sensor: only that sensor's entities rewrite their
            # state, not every entity of every sensor (see async_add_listener).
            self._call_soon_threadsafe(self._notify_listeners, key[0])

    def _check_temperature(self, key: tuple, temperature: float, raw: str) -> bool:
        if not (DEFAULT_TEMP_MIN <= temperature <= DEFAULT_TEMP_MAX):
            _LOGGER.warning(
                "[JeeLink] Temperature outlier (absolute): sensor=%s T=%s degC "
                "(allowed %s..%s) | raw: %s",
                key[0], temperature, DEFAULT_TEMP_MIN, DEFAULT_TEMP_MAX, raw,
            )
            self._outlier_counter.pop(key, None)
            return False
        last = self._cache.get(key)
        if last is not None:
            try:
                delta = abs(temperature - float(last))
                if delta > DEFAULT_TEMP_MAX_DELTA:
                    # Only CONSISTENT readings confirm an outlier: a genuine
                    # jump (sensor moved, long dropout) keeps reporting the
                    # new level, while random decode garbage differs wildly
                    # from packet to packet and restarts the count. The
                    # previous value was stored but never compared, so N
                    # arbitrary garbage values confirmed each other.
                    # Deliberately not an equality check on the stored value
                    # (as the option text claimed): real readings fluctuate by
                    # 0.1 degC, so a sensor would stay stuck on its old value
                    # forever after a real jump.
                    prev, count = self._outlier_counter.get(key, (None, 0))
                    if prev is None or abs(temperature - prev) > DEFAULT_TEMP_MAX_DELTA:
                        count = 1
                    else:
                        count += 1
                    self._outlier_counter[key] = (temperature, count)
                    if count >= self.outlier_confirm_count:
                        _LOGGER.info(
                            "[JeeLink] Outlier confirmed (%dx consistent): "
                            "sensor=%s %s->%s degC | raw: %s",
                            count, key[0], last, temperature, raw,
                        )
                        self._outlier_counter.pop(key, None)
                        return True
                    _LOGGER.warning(
                        "[JeeLink] Temperature outlier (delta): sensor=%s %s->%s degC "
                        "(d%.1f > %s, %d/%d) | raw: %s",
                        key[0], last, temperature, delta, DEFAULT_TEMP_MAX_DELTA,
                        count, self.outlier_confirm_count, raw,
                    )
                    return False
            except (TypeError, ValueError):
                pass
        self._outlier_counter.pop(key, None)
        return True

    def _check_humidity(self, key: tuple, humidity: int, raw: str) -> bool:
        # Absolute check: 1-100% (FHEM: $humidity && $humidity <= 100)
        if not (1 <= humidity <= 100):
            _LOGGER.warning(
                "[JeeLink] Humidity outlier (absolute): sensor=%s %d%% | raw: %s",
                key[0], humidity, raw,
            )
            self._outlier_counter.pop(key, None)
            return False
        last = self._cache.get(key)
        if last is not None:
            try:
                delta = abs(humidity - float(last))
                if delta > DEFAULT_HUM_MAX_DELTA:
                    # Same consistency check as in _check_temperature
                    prev, count = self._outlier_counter.get(key, (None, 0))
                    if prev is None or abs(humidity - prev) > DEFAULT_HUM_MAX_DELTA:
                        count = 1
                    else:
                        count += 1
                    self._outlier_counter[key] = (humidity, count)
                    if count >= self.outlier_confirm_count:
                        _LOGGER.info(
                            "[JeeLink] Outlier confirmed (%dx consistent): "
                            "sensor=%s %s->%d%% | raw: %s",
                            count, key[0], last, humidity, raw,
                        )
                        self._outlier_counter.pop(key, None)
                        return True
                    _LOGGER.warning(
                        "[JeeLink] Humidity outlier (delta): sensor=%s %s->%d%% "
                        "(d%.1f > %s, %d/%d) | raw: %s",
                        key[0], last, humidity, delta, DEFAULT_HUM_MAX_DELTA,
                        count, self.outlier_confirm_count, raw,
                    )
                    return False
            except (TypeError, ValueError):
                pass
        self._outlier_counter.pop(key, None)
        return True
