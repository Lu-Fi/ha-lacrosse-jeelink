"""LaCrosse JeeLink Bridge integration."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .coordinator import JeeLinkCoordinator

PLATFORMS = ["sensor", "binary_sensor", "button", "switch"]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coordinator = JeeLinkCoordinator(hass, entry)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Reconstruct known sensors from the entity registry AFTER all
    # platforms have registered their discovery callbacks. This way all
    # entities of known sensors exist again immediately - in particular the
    # "battery replaced" button of a sensor that will never send another
    # packet because its battery is empty.
    preloaded = coordinator.preload_from_registry()
    if preloaded:
        coordinator._fire_discoveries(preloaded)

    # Start the serial reader only NOW - after platform setup and
    # preload. Started earlier, discoveries from packets arriving in the
    # window before callback registration would fizzle out: the channels
    # would count as "known", the preload would skip them, and the
    # sensor's entities would stay unavailable until the next restart.
    await coordinator.async_start()

    # Apply options changes (timeouts, notify, port, ...) immediately by
    # reloading the entry - no HA restart required.
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coordinator: JeeLinkCoordinator = hass.data[DOMAIN][entry.entry_id]
    await coordinator.async_stop()
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unloaded


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: ConfigEntry, device_entry
) -> bool:
    """Allow manual removal of a device via Settings -> Devices (or the API).

    All per-sensor devices (LaCrosse, EMT7110, LevelSender) are dynamically
    discovered from radio packets - none of them are tied to fixed config
    entry data, so deleting one is always safe: the coordinator simply
    rediscovers the sensor (as a fresh device) if it ever sends another
    packet. Without this hook Home Assistant refuses any device deletion
    for this integration with "Config entry does not support device
    removal", even for empty/orphaned device entries left behind after
    their entities were removed.
    """
    return True
