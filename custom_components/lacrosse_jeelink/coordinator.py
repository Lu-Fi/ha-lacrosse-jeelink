"""JeeLink serial coordinator - dynamic sensor discovery, no hardcoded sensor map.

Protocol reference: FHEM 36_LaCrosse.pm

OK 9 format byte layout (parts[2..6]):
  parts[2]  Sensor-ID
  parts[3]  Flags:
              bit 7 (0x80) = new battery inserted
              bits 4-6 (0x70) = sensor type
              bits 0-3 (0x0F) = channel  (1 = main, 2 = probe2/external)
  parts[4]  Temperature MSB  }  temp = (MSB*256 + LSB - 1000) / 10.0
  parts[5]  Temperature LSB  }
  parts[6]  Humidity byte:
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


# ── Taupunkt (Magnus-Formel, identisch zu FHEM LaCrosse_CalcDewpoint) ─────────

def _dewpoint(temp: float, hum: float) -> float:
    """Magnus-Formel. Gleiche Koeffizienten wie in 36_LaCrosse.pm."""
    a, b = (7.5, 237.3) if temp >= 0 else (7.6, 240.7)
    sdd = 6.1078 * 10 ** ((a * temp) / (b + temp))
    dd  = hum / 100 * sdd
    v   = math.log10(dd / 6.1078)
    return (b * v) / (a - v)


@dataclass
class SensorDiscovery:
    """Beschreibt einen neu entdeckten Sensor-Kanal."""
    sensor_id: int
    channel: str   # "temperature" | "temperature2" | "humidity" | "battery"


class JeeLinkCoordinator:
    """Verwaltet die serielle JeeLink-Verbindung und verteilt Sensor-Updates."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry

        def _opt(key: str, default):
            """Option mit Fallback auf entry.data (aeltere Installationen)."""
            return entry.options.get(key, entry.data.get(key, default))

        self.serial_port: str = _opt(CONF_SERIAL_PORT, entry.data[CONF_SERIAL_PORT])
        self.auto_add: bool = _opt(CONF_AUTO_ADD, True)
        self.outlier_confirm_count: int = int(
            _opt(CONF_OUTLIER_CONFIRM_COUNT, OUTLIER_CONFIRM_COUNT)
        )
        # Konfigurierbare Timeouts (Options-Flow)
        self.serial_timeout: float = float(_opt(CONF_SERIAL_TIMEOUT, DEFAULT_SERIAL_TIMEOUT))
        self.reconnect_delay: int = int(_opt(CONF_RECONNECT_DELAY, DEFAULT_RECONNECT_DELAY))
        self.battery_replace_timeout: int = int(
            _opt(CONF_BATTERY_REPLACE_TIMEOUT, DEFAULT_BATTERY_REPLACE_TIMEOUT)
        )
        self.debug_timeout: int = int(_opt(CONF_DEBUG_TIMEOUT, DEFAULT_DEBUG_TIMEOUT))
        # Benachrichtigungen (Options-Flow): beliebige notify-Entity,
        # leer = keine Meldungen. notify_enabled ist der Hauptschalter,
        # jeder Meldungstyp ist zusaetzlich einzeln schaltbar.
        self.notify_enabled: bool = bool(_opt(CONF_NOTIFY_ENABLED, True))
        self.notify_entity: str = _opt(CONF_NOTIFY_ENTITY, DEFAULT_NOTIFY_ENTITY)
        self.notify_types: dict[str, bool] = {
            "connection": bool(_opt(CONF_NOTIFY_CONNECTION, True)),
            "data_timeout": bool(_opt(CONF_NOTIFY_DATA_TIMEOUT, True)),
            "new_sensor": bool(_opt(CONF_NOTIFY_NEW_SENSOR, True)),
            "battery_low": bool(_opt(CONF_NOTIFY_BATTERY_LOW, True)),
            "battery_replaced": bool(_opt(CONF_NOTIFY_BATTERY_REPLACED, True)),
        }
        # Funkstille-Watchdog (Minuten, 0 = aus)
        self.data_timeout_min: int = int(_opt(CONF_DATA_TIMEOUT, DEFAULT_DATA_TIMEOUT))
        self.last_data_ts: float = 0.0
        self._data_timeout_notified = False
        self._unsub_watchdog = None
        # Automatisches Aufräumen verwaister Auto-Sensoren (Stunden, 0 = aus)
        self.stale_cleanup_hours: int = int(
            _opt(CONF_STALE_CLEANUP_HOURS, DEFAULT_STALE_CLEANUP_HOURS)
        )
        self._last_stale_check = 0.0

        self.debug: bool = False
        # Verbindungsstatus fuer connected-Binary-Sensor + Benachrichtigungen
        self.connected: bool = False
        self._conn_notified_down = False
        # Batterie-Status-Cache fuer "Batterie schwach"-Benachrichtigung
        self._battery_notified: set[int] = set()

        # sensor_id -> set of channels already created as entities
        self._discovered: dict[int, set[str]] = {}

        # (sensor_id, channel) -> current value
        self.sensor_states: dict[tuple[int, str], object] = {}

        # Cache fuer Ausreisser-Pruefung
        self._cache: dict[tuple[int, str], object] = {}

        # Ausreisser-Bestaetigung: key -> (wert, anzahl_aufeinanderfolgend)
        self._outlier_counter: dict[tuple, tuple] = {}

        self._state_listeners: list = []
        self._discovery_callbacks: list = []

        # Batteriewechsel: new_id -> old_id (Alias nach ID-Wechsel)
        self._id_aliases: dict[int, int] = {}
        # Batteriewechsel-Modus aktiv: old_id -> Ablauf-Timestamp
        self._replace_battery: dict[int, float] = {}

        self._entry_id = entry.entry_id
        # Persistenz der ID-Aliase (Batteriewechsel): Ohne Store waere die
        # Zuordnung "neue Funk-ID -> alter Sensor" nach einem HA-Neustart
        # verloren und die neue ID wuerde als NEUER Sensor angelegt.
        self._alias_store: Store = Store(
            hass, 1, f"{DOMAIN}_{entry.entry_id}_aliases"
        )
        self._reset_event = threading.Event()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._debug_cancel = None
        self._sensor_device_infos: dict[int, DeviceInfo] = {}

        # Bridge-Device (fuer Reset-Button und Debug-Switch)
        self.device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="LaCrosse JeeLink Bridge",
            manufacturer="Jeelabs",
            model="JeeLink v3",
        )

    # ── Benachrichtigungen ─────────────────────────────────────────────────────

    def _lang(self) -> str:
        """'en' bei englischer HA-Systemsprache, sonst 'de' (Fallback)."""
        lang = (self.hass.config.language or "de").lower()
        return "en" if lang.startswith("en") else "de"

    def _notify_user(self, category: str, message_de: str, message_en: str) -> None:
        """Sendet eine Meldung an die konfigurierte notify-Entity.

        Threadsicher (kann aus dem seriellen Reader-Thread aufgerufen
        werden). Ohne konfigurierte Entity, mit notify_enabled=False oder
        abgeschaltetem Meldungstyp (category) passiert nichts. Fehler beim
        Versand duerfen den Betrieb nie stoeren.
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
                    "Benachrichtigung über %s fehlgeschlagen: %s",
                    self.notify_entity, err,
                )

        self.hass.loop.call_soon_threadsafe(
            lambda: self.hass.async_create_task(_send())
        )

    # ── Batteriewechsel ────────────────────────────────────────────────────────

    def start_battery_replace(self, sensor_id: int, timeout: int | None = None) -> None:
        """Aktiviert Batteriewechsel-Modus fuer sensor_id fuer timeout Sekunden.

        Identisch zu FHEMs 'set <device> replaceBatteryForSec <sec>'.
        Der naechste unbekannte Sensor mit battery_new-Flag wird als Alias
        auf diese ID gemappt. Ohne timeout gilt die konfigurierte Option
        battery_replace_timeout (Standard 120 s).
        """
        if timeout is None:
            timeout = self.battery_replace_timeout
        self._replace_battery[sensor_id] = time.time() + timeout
        _LOGGER.info(
            "Batteriewechsel-Modus aktiv fuer Sensor %d (%ds) - "
            "bitte Batterie jetzt wechseln",
            sensor_id, timeout,
        )
        self._notify_listeners()
        # Nach Ablauf des Fensters die Entities aktualisieren, damit das
        # Button-Attribut replace_active nicht bis zum naechsten Paket
        # faelschlich auf "true" stehen bleibt.
        async_call_later(self.hass, timeout + 1, lambda _now: self._notify_listeners())

    def is_battery_replace_active(self, sensor_id: int) -> bool:
        """True wenn Batteriewechsel-Modus fuer diesen Sensor gerade aktiv ist."""
        expiry = self._replace_battery.get(sensor_id)
        return expiry is not None and time.time() < expiry

    @property
    def data_stale(self) -> bool:
        """True, solange der Funkstille-Watchdog ausgeloest ist (laenger als
        data_timeout Minuten kein Paket trotz Verbindung). Als binary_sensor
        exponiert, damit Automationen darauf reagieren koennen (z.B.
        Stick-Reset), statt auf Benachrichtigungen angewiesen zu sein."""
        return self._data_timeout_notified

    def _resolve_id(self, sensor_id: int) -> int:
        """Loest sensor_id ueber Alias-Tabelle auf (nach Batteriewechsel)."""
        return self._id_aliases.get(sensor_id, sensor_id)

    def _schedule_alias_save(self) -> None:
        """Persistiert die Alias-Tabelle (threadsicher aus dem Reader-Thread)."""
        data = {str(k): v for k, v in self._id_aliases.items()}

        def _save() -> None:
            self._alias_store.async_delay_save(lambda: data, 1.0)

        self.hass.loop.call_soon_threadsafe(_save)

    def get_sensor_device_info(self, sensor_id: int) -> DeviceInfo:
        """DeviceInfo fuer einen einzelnen LaCrosse-Sensor, verknuepft mit der Bridge."""
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
        # Persistierte ID-Aliase (Batteriewechsel) laden, BEVOR Pakete
        # verarbeitet werden - sonst wuerde eine bereits gemappte neue
        # Funk-ID als neuer Sensor angelegt.
        stored = await self._alias_store.async_load()
        if stored:
            self._id_aliases = {int(k): int(v) for k, v in stored.items()}
            _LOGGER.debug("Geladene ID-Aliase: %s", self._id_aliases)
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._serial_loop, name="jeelink-serial", daemon=True
        )
        self._thread.start()

        # Minuetlicher Wartungs-Tick: Funkstille-Watchdog (data_timeout
        # Minuten, 0 = aus) und Aufräumen verwaister Auto-Sensoren
        # (stale_cleanup_hours, 0 = aus, geprueft alle 15 Minuten).
        if self.data_timeout_min > 0 or self.stale_cleanup_hours > 0:
            self._unsub_watchdog = async_track_time_interval(
                self.hass, self._async_maintenance, datetime.timedelta(seconds=60)
            )

    def preload_from_registry(self) -> list[SensorDiscovery]:
        """Bekannte Sensoren aus der Entity-Registry rekonstruieren.

        Wird nach dem Plattform-Setup aufgerufen (siehe __init__.py), damit
        ALLE Entities eines bekannten Sensors sofort wieder existieren -
        auch wenn der Sensor (z.B. wegen leerer Batterie) nie wieder ein
        Paket sendet. Ohne dies blieben insbesondere der "Batterie
        gewechselt"-Button und der Batterie-Sensor eines toten Sensors nach
        einem Neustart dauerhaft nicht verfuegbar - der Button wird aber
        genau dann gebraucht.
        """
        registry = er.async_get(self.hass)
        known_entries = er.async_entries_for_config_entry(registry, self._entry_id)
        prefix = self._entry_id + "_"
        # unique_id-Suffix -> Discovery-Kanal. dewpoint wird bewusst
        # ausgelassen: die Entity entsteht zusammen mit "humidity".
        # "_battery_replace" (Button) muss vor "_battery" geprueft werden.
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
        """Minuetlicher Wartungs-Tick: Funkstille-Watchdog + Stale-Cleanup."""
        if self.data_timeout_min > 0:
            self._check_data_timeout()
        if self.stale_cleanup_hours > 0 and (
            time.time() - self._last_stale_check > 15 * 60
        ):
            self._last_stale_check = time.time()
            self._cleanup_stale_sensors()

    def _check_data_timeout(self) -> None:
        """Warnt, wenn laenger als data_timeout Minuten kein Funkpaket mehr
        geparst wurde (Entwarnung kommt aus _parse_line, sobald wieder
        Daten eintreffen)."""
        if not self.connected or not self.last_data_ts:
            # Ohne Verbindung greift bereits die Verbindungs-Meldung;
            # ohne jemals empfangene Daten keine Basis fuer einen Vergleich.
            return
        silence = time.time() - self.last_data_ts
        if silence > self.data_timeout_min * 60 and not self._data_timeout_notified:
            self._data_timeout_notified = True
            self._notify_listeners()  # binary_sensor "Funkstille" aktualisieren
            minutes = int(silence // 60)
            _LOGGER.warning(
                "JeeLink: seit %d Minuten keine Funkpakete mehr empfangen "
                "(Verbindung besteht)", minutes,
            )
            self._notify_user(
                "data_timeout",
                f"JeeLink: seit {minutes} Minuten keine Funkdaten mehr "
                "empfangen (Verbindung zum Stick besteht)",
                f"JeeLink: no radio data received for {minutes} minutes "
                "(serial connection is up)",
            )

    def _last_seen_epoch(self, sensor_id: int) -> float | None:
        """Zeitpunkt des letzten Pakets als Epoch-Sekunden. Der Wert kann je
        nach Quelle Epoch (live), datetime oder ISO-String (Restore) sein."""
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
        """Entfernt automatisch angelegte Sensoren, die laenger als
        stale_cleanup_hours keine Daten mehr gesendet haben.

        Schutzmechanismen - entfernt wird NUR ein Sensor, den der Nutzer
        erkennbar nie angefasst hat:
        - Geraet umbenannt (name_by_user), einem Raum zugeordnet (area_id)
          oder mit Labels versehen -> geschuetzt.
        - Irgendeine Entity des Sensors umbenannt, einem Raum zugeordnet,
          mit Labels/Aliassen versehen oder von der automatischen Vergabe
          abweichend konfiguriert -> geschuetzt.
        Ein derart "adoptierter" Sensor bleibt auch mit leerer Batterie
        erhalten (inkl. Batteriewechsel-Button!).
        - Ohne bekannten "Zuletzt empfangen"-Zeitstempel wird nichts
          entfernt (keine Basis fuer die Entscheidung).
        """
        cutoff = time.time() - self.stale_cleanup_hours * 3600
        ent_reg = er.async_get(self.hass)
        dev_reg = dr.async_get(self.hass)
        all_entries = er.async_entries_for_config_entry(ent_reg, self._entry_id)

        for sensor_id in list(self._discovered):
            last = self._last_seen_epoch(sensor_id)
            if last is None or last >= cutoff:
                continue
            device = dev_reg.async_get_device(
                identifiers={(DOMAIN, f"{self._entry_id}_{sensor_id}")}
            )
            if device and (device.name_by_user or device.area_id or device.labels):
                continue  # vom Nutzer angefasst -> niemals automatisch loeschen

            uid_prefix = f"{self._entry_id}_{sensor_id}_"
            sensor_entities = [
                reg_entry for reg_entry in all_entries
                if reg_entry.unique_id.startswith(uid_prefix)
            ]
            if any(
                reg_entry.name or reg_entry.area_id or reg_entry.labels
                or reg_entry.aliases
                for reg_entry in sensor_entities
            ):
                continue  # mindestens eine Entity individualisiert -> behalten

            for reg_entry in sensor_entities:
                ent_reg.async_remove(reg_entry.entity_id)
            if device:
                dev_reg.async_remove_device(device.id)

            # Internen Zustand mit ausraeumen
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
                "LaCrosse Sensor %d automatisch entfernt: keine Daten seit "
                "mehr als %d h und nicht umbenannt (stale_cleanup_hours)",
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

    # ── Discovery-Callbacks ────────────────────────────────────────────────────

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
        _LOGGER.info("JeeLink: DTR-Reset angefordert")
        self._reset_event.set()

    # ── Debug-Modus ────────────────────────────────────────────────────────────

    def enable_debug(self) -> None:
        self.debug = True
        _LOGGER.info(
            "[DEBUG] Debug-Modus aktiviert - automatisch aus in %ds", self.debug_timeout
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
        _LOGGER.info("[DEBUG] Debug-Modus deaktiviert")
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
            "[DEBUG] Debug-Modus automatisch nach %ds beendet", self.debug_timeout
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

    # ── Serieller Reader ───────────────────────────────────────────────────────

    def _dtr_reset(self, ser: serial.Serial) -> None:
        try:
            ser.setDTR(False)
            time.sleep(0.25)
            ser.setDTR(True)
            time.sleep(2.0)
            _LOGGER.info("JeeLink Hardware-Reset (DTR) durchgefuehrt")
        except Exception as exc:
            _LOGGER.warning("DTR-Reset fehlgeschlagen: %s", exc)

    def _set_connected(self, connected: bool, reason: str = "") -> None:
        """Verbindungsstatus pflegen + Benachrichtigung bei Wechsel."""
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
                # stty ist ein Linux-spezifisches Sicherheitsnetz (raw-Modus,
                # kein Echo). Best-effort: auf Systemen ohne stty oder mit
                # anderen Gerätepfaden setzt pyserial die Parameter selbst.
                try:
                    subprocess.run(
                        ["stty", "-F", self.serial_port, "57600", "raw",
                         "-echo", "-echoe", "-echok"],
                        check=True, capture_output=True
                    )
                except (OSError, subprocess.CalledProcessError) as exc:
                    _LOGGER.debug("stty übersprungen (%s)", exc)
                _LOGGER.info("Verbinde mit %s...", self.serial_port)
                ser = serial.Serial(self.serial_port, 57600, timeout=self.serial_timeout)
                self._dtr_reset(ser)
                ser.write(b"7m\r10t\r")
                _LOGGER.info("JeeLink initialisiert, lese Daten...")
                self._set_connected(True)
                # Baseline fuer den Funkstille-Watchdog: ab JETZT zaehlen,
                # nicht ab dem letzten Paket vor einem Verbindungsabbruch.
                self.last_data_ts = time.time()
                self._reset_event.clear()

                while not self._stop_event.is_set():
                    if self._reset_event.is_set():
                        _LOGGER.info("Reset-Signal empfangen - schliesse Serial fuer Reconnect")
                        self._reset_event.clear()
                        break

                    line = ser.readline().decode("utf-8", errors="ignore").strip()
                    if not line:
                        continue

                    if "drecvintr exit" in line:
                        _LOGGER.warning("Firmware: drecvintr exit - DTR-Reset")
                        self._dtr_reset(ser)
                        ser.write(b"7m\r10t\r")
                        continue

                    if "RFM12 hang" in line:
                        _LOGGER.warning("Firmware: RFM12 hang - DTR-Reset")
                        self._dtr_reset(ser)
                        ser.write(b"7m\r10t\r")
                        continue

                    if line.startswith("OK 9"):
                        self._parse_line(line)

            except Exception as exc:
                _LOGGER.error(
                    "Serieller Fehler: %s - reconnect in %ds", exc, self.reconnect_delay
                )
                self._set_connected(False, str(exc))
                try:
                    if ser:
                        ser.close()
                except Exception:
                    pass
                # In kurzen Schritten warten, damit ein Unload nicht blockiert
                deadline = time.time() + self.reconnect_delay
                while time.time() < deadline and not self._stop_event.is_set():
                    time.sleep(0.5)

    # ── Protokoll-Parsing (gemaess 36_LaCrosse.pm) ────────────────────────────

    def _parse_line(self, line: str) -> None:
        """Parse eines OK-9-Telegramms.

        Byte-Layout (identisch zu FHEM 36_LaCrosse.pm Zeilen 213-220):
          parts[2]  Sensor-ID
          parts[3]  Flags: bit7=new_battery, bits4-6=type, bits0-3=channel
          parts[4]  Temp MSB
          parts[5]  Temp LSB
          parts[6]  Hum-Byte: bit7=battery_low, bits0-6=humidity (1-100 gueltig)
        """
        try:
            parts = line.split()
            if len(parts) < 6:
                return

            # Funkstille-Watchdog fuettern + ggf. Entwarnung senden
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

            # Temperatur
            temperature = round((t_msb * 256 + t_lsb - 1000) / 10.0, 1)

            # Flags-Byte (parts[3])
            battery_new = bool(flags & 0x80)          # Bit 7: neue Batterie eingelegt
            channel     = flags & 0x0F                 # Bits 0-3: Kanal (1=Haupt, 2=Probe2)
            is_probe2   = (channel == 2)

            # Humidity-Byte (parts[6])
            # Bit 7 = schwache Batterie (battery_low), Bits 0-6 = Feuchte
            if hum_raw is not None:
                battery_low = bool(hum_raw & 0x80)
                hum_masked  = hum_raw & 0x7F           # Batterie-Bit abmaskieren
                # Gueltig: 1-100; >100 = kein Feuchtesensor (z.B. 106 = temp-only)
                hum = hum_masked if 1 <= hum_masked <= 100 else None
            else:
                battery_low = False
                hum_masked  = None
                hum         = None

            # ── Batteriewechsel-Erkennung (wie FHEM replaceBatteryForSec) ──────
            # Unbekannte ID mit battery_new -> pruefen ob ein Sensor im Replace-Modus ist
            is_unknown = (sensor_id not in self._discovered
                          and sensor_id not in self._id_aliases)
            if battery_new and is_unknown:
                now = time.time()
                for old_id, expiry in list(self._replace_battery.items()):
                    if now < expiry:
                        _LOGGER.info(
                            "Batteriewechsel erkannt: Sensor %d hat neue ID %d - "
                            "Alias gespeichert, bestehende Entities bleiben erhalten",
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

            # ID durch Alias aufloesen (liefert old_id falls nach Batteriewechsel)
            resolved_id = self._resolve_id(sensor_id)

            self._debug_log(
                f"raw={line} | id={sensor_id} resolved={resolved_id} ch={channel} "
                f"T={temperature} degC hum_raw={hum_raw} "
                f"hum_masked={hum_masked} hum={hum}% "
                f"bat_low={battery_low} bat_new={battery_new} probe2={is_probe2}"
            )

            if battery_new and resolved_id == sensor_id:
                _LOGGER.info(
                    "JeeLink sensor %d: neue Batterie eingelegt "
                    "(kein Batteriewechsel-Modus aktiv)",
                    sensor_id,
                )

            # Welche Kanaele sind fuer diesen Sensor neu?
            temp_channel = "temperature2" if is_probe2 else "temperature"
            new_discoveries: list[SensorDiscovery] = []
            was_known = resolved_id in self._discovered
            known = self._discovered.setdefault(resolved_id, set())

            if self.auto_add:
                if temp_channel not in known:
                    known.add(temp_channel)
                    new_discoveries.append(SensorDiscovery(resolved_id, temp_channel))
                # Humidity-Entity nur wenn echter Messwert empfangen (1-100%)
                if hum is not None and "humidity" not in known:
                    known.add("humidity")
                    new_discoveries.append(SensorDiscovery(resolved_id, "humidity"))
                if "battery" not in known:
                    known.add("battery")
                    new_discoveries.append(SensorDiscovery(resolved_id, "battery"))
                # "Zuletzt empfangen"-Zeitstempel pro Sensor
                if "last_seen" not in known:
                    known.add("last_seen")
                    new_discoveries.append(SensorDiscovery(resolved_id, "last_seen"))
                # Batteriewechsel-Button einmalig pro Sensor anlegen
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

            # Temperatur-Update
            temp_key = (resolved_id, temp_channel)
            temp_ok = self._check_temperature(temp_key, temperature, line)
            self._debug_log(
                f"sensor {resolved_id} {temp_channel}: {temperature} degC "
                f"-> {'OK' if temp_ok else 'GEFILTERT'}"
            )
            if temp_ok:
                self._update(temp_key, temperature)

            # Humidity-Update (nur echte Werte 1-100%)
            if hum is not None:
                hum_key = (resolved_id, "humidity")
                hum_ok = self._check_humidity(hum_key, hum, line)
                self._debug_log(
                    f"sensor {resolved_id} humidity: {hum}% "
                    f"-> {'OK' if hum_ok else 'GEFILTERT'}"
                )
                if hum_ok:
                    self._update(hum_key, hum)

                    # Taupunkt berechnen (wie FHEM doDewpoint)
                    if temp_ok:
                        try:
                            dp = round(_dewpoint(temperature, hum), 1)
                            self._update((resolved_id, "dewpoint"), dp)
                        except (ValueError, ZeroDivisionError):
                            pass

            # "Zuletzt empfangen": jedes geparste Paket zaehlt, auch wenn der
            # Messwert vom Ausreisser-Filter verworfen wurde - empfangen ist
            # empfangen. Auf volle Minuten quantisiert, damit nicht jedes
            # 4-Sekunden-Paket einen neuen State (Datenbank-Eintrag) erzeugt.
            self._update((resolved_id, "last_seen"), int(time.time() // 60 * 60))

            # Batterie-Update (+ einmalige Meldung beim Wechsel auf "schwach")
            if battery_low and resolved_id not in self._battery_notified:
                self._battery_notified.add(resolved_id)
                # Nur melden, wenn der Sensor schon bekannt war (kein Spam
                # direkt bei der Erst-Discovery eines leeren Sensors).
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
            _LOGGER.error("Parse-Fehler '%s': %s", line, exc)

    def _update(self, key: tuple, value) -> None:
        if self._cache.get(key) != value:
            self._cache[key] = value
            self.sensor_states[key] = value
            self.hass.loop.call_soon_threadsafe(self._notify_listeners)

    def _check_temperature(self, key: tuple, temperature: float, raw: str) -> bool:
        if not (DEFAULT_TEMP_MIN <= temperature <= DEFAULT_TEMP_MAX):
            _LOGGER.warning(
                "[JeeLink] Temp-Ausreisser absolut: sensor=%s T=%s degC "
                "(erlaubt %s..%s) | raw: %s",
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
                            "[JeeLink] Ausreisser bestaetigt (%dx gleicher Wert): "
                            "sensor=%s %s->%s degC | raw: %s",
                            count, key[0], last, temperature, raw,
                        )
                        self._outlier_counter.pop(key, None)
                        return True
                    _LOGGER.warning(
                        "[JeeLink] Temp-Ausreisser delta: sensor=%s %s->%s degC "
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
        # Absolut-Pruefung: 1-100% (FHEM: $humidity && $humidity <= 100)
        if not (1 <= humidity <= 100):
            _LOGGER.warning(
                "[JeeLink] Feuchte-Ausreisser absolut: sensor=%s %d%% | raw: %s",
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
                            "[JeeLink] Ausreisser bestaetigt (%dx gleicher Wert): "
                            "sensor=%s %s->%d%% | raw: %s",
                            count, key[0], last, humidity, raw,
                        )
                        self._outlier_counter.pop(key, None)
                        return True
                    _LOGGER.warning(
                        "[JeeLink] Feuchte-Ausreisser delta: sensor=%s %s->%d%% "
                        "(d%.1f > %s, %d/%d) | raw: %s",
                        key[0], last, humidity, delta, DEFAULT_HUM_MAX_DELTA,
                        count, self.outlier_confirm_count, raw,
                    )
                    return False
            except (TypeError, ValueError):
                pass
        self._outlier_counter.pop(key, None)
        return True
