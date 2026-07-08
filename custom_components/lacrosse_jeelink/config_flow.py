"""Config flow for LaCrosse JeeLink Bridge."""
from __future__ import annotations

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .const import (
    CONF_AUTO_ADD,
    CONF_BATTERY_REPLACE_TIMEOUT,
    CONF_DATA_TIMEOUT,
    CONF_DEBUG_TIMEOUT,
    CONF_DISCOVERY_MIN_PACKETS,
    CONF_DISCOVERY_WINDOW_SEC,
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
    CONF_SERIAL_PORT,
    CONF_SERIAL_TIMEOUT,
    CONF_STALE_CLEANUP_HOURS,
    DEFAULT_BATTERY_REPLACE_TIMEOUT,
    DEFAULT_DATA_TIMEOUT,
    DEFAULT_DEBUG_TIMEOUT,
    DEFAULT_DISCOVERY_MIN_PACKETS,
    DEFAULT_DISCOVERY_WINDOW_SEC,
    DEFAULT_INIT_COMMANDS,
    DEFAULT_NOTIFY_ENTITY,
    DEFAULT_RECONNECT_DELAY,
    DEFAULT_SERIAL_TIMEOUT,
    DEFAULT_STALE_CLEANUP_HOURS,
    DOMAIN,
    OUTLIER_CONFIRM_COUNT,
)


def _list_serial_ports() -> list[str]:
    """Return available serial ports sorted by device path."""
    try:
        import serial.tools.list_ports

        ports = sorted(serial.tools.list_ports.comports(), key=lambda p: p.device)
        return [p.device for p in ports]
    except Exception:
        return []


def _port_selector(current: str | None) -> SelectSelector:
    """Dropdown with the detected serial ports; custom input allowed
    (e.g. /dev/serial/by-id/... symlinks that pyserial does not list)."""
    available = _list_serial_ports()
    if current and current not in available:
        available.insert(0, current)
    return SelectSelector(
        SelectSelectorConfig(
            options=available,
            custom_value=True,
            mode=SelectSelectorMode.DROPDOWN,
        )
    )


class JeeLinkConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Initial setup: serial port + auto discovery."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        errors: dict[str, str] = {}

        if user_input is not None:
            await self.async_set_unique_id(user_input[CONF_SERIAL_PORT])
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title="LaCrosse JeeLink Bridge",
                data=user_input,
            )

        port_sel = await self.hass.async_add_executor_job(_port_selector, None)
        schema = vol.Schema(
            {
                vol.Required(CONF_SERIAL_PORT): port_sel,
                vol.Required(CONF_AUTO_ADD, default=True): BooleanSelector(),
                vol.Required(
                    CONF_OUTLIER_CONFIRM_COUNT, default=OUTLIER_CONFIRM_COUNT
                ): NumberSelector(
                    NumberSelectorConfig(min=2, max=20, step=1, mode=NumberSelectorMode.BOX)
                ),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return JeeLinkOptionsFlow(config_entry)


class JeeLinkOptionsFlow(config_entries.OptionsFlow):
    """Change settings after the initial setup (applied immediately via
    an entry reload)."""

    def __init__(self, config_entry):
        self._entry = config_entry

    def _current(self, key: str, default):
        return self._entry.options.get(key, self._entry.data.get(key, default))

    async def async_step_init(self, user_input=None):
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current_port = self._current(CONF_SERIAL_PORT, "")
        port_sel = await self.hass.async_add_executor_job(_port_selector, current_port)

        schema = vol.Schema(
            {
                vol.Required(CONF_SERIAL_PORT, default=current_port): port_sel,
                vol.Required(
                    CONF_AUTO_ADD, default=self._current(CONF_AUTO_ADD, True)
                ): BooleanSelector(),
                vol.Required(
                    CONF_OUTLIER_CONFIRM_COUNT,
                    default=self._current(CONF_OUTLIER_CONFIRM_COUNT, OUTLIER_CONFIRM_COUNT),
                ): NumberSelector(
                    NumberSelectorConfig(min=2, max=20, step=1, mode=NumberSelectorMode.BOX)
                ),
                # Timeouts
                vol.Required(
                    CONF_SERIAL_TIMEOUT,
                    default=self._current(CONF_SERIAL_TIMEOUT, DEFAULT_SERIAL_TIMEOUT),
                ): NumberSelector(
                    NumberSelectorConfig(min=0.5, max=10, step=0.5, mode=NumberSelectorMode.BOX)
                ),
                vol.Required(
                    CONF_RECONNECT_DELAY,
                    default=self._current(CONF_RECONNECT_DELAY, DEFAULT_RECONNECT_DELAY),
                ): NumberSelector(
                    NumberSelectorConfig(min=1, max=600, step=1, mode=NumberSelectorMode.BOX)
                ),
                vol.Required(
                    CONF_BATTERY_REPLACE_TIMEOUT,
                    default=self._current(
                        CONF_BATTERY_REPLACE_TIMEOUT, DEFAULT_BATTERY_REPLACE_TIMEOUT
                    ),
                ): NumberSelector(
                    NumberSelectorConfig(min=30, max=600, step=10, mode=NumberSelectorMode.BOX)
                ),
                vol.Required(
                    CONF_DEBUG_TIMEOUT,
                    default=self._current(CONF_DEBUG_TIMEOUT, DEFAULT_DEBUG_TIMEOUT),
                ): NumberSelector(
                    NumberSelectorConfig(min=60, max=1800, step=30, mode=NumberSelectorMode.BOX)
                ),
                # Firmware init commands (space-separated, FHEM-style)
                vol.Required(
                    CONF_INIT_COMMANDS,
                    default=self._current(CONF_INIT_COMMANDS, DEFAULT_INIT_COMMANDS),
                ): str,
                # Radio-silence watchdog (minutes, 0 = off)
                vol.Required(
                    CONF_DATA_TIMEOUT,
                    default=self._current(CONF_DATA_TIMEOUT, DEFAULT_DATA_TIMEOUT),
                ): NumberSelector(
                    NumberSelectorConfig(min=0, max=1440, step=1, mode=NumberSelectorMode.BOX)
                ),
                # Automatic cleanup of stray auto-discovered sensors (hours, 0 = off)
                vol.Required(
                    CONF_STALE_CLEANUP_HOURS,
                    default=self._current(
                        CONF_STALE_CLEANUP_HOURS, DEFAULT_STALE_CLEANUP_HOURS
                    ),
                ): NumberSelector(
                    NumberSelectorConfig(min=0, max=720, step=1, mode=NumberSelectorMode.BOX)
                ),
                # Discovery threshold (like FHEM autoCreateThreshold):
                # create a new sensor only after N packets within T seconds
                vol.Required(
                    CONF_DISCOVERY_MIN_PACKETS,
                    default=self._current(
                        CONF_DISCOVERY_MIN_PACKETS, DEFAULT_DISCOVERY_MIN_PACKETS
                    ),
                ): NumberSelector(
                    NumberSelectorConfig(min=1, max=10, step=1, mode=NumberSelectorMode.BOX)
                ),
                vol.Required(
                    CONF_DISCOVERY_WINDOW_SEC,
                    default=self._current(
                        CONF_DISCOVERY_WINDOW_SEC, DEFAULT_DISCOVERY_WINDOW_SEC
                    ),
                ): NumberSelector(
                    NumberSelectorConfig(min=10, max=3600, step=10, mode=NumberSelectorMode.BOX)
                ),
                # Notifications: master switch + target entity + types
                vol.Required(
                    CONF_NOTIFY_ENABLED,
                    default=self._current(CONF_NOTIFY_ENABLED, True),
                ): BooleanSelector(),
                vol.Optional(
                    CONF_NOTIFY_ENTITY,
                    default=self._current(CONF_NOTIFY_ENTITY, DEFAULT_NOTIFY_ENTITY),
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="notify")
                ),
                vol.Required(
                    CONF_NOTIFY_CONNECTION,
                    default=self._current(CONF_NOTIFY_CONNECTION, True),
                ): BooleanSelector(),
                vol.Required(
                    CONF_NOTIFY_DATA_TIMEOUT,
                    default=self._current(CONF_NOTIFY_DATA_TIMEOUT, True),
                ): BooleanSelector(),
                vol.Required(
                    CONF_NOTIFY_NEW_SENSOR,
                    default=self._current(CONF_NOTIFY_NEW_SENSOR, True),
                ): BooleanSelector(),
                vol.Required(
                    CONF_NOTIFY_BATTERY_LOW,
                    default=self._current(CONF_NOTIFY_BATTERY_LOW, True),
                ): BooleanSelector(),
                vol.Required(
                    CONF_NOTIFY_BATTERY_REPLACED,
                    default=self._current(CONF_NOTIFY_BATTERY_REPLACED, True),
                ): BooleanSelector(),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
