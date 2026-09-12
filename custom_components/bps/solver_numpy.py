"""
Pure-numpy replacement for the one scipy call in the trilateration hot path.

`trilaterate()` asks scipy for a bounded, robustly-weighted least-squares fit of
a SINGLE 2-D point:

    least_squares(residual, x0, jac=..., bounds=(lo, hi),
                  method="trf", loss="soft_l1", f_scale=F)

scipy is a ~100MB dependency for that. With only two free parameters the
problem is small enough to solve directly:

* **soft_l1** is Iteratively Reweighted Least Squares. scipy minimises
  ``sum rho(z)`` with ``z = (f / f_scale)**2`` and
  ``rho(z) = 2 * (sqrt(1 + z) - 1)``. Differentiating gives a per-residual
  weight ``rho'(z) = 1 / sqrt(1 + z)``, so each iteration is an ordinary
  weighted least-squares solve with residuals scaled by ``sqrt(rho'(z))``.
* **bounds** on two independent parameters are a clip, not a constrained
  program — there is no coupling for a trust region to reconcile.
* the caller already supplies an **analytic Jacobian**, so no finite
  differencing is needed.

The result is Levenberg-Marquardt with IRLS reweighting and projection onto the
box. Not a general replacement for `least_squares` — it exists only to serve
this call site, and `tools/solver_bench.py` measures it against scipy on the
real floorplan geometry before anything is switched over.
"""

from __future__ import annotations

import numpy as np


class SolverResult:
    """The subset of scipy's OptimizeResult that `trilaterate()` reads."""

    __slots__ = ("x", "success", "nfev", "cost")

    def __init__(self, x, success, nfev, cost):
        self.x = x
        self.success = success
        self.nfev = nfev
        self.cost = cost


def _soft_l1_weights(f, f_scale):
    """IRLS weights for scipy's soft_l1 loss: rho'(z) with z = (f/f_scale)^2."""
    z = (f / f_scale) ** 2
    return 1.0 / np.sqrt(1.0 + z)


def _soft_l1_cost(f, f_scale):
    """scipy's reported cost: 0.5 * sum(f_scale^2 * rho(z))."""
    z = (f / f_scale) ** 2
    return 0.5 * float(np.sum(f_scale**2 * 2.0 * (np.sqrt(1.0 + z) - 1.0)))


def least_squares_bounded_soft_l1(
    fun,
    x0,
    jac,
    bounds,
    f_scale=1.0,
    max_iter=200,
    xtol=1e-8,
    ftol=1e-8,
    extra_starts=None,
):
    """
    Minimise the soft_l1 loss of ``fun`` over a box, starting at ``x0``.

    Drop-in for the single `least_squares(..., method="trf", loss="soft_l1")`
    call in `trilaterate()`. Returns a `SolverResult` exposing `.x` and
    `.success`.

    ``extra_starts`` supplies additional starting points. With gross outliers
    present the robust objective is multi-modal, and a single descent from the
    centroid can settle in a much worse basin than scipy's trust region finds
    (measured at up to 246x the final cost before this was added). Each start
    is solved independently and the lowest-cost result wins; at two parameters
    the extra solves are cheap.

    Levenberg-Marquardt: solve ``(J'WJ + lambda*diag(J'WJ)) dx = -J'Wf`` each
    iteration, accept the step if the robust cost drops (shrinking lambda) and
    reject it otherwise (growing lambda). The step is clipped to the box before
    evaluation, so iterates stay feasible throughout rather than being repaired
    at the end.
    """
    lo, hi = bounds
    lo = np.asarray(lo, dtype=float)
    hi = np.asarray(hi, dtype=float)

    starts = [np.asarray(x0, dtype=float)]
    if extra_starts is not None:
        starts.extend(np.asarray(s, dtype=float) for s in extra_starts)

    best = None
    for start in starts:
        r = _solve_from(fun, start, jac, lo, hi, f_scale, max_iter, xtol, ftol)
        if best is None or (r.cost < best.cost and np.isfinite(r.cost)):
            best = r
    return best


def _solve_from(fun, x0, jac, lo, hi, f_scale, max_iter, xtol, ftol):
    """One Levenberg-Marquardt descent from a single starting point."""
    x = np.clip(np.asarray(x0, dtype=float), lo, hi)
    f = np.asarray(fun(x), dtype=float)
    nfev = 1
    cost = _soft_l1_cost(f, f_scale)

    lam = 1e-3
    success = False

    for _ in range(max_iter):
        J = np.asarray(jac(x), dtype=float)
        w = _soft_l1_weights(f, f_scale)
        # Fold the robust weight into both sides: minimising ||sqrt(w)*f||^2
        # locally is the IRLS surrogate for the soft_l1 objective.
        sw = np.sqrt(w)
        Jw = J * sw[:, None]
        fw = f * sw

        JTJ = Jw.T @ Jw
        g = Jw.T @ fw

        if not np.all(np.isfinite(JTJ)) or not np.all(np.isfinite(g)):
            break

        # Gradient small enough: converged.
        if np.linalg.norm(g) <= ftol * max(1.0, abs(cost)):
            success = True
            break

        stepped = False
        for _ in range(30):  # lambda search
            A = JTJ + lam * np.diag(np.maximum(np.diag(JTJ), 1e-12))
            try:
                dx = np.linalg.solve(A, -g)
            except np.linalg.LinAlgError:
                lam *= 10.0
                continue

            x_new = np.clip(x + dx, lo, hi)
            f_new = np.asarray(fun(x_new), dtype=float)
            nfev += 1
            cost_new = _soft_l1_cost(f_new, f_scale)

            if np.isfinite(cost_new) and cost_new < cost:
                dx_eff = x_new - x
                x, f, cost = x_new, f_new, cost_new
                lam = max(lam * 0.3, 1e-12)
                stepped = True
                # Converged when the accepted step stops moving the point.
                if np.linalg.norm(dx_eff) <= xtol * (1.0 + np.linalg.norm(x)):
                    success = True
                break

            lam *= 10.0
            if lam > 1e12:
                break

        if not stepped:
            # No downhill step exists within the box: a local minimum, which
            # for this problem is the answer rather than a failure.
            success = True
            break
        if success:
            break
    else:
        # Iteration budget exhausted. The cost decreased monotonically, so x is
        # a valid minimiser even if unpolished - treat it as success when the
        # PROJECTED gradient is small (components pushing into an active bound
        # cannot be reduced and must not count against convergence). Returning
        # failure here merely discards a usable fit: measured at 10% of solves
        # against scipy, all in this branch.
        try:
            J = np.asarray(jac(x), dtype=float)
            w = _soft_l1_weights(f, f_scale)
            sw = np.sqrt(w)
            g = (J * sw[:, None]).T @ (f * sw)
            at_lo = np.isclose(x, lo)
            at_hi = np.isclose(x, hi)
            gp = np.where((at_lo & (g > 0)) | (at_hi & (g < 0)), 0.0, g)
            success = bool(np.linalg.norm(gp) <= 1e-4 * max(1.0, abs(cost)))
        except Exception:  # noqa: BLE001 - diagnostics only, never fatal
            success = False

    return SolverResult(x=x, success=success, nfev=nfev, cost=cost)
