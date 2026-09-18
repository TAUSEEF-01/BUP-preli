"""Minimum-cost 24-hour schedule as a linear program (HiGHS via SciPy).

Variables per hour h: grid g, solar used s, charge c, discharge d, battery energy after the hour E.
    g + s + d = demand + c
    E[h] = E[h-1] + c - d          (E[-1] = initial energy)
    reserve_floor[h] <= E[h] <= capacity,  E[23] = initial energy
    0 <= s <= solar_cap, 0 <= c <= charge_cap, 0 <= d <= discharge_cap, 0 <= g <= grid_cap
Stage 1 minimizes grid cost. Stage 2 keeps that cost and minimizes total charge + discharge,
which removes pointless cycling (including simultaneous charge and discharge).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.optimize import linprog

from app.directives import HourlyLimits
from app.schemas import HOURS_PER_DAY, Scenario

H = HOURS_PER_DAY
G, S, C, D, E = (i * H for i in range(5))
N_VARS = 5 * H
# Stage 2 may exceed the stage-1 optimum by at most this much. It must stay tiny: any slack
# is spent on shaving throughput, which shows up as values like 64.9999 kWh.
STAGE2_COST_SLACK = 1e-7


class InfeasibleError(Exception):
    """The compiled constraints admit no schedule."""


class SolverError(Exception):
    """The solver failed for a reason other than infeasibility."""


@dataclass(frozen=True)
class Solution:
    grid: tuple[float, ...]
    solar: tuple[float, ...]
    charge: tuple[float, ...]
    discharge: tuple[float, ...]
    energy: tuple[float, ...]
    cost: float


def _bounds(scenario: Scenario, limits: HourlyLimits, margin: float) -> list[tuple[float, float | None]]:
    capacity = scenario.battery.capacity_kwh

    def upper(value: float) -> float:
        return max(0.0, value - margin) if value > 0 else 0.0

    grid = [(0.0, None if cap is None else upper(cap)) for cap in limits.grid_cap]
    solar = [(0.0, upper(cap)) for cap in limits.solar_cap]
    charge = [(0.0, upper(cap)) for cap in limits.charge_cap]
    discharge = [(0.0, upper(cap)) for cap in limits.discharge_cap]
    energy = []
    for hour in range(H):
        low, high = limits.reserve_floor[hour], capacity
        # Hour 23 is pinned to the initial energy, so it gets no inward margin.
        if margin > 0 and hour < H - 1 and high - low > 2 * margin:
            low, high = low + margin, high - margin
        energy.append((low, high))
    return grid + solar + charge + discharge + energy


def solve(scenario: Scenario, limits: HourlyLimits, margin: float = 0.0) -> Solution:
    demand = np.array([h.demand_kwh for h in scenario.hours], dtype=float)
    tariff = np.array([h.tariff_bdt_per_kwh for h in scenario.hours], dtype=float)
    initial = scenario.battery.initial_energy_kwh

    a_eq = np.zeros((2 * H + 1, N_VARS))
    b_eq = np.zeros(2 * H + 1)
    for hour in range(H):
        # Energy balance: g + s + d - c = demand
        a_eq[hour, [G + hour, S + hour, D + hour]] = 1.0
        a_eq[hour, C + hour] = -1.0
        b_eq[hour] = demand[hour]
        # Battery transition: E[h] - E[h-1] - c + d = 0 (initial energy on the right for h = 0)
        row = H + hour
        a_eq[row, E + hour] = 1.0
        a_eq[row, C + hour] = -1.0
        a_eq[row, D + hour] = 1.0
        if hour == 0:
            b_eq[row] = initial
        else:
            a_eq[row, E + hour - 1] = -1.0
    # End-of-day neutrality.
    a_eq[2 * H, E + H - 1] = 1.0
    b_eq[2 * H] = initial

    bounds = _bounds(scenario, limits, margin)
    cost_vector = np.zeros(N_VARS)
    cost_vector[G : G + H] = tariff

    stage1 = linprog(cost_vector, A_eq=a_eq, b_eq=b_eq, bounds=bounds, method="highs")
    if stage1.status == 2:
        raise InfeasibleError("constraints are infeasible")
    if stage1.status != 0 or stage1.x is None:
        raise SolverError(f"solver status {stage1.status}")
    optimum = float(stage1.fun)

    throughput = np.zeros(N_VARS)
    throughput[C : C + H] = 1.0
    throughput[D : D + H] = 1.0
    stage2 = linprog(
        throughput,
        A_ub=cost_vector.reshape(1, -1),
        b_ub=[optimum + STAGE2_COST_SLACK],
        A_eq=a_eq,
        b_eq=b_eq,
        bounds=bounds,
        method="highs",
    )
    x = stage2.x if stage2.status == 0 and stage2.x is not None else stage1.x

    return Solution(
        grid=tuple(x[G : G + H]),
        solar=tuple(x[S : S + H]),
        charge=tuple(x[C : C + H]),
        discharge=tuple(x[D : D + H]),
        energy=tuple(x[E : E + H]),
        cost=float(cost_vector @ x),
    )
