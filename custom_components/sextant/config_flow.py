from homeassistant import config_entries
from homeassistant.core import callback
import logging
import voluptuous as vol
# Import DOMAIN from the lightweight const module, NOT from the package root:
# `from . import DOMAIN` executes __init__.py (numpy/shapely/watchdog) just
# to load the config flow, and couples config-flow loading to those heavy deps.
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)
OPTION_SHOW_SIDEBAR_PANEL = "show_sidebar_panel"
OPTION_UPDATE_INTERVAL = "update_interval"
DEFAULT_UPDATE_INTERVAL = 15

class SextantConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Sextant."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        """Handle the initial step."""
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title="Sextant", data={})

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Create the options flow."""
        return SextantOptionsFlow(config_entry)


class SextantOptionsFlow(config_entries.OptionsFlow):
    """Handle Sextant options."""

    def __init__(self, config_entry):
        self._config_entry = config_entry

    async def async_step_init(self, user_input=None):
        """Manage the Sextant options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current_value = self._config_entry.options.get(OPTION_SHOW_SIDEBAR_PANEL, True)
        current_interval = self._config_entry.options.get(OPTION_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL)
        schema = vol.Schema({
            vol.Required(OPTION_SHOW_SIDEBAR_PANEL, default=current_value): bool,
            # Seconds between trilateration recomputes per thing. Lower this only
            # if you need faster-than-15s position updates and can spare the CPU.
            vol.Required(OPTION_UPDATE_INTERVAL, default=current_interval): vol.All(
                vol.Coerce(int), vol.Range(min=1, max=300)
            ),
        })
        return self.async_show_form(step_id="init", data_schema=schema)
