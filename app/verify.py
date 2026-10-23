"""Independent check of a plan's score.

The planner's score comes from counters kept inside simulate(). This module does
not use those counters: it recounts everything from the saved hour-by-hour map
states (the same states the map plays), recomputes the cost from the actions, and
applies the score formula again with exact fractions. If both routes give the same
numbers, the score shown on screen is arithmetically correct for the rules used.
"""

from fractions import Fraction

from app.simulator import (CRITICAL_TYPES, FAILED, INTERVENTION_COST, SCORE_WEIGHTS,
                           Intervention, Network)


def indian(n: int) -> str:
    """Indian digit grouping, as on screen: 1283000 -> 12,83,000."""
    sign, digits = ("-", str(-n)) if n < 0 else ("", str(n))
    if len(digits) <= 3:
        return sign + digits
    head, tail = digits[:-3], digits[-3:]
    pairs = []
    while len(head) > 2:
        pairs.insert(0, head[-2:]); head = head[:-2]
    return sign + ",".join([head] + pairs + [tail])


def recount(network: Network, timeline: list[dict]) -> dict:
    """Count losses straight from the timeline. Hour 0 is the incident itself, so
    counting starts at hour 1, exactly like the simulator's rules."""
    failed_asset_hours = 0
    person_hours = 0
    critical_hours = 0
    facility_hours: dict[str, int] = {}
    recovery_hour = None
    for snap in timeline[1:]:
        down = [aid for aid, state in snap["states"].items() if state == FAILED]
        failed_asset_hours += len(down)
        person_hours += sum(network.assets[aid].people_served for aid in down)
        for aid in down:
            if network.assets[aid].type in CRITICAL_TYPES:
                critical_hours += 1
                facility_hours[aid] = facility_hours.get(aid, 0) + 1
        if recovery_hour is None and all(s == "operational" for s in snap["states"].values()):
            recovery_hour = snap["t"]
    return {"failed_asset_hours": failed_asset_hours,
            "total_unserved_service_person_hours": person_hours,
            "critical_facility_outage_hours": critical_hours,
            "facility_hours": facility_hours, "recovery_hour": recovery_hour}


def action_cost(iv: Intervention) -> tuple[int, str]:
    """Cost of one action and how it was worked out, in words."""
    unit = INTERVENTION_COST[iv.kind]
    if iv.kind == "backup":
        return unit * iv.hours, f"₹{indian(unit)} × {iv.hours} h"
    return unit, f"₹{indian(unit)}"


def verify_plan(network: Network, baseline_timeline: list[dict], plan_timeline: list[dict],
                actions: tuple[Intervention, ...], budget: int, engine: dict) -> dict:
    """Recompute the plan score from the timelines and compare it with the planner.

    `engine` is the planner's result for the plan (its impact, cost and score).
    """
    people_data = any(a.people_served for a in network.assets.values())
    measure = "total_unserved_service_person_hours" if people_data else "failed_asset_hours"
    measure_label = "people × hours without service" if people_data else "asset-hours out of service"
    base, plan = recount(network, baseline_timeline), recount(network, plan_timeline)

    costs = [action_cost(iv) for iv in actions]
    cost = sum(c for c, _ in costs)

    def avoided(before, after):
        return Fraction(before - after, before) if before else Fraction(0)

    loss_part = avoided(base[measure], plan[measure])
    critical_part = avoided(base["critical_facility_outage_hours"],
                            plan["critical_facility_outage_hours"])
    if budget:
        cost_part = 1 - Fraction(cost, budget)
    else:
        cost_part = Fraction(1 if cost == 0 else 0)
    w = SCORE_WEIGHTS
    exact = w["unserved"] * loss_part + w["critical"] * critical_part + w["cost"] * cost_part
    score = round(float(exact), 1)

    def part(name, weight, before, after, value, how):
        return {"name": name, "weight": weight, "before": before, "after": after,
                "how": how, "share_pct": round(100 * float(value), 1),
                "points": round(weight * float(value), 2)}

    parts = [
        part(f"Less {measure_label}", w["unserved"], base[measure], plan[measure], loss_part,
             f"({indian(base[measure])} − {indian(plan[measure])}) ÷ {indian(base[measure])}"),
        part("Less critical-facility outage", w["critical"],
             base["critical_facility_outage_hours"], plan["critical_facility_outage_hours"],
             critical_part,
             f"({base['critical_facility_outage_hours']} − "
             f"{plan['critical_facility_outage_hours']}) ÷ {base['critical_facility_outage_hours']}"),
        part("Cost kept low", w["cost"], budget, cost, cost_part,
             f"1 − ₹{indian(cost)} ÷ ₹{indian(budget)}" if budget else "no budget given"),
    ]

    eimp = engine["impact"]
    checks = [
        ("Score", engine["score"], score),
        (measure_label.capitalize(), eimp[measure], plan[measure]),
        ("Critical-facility outage hours", eimp["critical_facility_outage_hours"],
         plan["critical_facility_outage_hours"]),
        ("Full recovery hour", eimp["recovery_hour"], plan["recovery_hour"]),
        ("Cost (₹)", engine["cost"], cost),
    ]
    for f in eimp["critical_facilities_down"]:
        checks.append((f"{f['name']}: hours without service", f["outage_hours"],
                       plan["facility_hours"].get(f["id"], 0)))
    rows = [{"what": what, "planner": a, "recount": b, "match": a == b} for what, a, b in checks]
    return {"measure": measure, "measure_label": measure_label, "parts": parts,
            "cost_lines": [{"action": iv.label(network), "how": how, "cost": c}
                           for iv, (c, how) in zip(actions, costs)],
            "score": score, "planner_score": engine["score"], "checks": rows,
            "all_match": all(r["match"] for r in rows)}
