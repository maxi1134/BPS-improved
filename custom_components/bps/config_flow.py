from homeassistant import config_entries
from homeassistant.core import callback
import logging
import voluptuous as vol
# Import DOMAIN from the lightweight const module, NOT from the package root:
# `from . import DOMAIN` executes __init__.py (scipy/shapely/numpy/watchdog) just
# to load the config flow, and couples config-flow loading to those heavy deps.
from .const import (
    DOMAIN,
    OPTION_UPDATE_INTERVAL,
    DEFAULT_UPDATE_INTERVAL,
    MIN_UPDATE_INTERVAL,
    MAX_UPDATE_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)
OPTION_SHOW_SIDEBAR_PANEL = "show_sidebar_panel"

# Definiera vilka inställningar användaren kan ange
CONFIG_SCHEMA = vol.Schema(
    {
        vol.Required("hass_token"): str,
        vol.Required("hassURL"): str,
        vol.Optional("update_interval", default=1): int,
    }
)

class BPSConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for BPS."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        """Handle the initial step."""
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title="BLE Positioning System", data={})

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Create the options flow."""
        return BPSOptionsFlow(config_entry)


class BPSOptionsFlow(config_entries.OptionsFlow):
    """Handle BPS options."""

    def __init__(self, config_entry):
        self._config_entry = config_entry

    async def async_step_init(self, user_input=None):
        """Manage the BPS options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current_value = self._config_entry.options.get(OPTION_SHOW_SIDEBAR_PANEL, True)
        current_interval = self._config_entry.options.get(
            OPTION_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL
        )
        schema = vol.Schema({
            vol.Required(OPTION_SHOW_SIDEBAR_PANEL, default=current_value): bool,
            vol.Required(OPTION_UPDATE_INTERVAL, default=current_interval): vol.All(
                vol.Coerce(int),
                vol.Range(min=MIN_UPDATE_INTERVAL, max=MAX_UPDATE_INTERVAL),
            ),
        })
        return self.async_show_form(step_id="init", data_schema=schema)
