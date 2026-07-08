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

from .const import (
    DOMAIN,
    CONF_SERIAL_PORT,
    CONF_AUTO_ADD,
    CONF_BATTERY_REPLACE_TIMEOUT,
    CONF_DATA_TIMEOUT,
    CONF_DEBUG_TIMEOUT,
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
    sensor_id: int
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
        self._discovered: dict[int, set[str]] = {}

        # (sensor_id, channel) -> current value
        self.sensor_states: dict[tuple[int, str], object] = {}

        # Cache for outlier checking
        self._cache: dict[tuple[int, str], object] = {}

        # Outlier confirmation: key -> (value, consecutive_count)
        self._outlier_counter: dict[tuple, tuple] = {}

        self._state_listeners: list = []
        self._discovery_callbacks: list = []

        # Battery replacement: new_id -> old_id (alias after ID change)
        self._id_aliases: dict[int, int] = {}
        # Battery replacement mode active: old_id -> expiry timestamp
        self._replace_battery: dict[int, float] = {}

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
        self._sensor_device_infos: dict[int, DeviceInfo] = {}

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
        """'en' for an English HA system language, otherwise 'de' (fallback)."""
        lang = (self.hass.config.language or "de").lower()
        return "en" if lang.startswith("en") else "de"

    def _notify_user(self, category: str, message_de: str, message_en: str) -> None:
        """Send a message to the configured notify entity.

        Thread-safe (may be called from the serial reader thread). Nothing
        happens without a configured entity, with notify_enabled=False, or
        with the message type (category) switched off. Delivery errors must
        never disturb operation.
        """
        if not self.notify_enabled or not self.notify_entity:
            return
        if not self.notify_types.get(category, True):
            return
        message = message_en if self._lang() == "en" else message_de

        async def _send() -> None:
            try:
                await self.hass.services.async_call(
                    "notify",
                    "send_message",
                    {"entity_id": self.notify_entity, "message": message},
                    blocking=False,
                )
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning(
                    "Notification via %s failed: %s", self.notify_entity, err
                )

        self.hass.loop.call_soon_threadsafe(
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
        self._notify_listeners()
        # Refresh entities once the window expires so the button attribute
        # replace_active does not incorrectly stay "true" until the next
        # received packet.
        async_call_later(self.hass, timeout + 1, lambda _now: self._notify_listeners())

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
        """Persist the alias table (thread-safe from the reader thread)."""
        data = {str(k): v for k, v in self._id_aliases.items()}

        def _save() -> None:
            self._alias_store.async_delay_save(lambda: data, 1.0)

        self.hass.loop.call_soon_threadsafe(_save)

    def get_sensor_device_info(self, sensor_id: int) -> DeviceInfo:
        """DeviceInfo for a single LaCrosse sensor, linked to the bridge."""
        if sensor_id not in self._sensor_device_infos:
            self._sensor_device_infos[sensor_id] = DeviceInfo(
                identifiers={(DOMAIN, f"{self._entry_id}_{sensor_id}")},
                name=f"LaCrosse Sensor {sensor_id}",
                manufacturer="LaCrosse Technology",
                model="IT+ Sensor",
                serial_number=str(sensor_id),
                via_device=(DOMAIN, self._entry_id),
            )
        return self._sensor_device_infos[sensor_id]

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
        )
        discoveries: list[SensorDiscovery] = []
        for entity_entry in known_entries:
            uid = entity_entry.unique_id
            if not uid.startswith(prefix):
                continue
            rest = uid[len(prefix):]
            for suffix, channel in suffix_to_channel:
                if rest.endswith(suffix):
                    try:
                        sensor_id = int(rest[: -len(suffix)])
                    except ValueError:
                        break
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

    def _last_seen_epoch(self, sensor_id: int) -> float | None:
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
                    "Stale cleanup: sensor %d kept (no last-seen timestamp)",
                    sensor_id,
                )
                continue
            if last >= cutoff:
                continue
            device = dev_reg.async_get_device(
                identifiers={(DOMAIN, f"{self._entry_id}_{sensor_id}")}
            )
            if device and (device.name_by_user or device.area_id or device.labels):
                _LOGGER.debug(
                    "Stale cleanup: sensor %d kept (device customised: "
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
                    "Stale cleanup: sensor %d kept (customised entities: %s)",
                    sensor_id, customised,
                )
                continue  # at least one entity customised -> keep

            for reg_entry in sensor_entities:
                ent_reg.async_remove(reg_entry.entity_id)
            if device:
                dev_reg.async_remove_device(device.id)

            # Clean up internal state as well
            self._discovered.pop(sensor_id, None)
            self._sensor_device_infos.pop(sensor_id, None)
            self._battery_notified.discard(sensor_id)
            self._replace_battery.pop(sensor_id, None)
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

            _LOGGER.info(
                "LaCrosse sensor %d removed automatically: no data for more "
                "than %d h and never customised (stale_cleanup_hours)",
                sensor_id, self.stale_cleanup_hours,
            )

    async def async_stop(self) -> None:
        if self._unsub_watchdog:
            self._unsub_watchdog()
            self._unsub_watchdog = None
        self._stop_event.set()
        self._reset_event.set()
        if self._thread:
            await self.hass.async_add_executor_job(self._thread.join, 5)
        if self._debug_cancel:
            self._debug_cancel()

    # ── Listener pattern ───────────────────────────────────────────────────────

    def async_add_listener(self, listener) -> callable:
        self._state_listeners.append(listener)
        def remove():
            if listener in self._state_listeners:
                self._state_listeners.remove(listener)
        return remove

    @callback
    def _notify_listeners(self) -> None:
        for cb in self._state_listeners:
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

    def enable_debug(self) -> None:
        self.debug = True
        _LOGGER.info(
            "[DEBUG] Debug mode enabled - auto-off in %ds", self.debug_timeout
        )
        if self._debug_cancel:
            self._debug_cancel()
        self._debug_cancel = async_call_later(
            self.hass, self.debug_timeout, self._debug_auto_off
        )
        self.hass.async_create_task(
            self.hass.services.async_call(
                "logger", "set_level",
                {"custom_components.lacrosse_jeelink": "debug"},
            )
        )
        self._notify_listeners()

    def disable_debug(self) -> None:
        self.debug = False
        if self._debug_cancel:
            self._debug_cancel()
            self._debug_cancel = None
        _LOGGER.info("[DEBUG] Debug mode disabled")
        self.hass.async_create_task(
            self.hass.services.async_call(
                "logger", "set_level",
                {"custom_components.lacrosse_jeelink": "warning"},
            )
        )
        self._notify_listeners()

    @callback
    def _debug_auto_off(self, _now=None) -> None:
        self.debug = False
        self._debug_cancel = None
        _LOGGER.info(
            "[DEBUG] Debug mode ended automatically after %ds", self.debug_timeout
        )
        self.hass.async_create_task(
            self.hass.services.async_call(
                "logger", "set_level",
                {"custom_components.lacrosse_jeelink": "warning"},
            )
        )
        self._notify_listeners()

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
        (analogous to FHEM's model/settings internals)."""
        dev_reg = dr.async_get(self.hass)
        device = dev_reg.async_get_device(identifiers={(DOMAIN, self._entry_id)})
        if device:
            dev_reg.async_update_device(device.id, sw_version=self.firmware)
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
        self.hass.loop.call_soon_threadsafe(self._notify_listeners)

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
                ser = serial.Serial(self.serial_port, 57600, timeout=self.serial_timeout)
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
                            self.hass.loop.call_soon_threadsafe(
                                self._async_apply_firmware
                            )
                        continue

                    if line.startswith("OK 9"):
                        self._parse_line(line)

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
            if len(parts) < 6:
                return

            # Feed the radio-silence watchdog + send the all-clear if needed
            self.last_data_ts = time.time()
            if self._data_timeout_notified:
                self._data_timeout_notified = False
                self.hass.loop.call_soon_threadsafe(self._notify_listeners)
                self._notify_user(
                    "data_timeout",
                    "JeeLink: Funkdaten werden wieder empfangen",
                    "JeeLink: radio data is coming in again",
                )

            sensor_id = int(parts[2])
            flags     = int(parts[3])
            t_msb     = int(parts[4])
            t_lsb     = int(parts[5])
            hum_raw   = int(parts[6]) if len(parts) > 6 else None

            # Temperature
            temperature = round((t_msb * 256 + t_lsb - 1000) / 10.0, 1)

            # Flags byte (parts[3])
            battery_new = bool(flags & 0x80)          # bit 7: new battery inserted
            channel     = flags & 0x0F                 # bits 0-3: channel (1=main, 2=probe2)
            is_probe2   = (channel == 2)

            # Humidity byte (parts[6])
            # Bit 7 = weak battery (battery_low), bits 0-6 = humidity
            if hum_raw is not None:
                battery_low = bool(hum_raw & 0x80)
                hum_masked  = hum_raw & 0x7F           # mask off the battery bit
                # Valid: 1-100; >100 = no humidity sensor (e.g. 106 = temp-only)
                hum = hum_masked if 1 <= hum_masked <= 100 else None
            else:
                battery_low = False
                hum_masked  = None
                hum         = None

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
                        self._notify_user(
                            "battery_replaced",
                            f"LaCrosse Sensor {old_id}: Batteriewechsel erkannt "
                            f"(neue Funk-ID {sensor_id})",
                            f"LaCrosse sensor {old_id}: battery replacement detected "
                            f"(new radio ID {sensor_id})",
                        )
                        self.hass.loop.call_soon_threadsafe(self._notify_listeners)
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

            # Which channels are new for this sensor?
            temp_channel = "temperature2" if is_probe2 else "temperature"
            new_discoveries: list[SensorDiscovery] = []
            was_known = resolved_id in self._discovered
            known = self._discovered.setdefault(resolved_id, set())

            if self.auto_add:
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
                    self.hass.loop.call_soon_threadsafe(
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
                    self._notify_user(
                        "battery_low",
                        f"LaCrosse Sensor {resolved_id}: Batterie schwach",
                        f"LaCrosse sensor {resolved_id}: battery low",
                    )
            elif not battery_low:
                self._battery_notified.discard(resolved_id)
            self._update((resolved_id, "battery"), battery_low)

        except (IndexError, ValueError) as exc:
            _LOGGER.error("Parse error '%s': %s", line, exc)

    def _update(self, key: tuple, value) -> None:
        if self._cache.get(key) != value:
            self._cache[key] = value
            self.sensor_states[key] = value
            self.hass.loop.call_soon_threadsafe(self._notify_listeners)

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
                    _, count = self._outlier_counter.get(key, (temperature, 0))
                    count += 1
                    self._outlier_counter[key] = (temperature, count)
                    if count >= self.outlier_confirm_count:
                        _LOGGER.info(
                            "[JeeLink] Outlier confirmed (%dx same value): "
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
                    _, count = self._outlier_counter.get(key, (humidity, 0))
                    count += 1
                    self._outlier_counter[key] = (humidity, count)
                    if count >= self.outlier_confirm_count:
                        _LOGGER.info(
                            "[JeeLink] Outlier confirmed (%dx same value): "
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
