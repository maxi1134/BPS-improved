"""Configurable tracking cadence and the off-loop trilateration solve.

The tracking loop used to re-solve every tracker once per second with the
scipy fit running on the event loop. The cadence is now an entry option
(default 1 s, unchanged) and the solve runs in the executor.
"""
import asyncio
import types

import bps
from bps.const import (
    DEFAULT_UPDATE_INTERVAL,
    MAX_UPDATE_INTERVAL,
    MIN_UPDATE_INTERVAL,
    OPTION_UPDATE_INTERVAL,
)


def _config(options):
    return types.SimpleNamespace(options=options)


def test_default_interval_is_the_historical_one_second(hass):
    assert DEFAULT_UPDATE_INTERVAL == 1
    assert bps._configured_update_interval(hass, _config({})) == 1


def test_interval_is_read_from_the_entry_options(hass):
    assert bps._configured_update_interval(hass, _config({OPTION_UPDATE_INTERVAL: 5})) == 5


def test_interval_is_clamped_to_the_allowed_range(hass):
    low = bps._configured_update_interval(hass, _config({OPTION_UPDATE_INTERVAL: 0}))
    high = bps._configured_update_interval(hass, _config({OPTION_UPDATE_INTERVAL: 999}))
    assert low == MIN_UPDATE_INTERVAL
    assert high == MAX_UPDATE_INTERVAL


def test_garbage_interval_falls_back_to_the_default(hass):
    for bad in ("fast", None, [], {}):
        assert bps._configured_update_interval(hass, _config({OPTION_UPDATE_INTERVAL: bad})) == (
            DEFAULT_UPDATE_INTERVAL
        )


def test_yaml_style_config_reads_the_entry(hass):
    # async_setup is also reached with a plain config dict (no .options): the
    # interval then comes from the first BPS config entry, if any.
    entry = types.SimpleNamespace(options={OPTION_UPDATE_INTERVAL: 7})
    hass.config_entries = types.SimpleNamespace(async_entries=lambda _domain: [entry])
    assert bps._configured_update_interval(hass, {}) == 7
    hass.config_entries = types.SimpleNamespace(async_entries=lambda _domain: [])
    assert bps._configured_update_interval(hass, {}) == DEFAULT_UPDATE_INTERVAL


def test_trilaterate_is_dispatched_through_the_executor(hass, monkeypatch):
    # Every solve must go through hass.async_add_executor_job, never be called
    # directly on the loop.
    calls = []
    original = hass.async_add_executor_job

    async def spy(func, *args):
        calls.append(getattr(func, "func", func))
        return await original(func, *args)

    hass.async_add_executor_job = spy
    solved = []
    real = bps.trilaterate

    def fake_trilaterate(points, bounds=None, min_weight_radius=1e-3):
        solved.append(len(points))
        return real(points, bounds=bounds, min_weight_radius=min_weight_radius)

    monkeypatch.setattr(bps, "trilaterate", fake_trilaterate)

    scale = 40.0
    receivers = [
        {"entity_id": f"r{i}", "cords": {"x": x, "y": y, "r": d * scale}, "distance": d}
        for i, (x, y, d) in enumerate([(0, 0, 2.0), (400, 0, 8.5), (0, 400, 8.5), (400, 400, 11.0)])
    ]
    data = [{"entity": "phone", "data": {"floor": [{"name": "F", "scale": scale, "receivers": receivers, "zones": [], "subzones": []}]}}]
    asyncio.run(bps.update_trilateration_and_zone(hass, data, "phone"))

    assert solved, "the solver never ran"
    assert fake_trilaterate in calls, "trilaterate was called on the loop instead of the executor"
