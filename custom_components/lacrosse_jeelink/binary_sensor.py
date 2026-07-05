"""Battery binary sensors — dynamically discovered."""
from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import JeeLinkCoordinator, SensorDiscovery


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: JeeLinkCoordinator = hass.data[DOMAIN][entry.entry_id]

    # Static bridge sensors: serial connection + radio-silence watchdog
    async_add_entities(
        [
            JeeLinkConnectedSensor(coordinator, entry),
            JeeLinkRadioSilenceSensor(coordinator, entry),
        ]
    )

    @callback
    def _on_discovery(discoveries: list[SensorDiscovery]) -> None:
        entities = [
            LaCrosseBatterySensor(coordinator, entry, disc)
            for disc in discoveries
            if disc.channel == "battery"
        ]
        if entities:
            async_add_entities(entities)

    coordinator.register_discovery_callback(_on_discovery)


class JeeLinkConnectedSensor(BinarySensorEntity):
    """Connection state of the serial JeeLink link (bridge device)."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_translation_key = "connected"

    def __init__(self, coordinator: JeeLinkCoordinator, entry: ConfigEntry) -> None:
        self._coordinator = coordinator
        self._entry = entry
        self._remove_listener = None

    @property
    def unique_id(self) -> str:
        return f"{self._entry.entry_id}_connected"

    @property
    def device_info(self):
        return self._coordinator.device_info

    @property
    def is_on(self) -> bool:
        return self._coordinator.connected

    @property
    def extra_state_attributes(self) -> dict:
        return {"firmware": self._coordinator.firmware}

    async def async_added_to_hass(self) -> None:
        self._remove_listener = self._coordinator.async_add_listener(self._on_update)

    async def async_will_remove_from_hass(self) -> None:
        if self._remove_listener:
            self._remove_listener()

    @callback
    def _on_update(self) -> None:
        self.async_write_ha_state()


class JeeLinkRadioSilenceSensor(BinarySensorEntity):
    """Radio-silence watchdog as an entity (bridge device): on = no radio
    packet for longer than data_timeout minutes despite an open connection.
    Meant for automations (e.g. triggering a stick reset), independent of
    the notifications."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_translation_key = "radio_silence"

    def __init__(self, coordinator: JeeLinkCoordinator, entry: ConfigEntry) -> None:
        self._coordinator = coordinator
        self._entry = entry
        self._remove_listener = None

    @property
    def unique_id(self) -> str:
        return f"{self._entry.entry_id}_radio_silence"

    @property
    def device_info(self):
        return self._coordinator.device_info

    @property
    def is_on(self) -> bool:
        return self._coordinator.data_stale

    async def async_added_to_hass(self) -> None:
        self._remove_listener = self._coordinator.async_add_listener(self._on_update)

    async def async_will_remove_from_hass(self) -> None:
        if self._remove_listener:
            self._remove_listener()

    @callback
    def _on_update(self) -> None:
        self.async_write_ha_state()


class LaCrosseBatterySensor(BinarySensorEntity):
    """True (on) = battery low."""

    _attr_device_class = BinarySensorDeviceClass.BATTERY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_translation_key = "battery"

    def __init__(
        self,
        coordinator: JeeLinkCoordinator,
        entry: ConfigEntry,
        disc: SensorDiscovery,
    ) -> None:
        self._coordinator = coordinator
        self._disc = disc
        self._entry = entry
        self._state_key = (disc.sensor_id, "battery")
        self._remove_listener = None

    @property
    def unique_id(self) -> str:
        return f"{self._entry.entry_id}_{self._disc.sensor_id}_battery"

    @property
    def device_info(self):
        return self._coordinator.get_sensor_device_info(self._disc.sensor_id)

    @property
    def extra_state_attributes(self) -> dict:
        return {"sensor_id": self._disc.sensor_id}

    @property
    def is_on(self) -> bool | None:
        val = self._coordinator.sensor_states.get(self._state_key)
        return bool(val) if val is not None else None

    async def async_added_to_hass(self) -> None:
        self._remove_listener = self._coordinator.async_add_listener(self._on_update)

    async def async_will_remove_from_hass(self) -> None:
        if self._remove_listener:
            self._remove_listener()

    @callback
    def _on_update(self) -> None:
        self.async_write_ha_state()
