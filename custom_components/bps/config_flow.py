from homeassistant import config_entries
from homeassistant.core import callback
import logging
import voluptuous as vol
from . import DOMAIN

_LOGGER = logging.getLogger(__name__)

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
