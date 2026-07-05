"""Temperature, humidity, dewpoint and last-seen sensors — dynamically discovered."""
from __future__ import annotations

import datetime

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import JeeLinkCoordinator, SensorDiscovery


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: JeeLinkCoordinator = hass.data[DOMAIN][entry.entry_id]

    @callback
    def _on_discovery(discoveries: list[SensorDiscovery]) -> None:
        entities = []
        for disc in discoveries:
            if disc.channel in ("temperature", "temperature2"):
                entities.append(LaCrosseTempSensor(coordinator, entry, disc.sensor_id, disc.channel))
            elif disc.channel == "humidity":
                entities.append(LaCrosseHumSensor(coordinator, entry, disc.sensor_id))
                # Taupunkt: berechnet wie FHEM doDewpoint (Magnus-Formel)
                entities.append(LaCrosseDewpointSensor(coordinator, entry, disc.sensor_id))
            elif disc.channel == "last_seen":
                entities.append(LaCrosseLastSeenSensor(coordinator, entry, disc.sensor_id))
        if entities:
            async_add_entities(entities)

    coordinator.register_discovery_callback(_on_discovery)

    # Hinweis: Das Vorladen bekannter Sensoren aus der Entity-Registry
    # passiert zentral in __init__.py (coordinator.preload_from_registry())
    # fuer ALLE Plattformen - inklusive Batterie-Sensor und Batteriewechsel-
    # Button, die frueher nach einem Neustart bis zum ersten Paket fehlten.
    # RestoreSensor laedt die letzten Werte weiterhin in async_added_to_hass.


class _LaCrosseBase(RestoreSensor):
    """Gemeinsame Basis fuer alle LaCrosse-Sensor-Entities."""

    _attr_should_poll = False
    _attr_has_entity_name = True  # Name = Gerätename + Entity-Name (z.B. "Terrasse Temperatur")

    def __init__(
        self,
        coordinator: JeeLinkCoordinator,
        entry: ConfigEntry,
        sensor_id: int,
        channel: str,
    ) -> None:
        self._coordinator = coordinator
        self._entry = entry
        self._sensor_id = sensor_id
        self._state_key = (sensor_id, channel)
        self._remove_listener = None

    @property
    def device_info(self):
        return self._coordinator.get_sensor_device_info(self._sensor_id)

    @property
    def native_value(self):
        return self._coordinator.sensor_states.get(self._state_key)

    @property
    def extra_state_attributes(self) -> dict:
        return {"sensor_id": self._sensor_id}

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # Letzten Wert aus der DB wiederherstellen, falls noch kein Live-Wert vorliegt
        if self._state_key not in self._coordinator.sensor_states:
            last_data = await self.async_get_last_sensor_data()
            if last_data is not None and last_data.native_value is not None:
                self._coordinator.sensor_states[self._state_key] = last_data.native_value
                self._coordinator._cache[self._state_key] = last_data.native_value
        self._remove_listener = self._coordinator.async_add_listener(self._on_update)
        self.async_write_ha_state()

    async def async_will_remove_from_hass(self) -> None:
        if self._remove_listener:
            self._remove_listener()

    @callback
    def _on_update(self) -> None:
        self.async_write_ha_state()


class LaCrosseTempSensor(_LaCrosseBase):
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "°C"

    def __init__(self, coordinator, entry, sensor_id: int, channel: str):
        super().__init__(coordinator, entry, sensor_id, channel)
        self._channel = channel
        self._attr_translation_key = channel  # "temperature" oder "temperature2"

    @property
    def unique_id(self) -> str:
        return f"{self._entry.entry_id}_{self._sensor_id}_{self._channel}"


class LaCrosseHumSensor(_LaCrosseBase):
    _attr_device_class = SensorDeviceClass.HUMIDITY
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "%"
    _attr_translation_key = "humidity"

    def __init__(self, coordinator, entry, sensor_id: int):
        super().__init__(coordinator, entry, sensor_id, "humidity")

    @property
    def unique_id(self) -> str:
        return f"{self._entry.entry_id}_{self._sensor_id}_humidity"


class LaCrosseLastSeenSensor(_LaCrosseBase):
    """Zeitpunkt des zuletzt empfangenen Funkpakets dieses Sensors.

    Zaehlt jedes geparste Paket - auch wenn der Messwert vom Ausreisser-
    Filter verworfen wurde. Minutengenau (bewusst quantisiert, um die
    Datenbank nicht mit jedem 4-Sekunden-Paket zu fuellen). Praktisch, um
    tote Sensoren (leere Batterie, Funkloch) per Automation zu erkennen.
    """

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "last_seen"

    def __init__(self, coordinator, entry, sensor_id: int):
        super().__init__(coordinator, entry, sensor_id, "last_seen")

    @property
    def unique_id(self) -> str:
        return f"{self._entry.entry_id}_{self._sensor_id}_last_seen"

    @property
    def native_value(self):
        val = self._coordinator.sensor_states.get(self._state_key)
        if val is None:
            return None
        # Live-Wert: Epoch-Sekunden (aus dem Reader-Thread). Nach Restore
        # kann auch ein datetime (oder ISO-String) im Cache liegen.
        if isinstance(val, (int, float)):
            return dt_util.utc_from_timestamp(val)
        if isinstance(val, datetime.datetime):
            return val
        if isinstance(val, str):
            return dt_util.parse_datetime(val)
        return None


class LaCrosseDewpointSensor(_LaCrosseBase):
    """Taupunkt-Berechnung - identisch zur FHEM-Funktion LaCrosse_CalcDewpoint."""

    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "°C"
    _attr_icon = "mdi:water-thermometer"
    _attr_translation_key = "dewpoint"

    def __init__(self, coordinator, entry, sensor_id: int):
        super().__init__(coordinator, entry, sensor_id, "dewpoint")

    @property
    def unique_id(self) -> str:
        return f"{self._entry.entry_id}_{self._sensor_id}_dewpoint"
