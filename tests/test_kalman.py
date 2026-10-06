"""The two-speed Kalman step (_kf_step).

At one step per fifteen-second cycle, the old single process noise let the
filter trust every fix outright: nothing was smoothed, and a thing on a table
reported the fix jitter as its speed. These pin down the two things the step
has to do: settle on a still thing, and follow a moving one at once.
"""
import math
import random

import numpy as np

import sextant

DT = 10.0           # the prediction step, as capped by KF_MAX_DT_S
CYCLE = 15.0        # the wall clock between cycles, which the hold counts down
SCALE = 100.0       # px per metre
R_VAR = (sextant.KF_MEAS_NOISE_M * SCALE) ** 2
A_MOVE = (sextant.KF_ACCEL_NOISE_MS2 * SCALE) ** 2
A_STILL = (sextant.KF_ACCEL_NOISE_STILL_MS2 * SCALE) ** 2


def _fresh(x, y):
    v_var = (sextant.KF_INIT_VEL_UNC_MS * SCALE) ** 2
    return np.array([x, y, 0.0, 0.0], dtype=float), np.diag([R_VAR, R_VAR, v_var, v_var]).astype(float)


def _run(fixes, seed=1):
    x, P = _fresh(*fixes[0])
    moving, out = 0, []
    for z in fixes[1:]:
        x, P, moving, nis = sextant._kf_step(x, P, z, DT, R_VAR, A_MOVE, A_STILL, moving, sextant.KF_MOVE_NIS, sextant.KF_MOVE_HOLD_S, elapsed=CYCLE)
        out.append((x.copy(), moving, nis))
    return out


def _still_fixes(n, jitter_m=1.2, seed=7, at=(1000.0, 800.0)):
    rng = random.Random(seed)
    return [(at[0] + rng.gauss(0, jitter_m * SCALE / 1.5), at[1] + rng.gauss(0, jitter_m * SCALE / 1.5)) for _ in range(n)]


def test_a_still_thing_settles_and_reads_as_still():
    """The AirPods case: 1.2 m of jitter per cycle on a table. After a minute
    or two the estimate sits well inside the jitter, and the speed is far
    under the 0.3 m/s stillness threshold that the room lock needs."""
    out = _run(_still_fixes(40))
    late = out[20:]
    err = sorted(math.hypot(x[0] - 1000.0, x[1] - 800.0) / SCALE for x, _m, _n in late)
    speed = [math.hypot(x[2], x[3]) / SCALE for x, _m, _n in late]
    assert err[len(err) // 2] < 0.5, f"median error should be well inside the jitter, was {err[len(err) // 2]:.2f} m"
    assert err[-1] < 1.0, f"worst error {err[-1]:.2f} m"
    assert max(speed) < 0.1, f"a still thing must read still, speeds up to {max(speed):.2f} m/s"
    assert all(m == 0 for _x, m, _n in late), "jitter alone must not count as moving"


def test_the_old_single_noise_did_not_settle():
    """The bug this replaces, kept as a measurement: with the responsive noise
    alone the estimate follows every fix and the speed reads as jitter."""
    fixes = _still_fixes(40)
    x, P = _fresh(*fixes[0])
    speeds = []
    for z in fixes[1:]:
        x, P, _m, _n = sextant._kf_step(x, P, z, DT, R_VAR, A_MOVE, A_MOVE, 0, 1e9, 0)
        speeds.append(math.hypot(x[2], x[3]) / SCALE)
    assert max(speeds[20:]) > 0.3


def test_a_move_is_followed_at_once():
    """Settled on a table, then carried 6 m across the room: the estimate is
    within a metre of the new place inside three cycles, not a slow drift."""
    still = _still_fixes(30)
    moved = [(1600.0 + dx, 800.0 + dy) for dx, dy in [(0, 0), (30, -20), (-20, 10), (10, 10), (0, 0)]]
    out = _run(still + moved)
    after = out[len(still) - 1:]        # steps that saw the moved fixes
    assert after[0][1] > 0, "the first far fix must switch the filter to moving"
    err = [math.hypot(x[0] - 1600.0, x[1] - 800.0) / SCALE for x, _m, _n in after[:3]]
    assert min(err) < 1.0, f"should have followed within three cycles, errors {['%.2f' % e for e in err]}"


def test_a_slow_walk_is_still_tracked():
    """A cat walking 1 m a cycle never jumps far enough in one step to trip
    the move detector; the quiet filter has to follow it by itself, through
    its velocity, rather than leave it behind."""
    still = _still_fixes(30)
    walk = [(1000.0 + 100.0 * i, 800.0) for i in range(1, 16)]
    out = _run(still + walk)
    tail = out[-1][0]
    assert math.hypot(tail[0] - walk[-1][0], tail[1] - walk[-1][1]) / SCALE < 1.5


def test_the_hold_keeps_following_through_a_pause_then_lets_go():
    """Settled on a table, carried 7 m and set down: the first far fix arms
    the hold, the constant-velocity model overshoots for a cycle or two (which
    re-arms it), and within a few cycles of the fixes standing still it is
    quiet again. (From a FRESH filter the same jump arms nothing: its velocity
    is still unknown, so a jump is within what it expects, and it follows.)"""
    x, P = _fresh(1000.0, 800.0)
    moving = 0
    for z in _still_fixes(30)[1:]:
        x, P, moving, _ = sextant._kf_step(x, P, z, DT, R_VAR, A_MOVE, A_STILL, moving, sextant.KF_MOVE_NIS, sextant.KF_MOVE_HOLD_S, elapsed=CYCLE)
    assert moving == 0
    seen = []
    for _ in range(10):
        x, P, moving, _ = sextant._kf_step(x, P, (1700.0, 800.0), DT, R_VAR, A_MOVE, A_STILL, moving, sextant.KF_MOVE_NIS, sextant.KF_MOVE_HOLD_S, elapsed=CYCLE)
        seen.append(moving)
    assert seen[0] == sextant.KF_MOVE_HOLD_S, f"the jump must arm the hold at once, saw {seen}"
    assert 0 in seen[:8], f"the hold must let go once the fixes stand still, saw {seen}"
    assert seen[-1] == 0 and seen[-2] == 0
    assert math.hypot(x[0] - 1700.0, x[1] - 800.0) / SCALE < 0.3


def test_restored_state_without_the_new_keys_still_runs():
    """A runtime snapshot from before this change carries no 'moving'; the
    update must treat that as quiet, not raise."""
    sextant._kf_position_state.clear()
    sextant._kf_position_state["thing"] = {"x": np.array([1000.0, 800.0, 0.0, 0.0]), "P": np.diag([R_VAR, R_VAR, 1.0, 1.0]), "ts": 0.0, "floor": "F"}
    import time
    sextant._kf_position_state["thing"]["ts"] = time.time() - 5
    px, py = sextant._kalman_position_update("thing", "F", (1010.0, 805.0), SCALE, None)
    st = sextant._kf_position_state["thing"]
    assert st["moving"] == 0 and "nis" in st
    assert abs(px - 1000.0) < 10 and abs(py - 800.0) < 5
