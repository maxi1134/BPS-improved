"""A faster refresh gives more fixes, not jumpier ones.

The countdown's menu sets a temporary interval (set_interval_override); the
smoothing and the hold timers are wall clock, so three 5 s cycles move the
estimates as far as one 15 s cycle did.
"""
import asyncio
import time

import numpy as np

import sextant


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _reset():
    sextant._floor_probability.clear()
    sextant._floor_probability_at.clear()
    sextant._interval_override.update(secs=None, until=0.0)


# --- smoothing per second, not per cycle ------------------------------------------

def test_the_smoothing_weight_is_as_tuned_at_15_s_and_compounds_below_it():
    w = sextant.FLOOR_PROB_SMOOTHING
    assert sextant._step_weight(w, 0.0, 15.0) == w
    assert abs(sextant._step_weight(w, 0.0, 5.0) ** 3 - w) < 1e-12
    assert sextant._step_weight(w, None, 5.0) == w, "no previous step: as tuned"
    assert sextant._step_weight(w, 0.0, 3600.0) == w ** (sextant.SMOOTHING_MAX_STEP_S / sextant.SMOOTHING_REF_S), "a long gap is capped"


def test_three_5_s_cycles_move_the_floor_odds_as_far_as_one_15_s_cycle():
    _reset()
    sextant._update_floor_probabilities("slow", {"A": 1.0}, now=0.0)
    sextant._update_floor_probabilities("fast", {"A": 1.0}, now=0.0)
    slow = sextant._update_floor_probabilities("slow", {"A": 0.2, "B": 0.8}, now=15.0)
    for t in (5.0, 10.0, 15.0):
        fast = sextant._update_floor_probabilities("fast", {"A": 0.2, "B": 0.8}, now=t)
    assert abs(slow["B"] - fast["B"]) < 1e-9, (slow, fast)


def test_the_first_step_after_a_reset_is_a_whole_one():
    _reset()
    sextant._update_floor_probabilities("e", {"A": 1.0}, now=100.0)
    sextant._floor_probability.pop("e")                 # reset, the time left behind
    probs = sextant._update_floor_probabilities("e", {"B": 1.0}, now=100.001)
    assert probs == {"B": 1.0}, "an empty history must not swallow the new evidence"


def test_the_kalman_hold_lasts_the_same_wall_clock_at_any_interval():
    def hold_after_jump(cycle):
        x = np.array([1000.0, 800.0, 0.0, 0.0]); P = np.diag([100.0, 100.0, 1.0, 1.0])
        for _ in range(40):
            x, P, moving, _ = sextant._kf_step(x, P, (1000.0, 800.0), min(cycle, 10.0), 900.0, 0.2, 1e-4, 0,
                                               sextant.KF_MOVE_NIS, sextant.KF_MOVE_HOLD_S, elapsed=cycle)
        moving = sextant.KF_MOVE_HOLD_S                     # just jumped
        held = 0.0
        while moving > 0:
            x, P, moving, _ = sextant._kf_step(x, P, (float(x[0]), float(x[1])), min(cycle, 10.0), 900.0, 0.2, 1e-4, moving,
                                               1e9, sextant.KF_MOVE_HOLD_S, elapsed=cycle)
            held += cycle
        return held
    assert hold_after_jump(15.0) == hold_after_jump(5.0) == sextant.KF_MOVE_HOLD_S


# --- the temporary interval -----------------------------------------------------------

def test_an_override_lasts_a_quarter_hour_then_lapses():
    _reset()
    info = sextant.set_interval_override(5, now=1000.0)
    assert info["secs"] == 5.0 and info["until"] == 1000.0 + sextant.INTERVAL_OVERRIDE_S
    assert sextant.cycle_interval(now=1000.0 + 60) == 5.0
    assert sextant.cycle_interval(now=1000.0 + sextant.INTERVAL_OVERRIDE_S + 1) == float(sextant.secToUpdate)
    assert sextant.interval_info(now=1000.0 + sextant.INTERVAL_OVERRIDE_S + 1)["until"] is None


def test_picking_normal_ends_the_override():
    _reset()
    sextant.set_interval_override(5, now=1000.0)
    assert sextant.set_interval_override(None, now=1001.0)["until"] is None
    sextant.set_interval_override(5, now=1000.0)
    assert sextant.set_interval_override(sextant.secToUpdate, now=1001.0)["until"] is None
    assert int(sextant.secToUpdate) in sextant.interval_info()["choices"]


def test_the_wait_is_re_timed_when_the_interval_changes(monkeypatch):
    _reset()
    monkeypatch.setattr(sextant, "secToUpdate", 60)
    monkeypatch.setattr(sextant, "_cycle_wake", None)

    async def scenario():
        waiting = asyncio.ensure_future(sextant._wait_for_next_cycle())
        await asyncio.sleep(0.05)
        assert not waiting.done(), "sitting out the minute"
        sextant.set_interval_override(0.1)                  # the panel picks a faster refresh
        started = time.monotonic()
        await asyncio.wait_for(waiting, timeout=2.0)
        return time.monotonic() - started

    assert run(scenario()) < 1.0
    _reset()
