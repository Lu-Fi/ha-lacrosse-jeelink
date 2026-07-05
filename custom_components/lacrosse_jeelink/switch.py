"""Debug-mode switch for LaCrosse JeeLink."""
from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import JeeLinkCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: JeeLinkCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([JeeLinkDebugSwitch(coordinator, entry)])


class JeeLinkDebugSwitch(SwitchEntity):
    """Toggle verbose serial logging. Auto-off after the configured timeout."""

    _attr_has_entity_name = True
    _attr_translation_key = "debug"
    _attr_icon = "mdi:bug"

    def __init__(self, coordinator: JeeLinkCoordinator, entry: ConfigEntry) -> None:
        self._coordinator = coordinator
        self._entry = entry
        self._remove_listener = None

    @property
    def unique_id(self) -> str:
        return f"{self._entry.entry_id}_debug"

    @property
    def device_info(self):
        return self._coordinator.device_info

    @property
    def is_on(self) -> bool:
        return self._coordinator.debug

    async def async_added_to_hass(self) -> None:
        self._remove_listener = self._coordinator.async_add_listener(self._on_update)

    async def async_will_remove_from_hass(self) -> None:
        if self._remove_listener:
            self._remove_listener()

    @callback
    def _on_update(self) -> None:
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs) -> None:
        self._coordinator.enable_debug()

    async def async_turn_off(self, **kwargs) -> None:
        self._coordinator.disable_debug()
