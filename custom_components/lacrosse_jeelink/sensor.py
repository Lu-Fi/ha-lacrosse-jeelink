"""Temperature, humidity, dew point and last-seen sensors - dynamically discovered."""
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
                # Dew point: calculated like FHEM doDewpoint (Magnus formula)
                entities.append(LaCrosseDewpointSensor(coordinator, entry, disc.sensor_id))
            elif disc.channel == "last_seen":
                entities.append(LaCrosseLastSeenSensor(coordinator, entry, disc.sensor_id))
        if entities:
            async_add_entities(entities)

    coordinator.register_discovery_callback(_on_discovery)

    # Note: preloading known sensors from the entity registry happens
    # centrally in __init__.py (coordinator.preload_from_registry()) for
    # ALL platforms - including the battery sensor and battery-replaced
    # button, which previously were missing after a restart until the
    # first packet. RestoreSensor still loads the last values in
    # async_added_to_hass.


class _LaCrosseBase(RestoreSensor):
    """Common base for all LaCrosse sensor entities."""

    _attr_should_poll = False
    _attr_has_entity_name = True  # name = device name + entity name (e.g. "Terrasse Temperature")

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
        # Restore the last value from the DB if no live value exists yet
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
    """Timestamp of the last received radio packet of this sensor.

    Counts every parsed packet - even if the reading was rejected by the
    outlier filter. Minute resolution (deliberately quantised so the
    database is not flooded by every 4-second packet). Handy for spotting
    dead sensors (empty battery, radio dead spot) in automations.
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
        # Live value: epoch seconds (from the reader thread). After a
        # restore the cache may also hold a datetime (or an ISO string).
        if isinstance(val, (int, float)):
            return dt_util.utc_from_timestamp(val)
        if isinstance(val, datetime.datetime):
            return val
        if isinstance(val, str):
            return dt_util.parse_datetime(val)
        return None


class LaCrosseDewpointSensor(_LaCrosseBase):
    """Dew point calculation - identical to FHEM's LaCrosse_CalcDewpoint."""

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
