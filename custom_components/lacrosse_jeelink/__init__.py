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

    # Bekannte Sensoren aus der Entity-Registry rekonstruieren, NACHDEM alle
    # Plattformen ihre Discovery-Callbacks registriert haben. So existieren
    # saemtliche Entities bekannter Sensoren sofort wieder - insbesondere der
    # "Batterie gewechselt"-Button eines Sensors, der wegen leerer Batterie
    # nie wieder von selbst ein Paket senden wird.
    preloaded = coordinator.preload_from_registry()
    if preloaded:
        coordinator._fire_discoveries(preloaded)

    # Den seriellen Reader erst JETZT starten - nach Plattform-Setup und
    # Preload. Startet er frueher, verpuffen Discoveries von Paketen, die
    # im Fenster vor der Callback-Registrierung eintreffen: die Kanaele
    # gelten dann als "bekannt", der Preload ueberspringt sie, und die
    # Entities des Sensors bleiben bis zum naechsten Neustart unavailable.
    await coordinator.async_start()

    # Options-Aenderungen (Timeouts, Notify, Port, ...) sofort anwenden,
    # indem der Eintrag neu geladen wird - kein HA-Neustart noetig.
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
