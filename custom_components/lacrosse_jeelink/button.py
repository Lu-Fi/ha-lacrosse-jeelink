"""Buttons: JeeLink DTR-Reset (Bridge) + Batteriewechsel (pro Sensor)."""
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

    # Bridge-Reset-Button (statisch, immer vorhanden)
    async_add_entities([JeeLinkResetButton(coordinator, entry)])

    # Batteriewechsel-Button (dynamisch, je entdecktem Sensor)
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
    """DTR Hardware-Reset des JeeLink USB-Sticks."""

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
    """Startet den Batteriewechsel-Modus fuer einen Sensor (wie FHEM replaceBatteryForSec).

    Nach dem Druck hat man 120 Sekunden Zeit, die Batterie zu wechseln.
    Der Sensor bekommt dabei eine neue zufaellige ID - die Integration
    erkennt sie automatisch und mappt sie auf den bestehenden Sensor.
    """

    _attr_icon = "mdi:battery-sync"
    _attr_has_entity_name = True
    _attr_translation_key = "battery_replace"
    _attr_available = True  # Immer pressbar, auch wenn Sensor-Entities unavailable sind

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
        """Zeigt ob der Replace-Modus gerade aktiv ist."""
        return {
            "replace_active": self._coordinator.is_battery_replace_active(self._sensor_id)
        }

    async def async_added_to_hass(self) -> None:
        # Listener um den replace_active-Status in Echtzeit zu aktualisieren
        self._remove_listener = self._coordinator.async_add_listener(self._on_update)

    async def async_will_remove_from_hass(self) -> None:
        if self._remove_listener:
            self._remove_listener()

    @callback
    def _on_update(self) -> None:
        self.async_write_ha_state()

    async def async_press(self) -> None:
        """Aktiviert den Batteriewechsel-Modus fuer 120 Sekunden."""
        self._coordinator.start_battery_replace(self._sensor_id)
