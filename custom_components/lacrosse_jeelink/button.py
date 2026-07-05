"""Buttons: JeeLink DTR reset (bridge) + battery replacement (per sensor)."""
from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import JeeLinkCoordinator, SensorDiscovery


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: JeeLinkCoordinator = hass.data[DOMAIN][entry.entry_id]

    # Bridge reset button (static, always present)
    async_add_entities([JeeLinkResetButton(coordinator, entry)])

    # Battery-replaced button (dynamic, per discovered sensor)
    @callback
    def _on_discovery(discoveries: list[SensorDiscovery]) -> None:
        entities = [
            LaCrosseBatteryReplaceButton(coordinator, entry, disc.sensor_id)
            for disc in discoveries
            if disc.channel == "replace_battery"
        ]
        if entities:
            async_add_entities(entities)

    coordinator.register_discovery_callback(_on_discovery)


class JeeLinkResetButton(ButtonEntity):
    """DTR hardware reset of the JeeLink USB stick."""

    _attr_has_entity_name = True
    _attr_translation_key = "reset"
    _attr_icon = "mdi:restart"

    def __init__(self, coordinator: JeeLinkCoordinator, entry: ConfigEntry) -> None:
        self._coordinator = coordinator
        self._entry = entry

    @property
    def unique_id(self) -> str:
        return f"{self._entry.entry_id}_reset"

    @property
    def device_info(self):
        return self._coordinator.device_info

    async def async_press(self) -> None:
        await self.hass.async_add_executor_job(self._coordinator.request_reset)


class LaCrosseBatteryReplaceButton(ButtonEntity):
    """Arms the battery replacement mode for a sensor (like FHEM's
    replaceBatteryForSec).

    After pressing, the configured window (default 120 s) is open to swap
    the battery. The sensor picks a new random radio ID - the integration
    detects it automatically and maps it onto the existing sensor.
    """

    _attr_icon = "mdi:battery-sync"
    _attr_has_entity_name = True
    _attr_translation_key = "battery_replace"
    _attr_available = True  # always pressable, even if sensor entities are unavailable

    def __init__(
        self,
        coordinator: JeeLinkCoordinator,
        entry: ConfigEntry,
        sensor_id: int,
    ) -> None:
        self._coordinator = coordinator
        self._entry = entry
        self._sensor_id = sensor_id
        self._remove_listener = None

    @property
    def unique_id(self) -> str:
        return f"{self._entry.entry_id}_{self._sensor_id}_battery_replace"

    @property
    def device_info(self):
        return self._coordinator.get_sensor_device_info(self._sensor_id)

    @property
    def extra_state_attributes(self) -> dict:
        """Shows whether the replace mode is currently armed."""
        return {
            "replace_active": self._coordinator.is_battery_replace_active(self._sensor_id)
        }

    async def async_added_to_hass(self) -> None:
        # Listener to update the replace_active state in real time
        self._remove_listener = self._coordinator.async_add_listener(self._on_update)

    async def async_will_remove_from_hass(self) -> None:
        if self._remove_listener:
            self._remove_listener()

    @callback
    def _on_update(self) -> None:
        self.async_write_ha_state()

    async def async_press(self) -> None:
        """Arm the battery replacement mode (configured window)."""
        self._coordinator.start_battery_replace(self._sensor_id)
