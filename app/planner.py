"""Response planning under real limits (crews, generators, budget).

The planner tries every combination of feasible actions that fits the available
resources, re-simulates each one from the same starting conditions, and ranks the
plans with the same transparent weights as the Intervention Lab. No AI is involved
in any number: every number comes from the rule-based simulator, and the best
plan's score is recounted independently by app.verify.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations

from app.simulator import (SCORE_WEIGHTS, Intervention, Network, Scenario,
                           candidate_interventions, check_feasibility, cost_part, hazard_profile,
                           damaged_assets, expedited_repair_hour, intervention_delay,
                           service_loss_measure, simulate)
from app.verify import verify_plan

# Which resource each kind of action uses up.
RESOURCE_USE = {"repair": "crews", "alternate": "crews", "backup": "generators"}
MAX_ACTIONS = 4


@dataclass(frozen=True)
class Resources:
    crews: int = 2         # extra repair / switching crews available
    generators: int = 2    # temporary generators available
    budget: int = 60000    # money available for the response (INR)


def action_timing(network: Network, scenario: Scenario, iv: Intervention) -> str:
    """Plain-language 'when does this take effect' for one action."""
    if iv.kind == "repair":
        start = damaged_assets(scenario)[iv.target] + hazard_profile(scenario.hazard).crew_delay
        return f"crew starts at hour {start}, repair done by hour {iv.hours}"
    when = intervention_delay(scenario, iv.kind)
    return f"{'generator delivered' if iv.kind == 'backup' else 'switched on'} at hour {when}"


def describe(network: Network, scenario: Scenario, iv: Intervention) -> dict:
    return {"kind": iv.kind, "target": iv.target, "hours": iv.hours,
            "label": iv.label(network), "cost": iv.cost, "uses": RESOURCE_USE[iv.kind],
            "timing": action_timing(network, scenario, iv)}


def score(baseline_impact: dict, impact: dict, cost: int, budget: int) -> dict:
    measure = service_loss_measure(baseline_impact)
    base_u = baseline_impact[measure]
    base_c = baseline_impact["critical_facility_outage_hours"]
    avoided_u = (base_u - impact[measure]) / base_u if base_u else 0
    avoided_c = (base_c - impact["critical_facility_outage_hours"]) / base_c if base_c else 0
    return {"avoided_unserved_pct": round(100 * avoided_u, 1),
            "avoided_critical_pct": round(100 * avoided_c, 1),
            "score": round(SCORE_WEIGHTS["unserved"] * avoided_u
                           + SCORE_WEIGHTS["critical"] * avoided_c
                           + SCORE_WEIGHTS["cost"] * cost_part(cost, budget), 1)}


def evaluate(network: Network, scenario: Scenario, actions: tuple[Intervention, ...],
             baseline_impact: dict, resources: Resources) -> dict:
    impact = simulate(network, scenario, actions).impact
    cost = sum(iv.cost for iv in actions)
    used = {"crews": 0, "generators": 0}
    for iv in actions:
        used[RESOURCE_USE[iv.kind]] += 1
    return {"actions": [describe(network, scenario, iv) for iv in actions],
            "impact": impact, "cost": cost, "crews_used": used["crews"],
            "generators_used": used["generators"],
            **score(baseline_impact, impact, cost, resources.budget)}


def fits(actions: tuple[Intervention, ...], resources: Resources) -> bool:
    crews = sum(1 for iv in actions if RESOURCE_USE[iv.kind] == "crews")
    gens = sum(1 for iv in actions if RESOURCE_USE[iv.kind] == "generators")
    return (crews <= resources.crews and gens <= resources.generators
            and sum(iv.cost for iv in actions) <= resources.budget)


def plan_response(network: Network, scenario: Scenario, resources: Resources,
                  previous: tuple[Intervention, ...] = ()) -> dict:
    baseline = simulate(network, scenario)
    feasible = [iv for iv in candidate_interventions(network, scenario, baseline)
                if check_feasibility(network, scenario, iv) is None]
    candidates = []
    for size in range(0, min(MAX_ACTIONS, len(feasible)) + 1):
        for combo in combinations(feasible, size):
            if fits(combo, resources):
                candidates.append((evaluate(network, scenario, combo, baseline.impact,
                                            resources), combo))
    # Best score first; ties go to the cheaper plan, then the plan with fewer actions.
    candidates.sort(key=lambda c: (-c[0]["score"], c[0]["cost"], len(c[0]["actions"]),
                                   [a["label"] for a in c[0]["actions"]]))
    plans = [p for p, _ in candidates]
    best_actions = candidates[0][1]
    best_timeline = simulate(network, scenario, best_actions).timeline
    result = {"baseline_impact": baseline.impact, "resources": vars(resources),
              "best": plans[0], "alternatives": plans[1:4], "plans_checked": len(plans),
              "weights": SCORE_WEIGHTS,
              "baseline_timeline": baseline.timeline, "best_timeline": best_timeline,
              "verification": verify_plan(network, baseline.timeline, best_timeline,
                                          best_actions, resources.budget, plans[0])}
    if previous:
        result["previous_plan"] = evaluate_previous(network, scenario, previous,
                                                    baseline.impact, resources)
    return result


def evaluate_previous(network: Network, scenario: Scenario, previous: tuple[Intervention, ...],
                      baseline_impact: dict, resources: Resources) -> dict:
    """How the earlier plan performs under the updated scenario (after field reports).

    An extra repair crew still finishes in 60% of the work time, but it cannot beat
    reported delays, so its completion hour is recomputed for the updated scenario.
    """
    previous = tuple(
        Intervention("repair", iv.target, expedited_repair_hour(network, scenario, iv.target))
        if iv.kind == "repair" and iv.target in damaged_assets(scenario) else iv
        for iv in previous)
    problems = [f"{iv.label(network)}: {reason}" for iv in previous
                if (reason := check_feasibility(network, scenario, iv))]
    if problems:
        return {"feasible": False, "problems": problems}
    return {"feasible": True, **evaluate(network, scenario, previous, baseline_impact, resources)}
