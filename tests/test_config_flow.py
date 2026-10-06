"""The config flow: one Sextant entry, and an options form with the panel toggle and the update interval."""
import asyncio
import types

import pytest

import sextant  # noqa: F401
from sextant import config_flow as cf
from sextant.const import DOMAIN

from homeassistant import config_entries


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_user_step_creates_the_single_entry():
    flow = cf.SextantConfigFlow()
    flow.hass = types.SimpleNamespace(configured_unique_ids=set())
    result = run(flow.async_step_user())
    assert result["type"] == "create_entry" and result["title"] == "Sextant" and result["data"] == {}
    assert flow.unique_id == DOMAIN and cf.SextantConfigFlow.domain == DOMAIN


def test_second_entry_is_refused():
    flow = cf.SextantConfigFlow()
    flow.hass = types.SimpleNamespace(configured_unique_ids={DOMAIN})
    with pytest.raises(config_entries.AbortFlow):
        run(flow.async_step_user())


def test_options_flow_shows_the_form_then_stores_the_choices():
    entry = types.SimpleNamespace(options={cf.OPTION_UPDATE_INTERVAL: 30})
    flow = cf.SextantConfigFlow.async_get_options_flow(entry)
    assert isinstance(flow, cf.SextantOptionsFlow)
    shown = run(flow.async_step_init())
    assert shown["type"] == "form" and shown["step_id"] == "init"
    saved = run(flow.async_step_init({cf.OPTION_SHOW_SIDEBAR_PANEL: False, cf.OPTION_UPDATE_INTERVAL: 5}))
    assert saved == {"type": "create_entry", "title": "", "data": {cf.OPTION_SHOW_SIDEBAR_PANEL: False, cf.OPTION_UPDATE_INTERVAL: 5}}
    assert cf.DEFAULT_UPDATE_INTERVAL == 15
