"""Deterministic, time-stepped cascade simulator with an evidence ledger.

Rules are applied synchronously each hour from the previous hour's states, so
results do not depend on the order in which assets are processed.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import networkx as nx

OPERATIONAL, ON_BACKUP, FAILED = "operational", "on_backup", "failed"
CRITICAL_TYPES = {"hospital", "shelter", "eoc"}
SEVERITY_REPAIR_MULTIPLIER = {"low": 1.0, "medium": 1.5, "high": 2.0}

INTERVENTION_COST = {"repair": 40000, "backup": 1500, "alternate": 20000}
INTERVENTION_DELAY_HOURS = {"repair": 0, "backup": 2, "alternate": 1}
SCORE_WEIGHTS = {"unserved": 60, "critical": 30, "cost": 10}
EXPEDITE_FACTOR = 0.6  # an extra crew finishes the repair in 60% of the normal time


@dataclass(frozen=True)
class HazardProfile:
    """How a hazard changes the rules. Illustrative values, not measured data."""
    description: str
    crew_delay: int = 0           # hours before repair crews can start on damaged assets
    delivery_delay: int = 0       # extra hours to deliver temporary generators
    alternate_delay: int = 0      # extra hours before an alternate line can be switched on
    repair_factor: float = 1.0    # repair takes this many times longer
    capacity_factor: float = 1.0  # share of substation capacity available for extra load
    backup_factor: float = 1.0    # share of normal on-site backup duration available


HAZARDS = {
    "equipment fault": HazardProfile(
        "Local equipment fault: crews start immediately, no wider disruption."),
    "flood": HazardProfile(
        "Waterlogged roads: crews reach damaged assets 4h late and generators arrive 2h late.",
        crew_delay=4, delivery_delay=2),
    "storm": HazardProfile(
        "Fallen trees and lines: crews start 2h late, generators arrive 1h late and "
        "alternate lines need 2h of inspection before switching on.",
        crew_delay=2, delivery_delay=1, alternate_delay=2),
    "heatwave": HazardProfile(
        "Extreme heat: substations have only 80% of capacity for extra load and on-site "
        "backup lasts 25% less because of cooling load.",
        capacity_factor=0.8, backup_factor=0.75),
    "fire": HazardProfile(
        "Fire: the site must be made safe (2h) and fire damage makes repairs 25% slower.",
        crew_delay=2, repair_factor=1.25),
}
NEUTRAL_HAZARD = HAZARDS["equipment fault"]


def hazard_profile(name: str) -> HazardProfile:
    return HAZARDS.get(name, NEUTRAL_HAZARD)


@dataclass(frozen=True)
class Asset:
    id: str
    name: str
    type: str
    service: str
    capacity: float
    people_served: int
    backup_hours: int
    repair_hours: int
    x: int
    y: int
    lat: float = 0.0
    lon: float = 0.0
    zone: str = ""


@dataclass(frozen=True)
class Edge:
    id: str
    supplier: str
    dependent: str
    load: float
    active: bool = True


@dataclass(frozen=True)
class Network:
    assets: dict[str, Asset]
    edges: dict[str, Edge]
    graph: nx.DiGraph


REPORT_KINDS = ("repair_delayed", "asset_down", "backup_left")


@dataclass(frozen=True)
class FieldReport:
    """A confirmed observation from the field, used to update the scenario.

    repair_delayed: repair of a damaged asset will finish `hours` later than planned
    asset_down:     the asset was found failed at `hour` (a new, confirmed failure)
    backup_left:    at `hour`, the asset has only `hours` of on-site backup left
    """
    kind: str
    asset: str
    hour: int
    hours: int = 0


@dataclass(frozen=True)
class Scenario:
    hazard: str
    severity: str
    failed_assets: tuple[str, ...]
    horizon_hours: int = 48
    reports: tuple[FieldReport, ...] = ()


@dataclass(frozen=True)
class Intervention:
    kind: str  # repair | backup | alternate
    target: str  # asset id for repair/backup, edge id for alternate
    hours: int = 0

    @property
    def cost(self) -> int:
        unit = INTERVENTION_COST[self.kind]
        return unit * self.hours if self.kind == "backup" else unit

    def label(self, network: Network) -> str:
        if self.kind == "repair":
            return (f"Send an extra crew to {network.assets[self.target].name} "
                    f"(repair done by hour {self.hours})")
        if self.kind == "backup":
            return f"Deploy {self.hours}h backup power to {network.assets[self.target].name}"
        edge = network.edges[self.target]
        return (f"Activate alternate supply {network.assets[edge.supplier].name} -> "
                f"{network.assets[edge.dependent].name}")


@dataclass
class SimulationResult:
    ledger: list[dict] = field(default_factory=list)
    timeline: list[dict] = field(default_factory=list)
    impact: dict = field(default_factory=dict)


def damaged_assets(scenario: Scenario) -> dict[str, int]:
    """Damaged asset -> hour it failed: hazard damage at hour 0, field-reported failures later."""
    damaged = {aid: 0 for aid in scenario.failed_assets}
    for r in scenario.reports:
        if r.kind == "asset_down" and r.asset not in damaged:
            damaged[r.asset] = r.hour
    return damaged


def reported_repair_delay(scenario: Scenario, asset_id: str) -> int:
    return sum(r.hours for r in scenario.reports
               if r.kind == "repair_delayed" and r.asset == asset_id)


def repair_work_hours(network: Network, scenario: Scenario, asset_id: str) -> int:
    return math.ceil(network.assets[asset_id].repair_hours
                     * SEVERITY_REPAIR_MULTIPLIER[scenario.severity]
                     * hazard_profile(scenario.hazard).repair_factor)


def default_repair_hour(network: Network, scenario: Scenario, asset_id: str) -> int:
    """Hour at which the normal repair of a damaged asset completes.

    = hour it failed + crew access delay for the hazard + reported delays + repair work.
    """
    start = damaged_assets(scenario).get(asset_id, 0)
    return (start + hazard_profile(scenario.hazard).crew_delay
            + reported_repair_delay(scenario, asset_id)
            + repair_work_hours(network, scenario, asset_id))


def expedited_repair_hour(network: Network, scenario: Scenario, asset_id: str) -> int:
    """Completion hour with an extra crew: same delays, 60% of the repair work."""
    work = repair_work_hours(network, scenario, asset_id)
    return (default_repair_hour(network, scenario, asset_id) - work
            + max(1, math.ceil(work * EXPEDITE_FACTOR)))


def initial_backup_hours(network: Network, scenario: Scenario, asset_id: str) -> int:
    return math.floor(network.assets[asset_id].backup_hours
                      * hazard_profile(scenario.hazard).backup_factor)


def intervention_delay(scenario: Scenario, kind: str) -> int:
    profile = hazard_profile(scenario.hazard)
    extra = {"backup": profile.delivery_delay, "alternate": profile.alternate_delay}
    return INTERVENTION_DELAY_HOURS[kind] + extra.get(kind, 0)


def check_feasibility(network: Network, scenario: Scenario, iv: Intervention) -> str | None:
    """Return a rejection reason, or None when the intervention can be applied."""
    if iv.kind == "repair":
        if iv.target not in damaged_assets(scenario):
            return "asset is not damaged in this scenario"
        default = default_repair_hour(network, scenario, iv.target)
        if iv.hours <= 0 or iv.hours >= default:
            return f"not faster than the default repair ({default}h)"
        return None
    if iv.kind == "backup":
        asset = network.assets.get(iv.target)
        if asset is None:
            return "unknown asset"
        if asset.type == "substation":
            return "temporary generators cannot replace a substation"
        if not 1 <= iv.hours <= 48:
            return "backup duration must be 1-48 hours"
        return None
    if iv.kind == "alternate":
        edge = network.edges.get(iv.target)
        if edge is None or edge.active:
            return "no inactive alternate connection with this id"
        if edge.supplier in damaged_assets(scenario):
            return "alternate supplier is itself damaged"
        supplier = network.assets[edge.supplier]
        load = sum(e.load for e in network.edges.values()
                   if e.supplier == edge.supplier and e.active) + edge.load
        capacity = supplier.capacity * hazard_profile(scenario.hazard).capacity_factor
        if load > capacity:
            derated = " after heat derating" if capacity < supplier.capacity else ""
            return (f"{supplier.name} would be overloaded "
                    f"({load:g} > capacity {capacity:g}{derated})")
        return None
    return f"unknown intervention kind '{iv.kind}'"


def simulate(network: Network, scenario: Scenario,
             interventions: tuple[Intervention, ...] = ()) -> SimulationResult:
    assets = network.assets
    result = SimulationResult()
    status = {a: OPERATIONAL for a in assets}
    backup_left = {a: initial_backup_hours(network, scenario, a) for a in assets}
    repair_due: dict[str, int] = {}
    active = {e.id: e.active for e in network.edges.values()}
    crew_delay = hazard_profile(scenario.hazard).crew_delay

    basis = {"hazard": "reported", "field": "reported", "intervention": "planned",
             "state": "predicted"}

    def log(t, asset, old, new, cause, rule, kind="state"):
        result.ledger.append({"id": len(result.ledger) + 1, "t": t, "type": kind,
                              "basis": basis[kind],
                              "asset": asset, "asset_name": assets[asset].name,
                              "from": old, "to": new, "cause": cause, "rule": rule})

    for aid in scenario.failed_assets:
        status[aid] = FAILED
        repair_due[aid] = default_repair_hour(network, scenario, aid)
        delay = reported_repair_delay(scenario, aid)
        access = f"crews can start at hour {crew_delay}; " if crew_delay else ""
        if delay:
            access += f"field-reported delay +{delay}h; "
        log(0, aid, OPERATIONAL, FAILED, f"{scenario.hazard} ({scenario.severity} severity)",
            f"hazard damage; {access}default repair completes at hour {repair_due[aid]}",
            "hazard")

    late_failures = {r.asset: r for r in scenario.reports
                     if r.kind == "asset_down" and r.asset not in scenario.failed_assets}
    backup_reports = sorted((r for r in scenario.reports if r.kind == "backup_left"),
                            key=lambda r: r.hour)
    delay_reports = [r for r in scenario.reports if r.kind == "repair_delayed"]

    scheduled: list[tuple[int, Intervention]] = []
    expedite_on_failure: dict[str, int] = {}
    for iv in interventions:
        reason = check_feasibility(network, scenario, iv)
        if reason:
            raise ValueError(f"infeasible intervention {iv}: {reason}")
        if iv.kind == "repair":
            if iv.target in late_failures:
                expedite_on_failure[iv.target] = iv.hours  # applied when it fails
            else:
                repair_due[iv.target] = iv.hours
            log(0, iv.target, status[iv.target], status[iv.target],
                "intervention: extra repair crew assigned",
                f"repair now completes at hour {iv.hours}", "intervention")
        else:
            scheduled.append((intervention_delay(scenario, iv.kind), iv))

    def missing_services(aid: str, prev: dict[str, str]) -> list[str]:
        needed: dict[str, bool] = {}
        for e in network.edges.values():
            if e.dependent != aid or not active[e.id]:
                continue
            service = assets[e.supplier].service
            needed[service] = needed.get(service, False) or prev[e.supplier] != FAILED
        return sorted(s for s, ok in needed.items() if not ok)

    def snapshot(t):
        result.timeline.append({"t": t, "states": dict(status),
                                "active_edges": [e for e, on in active.items() if on]})

    snapshot(0)
    unserved: dict[str, float] = {}
    peak_people: dict[str, int] = {}
    facility_hours: dict[str, int] = {}
    facility_first: dict[str, int] = {}
    critical_hours = 0
    recovery_hour = None
    peak_failed = sum(1 for s in status.values() if s == FAILED)

    for t in range(1, scenario.horizon_hours + 1):
        for r in delay_reports:
            if r.hour == t:
                log(t, r.asset, status[r.asset], status[r.asset], "field report: repair delayed",
                    f"repair takes {r.hours}h longer; now completes at hour "
                    f"{repair_due.get(r.asset, default_repair_hour(network, scenario, r.asset))}",
                    "field")
        for aid, r in late_failures.items():
            if r.hour == t and status[aid] != FAILED:
                old = status[aid]
                status[aid] = FAILED
                repair_due[aid] = expedite_on_failure.get(
                    aid, default_repair_hour(network, scenario, aid))
                log(t, aid, old, FAILED, "field report: asset found failed",
                    f"confirmed failure; repair completes at hour {repair_due[aid]}", "field")
        for r in backup_reports:
            if r.hour == t:
                backup_left[r.asset] = r.hours
                log(t, r.asset, status[r.asset], status[r.asset],
                    "field report: backup level checked",
                    f"only {r.hours}h of on-site backup left", "field")
        for when, iv in scheduled:
            if when != t:
                continue
            if iv.kind == "backup":
                backup_left[iv.target] += iv.hours
                log(t, iv.target, status[iv.target], status[iv.target],
                    "intervention: temporary generator delivered",
                    f"+{iv.hours}h backup ({backup_left[iv.target]}h available)", "intervention")
            else:
                active[iv.target] = True
                edge = network.edges[iv.target]
                log(t, edge.dependent, status[edge.dependent], status[edge.dependent],
                    "intervention: alternate supply switched on",
                    f"edge {iv.target} activated within {assets[edge.supplier].name} capacity",
                    "intervention")

        prev = dict(status)
        for aid in assets:
            cur = prev[aid]
            if aid in late_failures and late_failures[aid].hour == t:
                continue  # just reported failed; rules apply from the next hour
            if aid in repair_due:
                if t < repair_due[aid]:
                    continue
                del repair_due[aid]
                cause, restored = "repair completed", True
            else:
                cause, restored = "", False
            missing = missing_services(aid, prev)
            if not missing:
                if cur != OPERATIONAL or restored:
                    status[aid] = OPERATIONAL
                    log(t, aid, cur, OPERATIONAL, cause or "upstream supply restored",
                        "all required services available")
                continue
            need = ", ".join(missing)
            if backup_left[aid] > 0:
                backup_left[aid] -= 1
                status[aid] = ON_BACKUP
                if cur != ON_BACKUP:
                    log(t, aid, cur, ON_BACKUP, f"lost {need} supply",
                        f"running on backup ({backup_left[aid] + 1}h remaining)")
            else:
                status[aid] = FAILED
                if cur != FAILED or restored:
                    why = "backup exhausted" if assets[aid].backup_hours else "no backup"
                    log(t, aid, cur, FAILED, f"lost {need} supply", f"{why}; service stops")

        snapshot(t)
        failed_now = 0
        people_now: dict[str, int] = {}
        for aid, s in status.items():
            if s != FAILED:
                continue
            failed_now += 1
            asset = assets[aid]
            unserved[asset.service] = unserved.get(asset.service, 0) + asset.people_served
            people_now[asset.service] = people_now.get(asset.service, 0) + asset.people_served
            if asset.type in CRITICAL_TYPES:
                critical_hours += 1
                facility_hours[aid] = facility_hours.get(aid, 0) + 1
                facility_first.setdefault(aid, t)
        for service, people in people_now.items():
            peak_people[service] = max(peak_people.get(service, 0), people)
        peak_failed = max(peak_failed, failed_now)
        if recovery_hour is None and all(s == OPERATIONAL for s in status.values()):
            recovery_hour = t

    result.impact = {
        "unserved_person_hours_by_service": {k: v for k, v in sorted(unserved.items()) if v},
        "total_unserved_service_person_hours": sum(unserved.values()),
        "critical_facility_outage_hours": critical_hours,
        "peak_assets_failed": peak_failed,
        "recovery_hour": recovery_hour,
        "recovered_within_horizon": recovery_hour is not None,
        "peak_people_without_service_by_service": {k: v for k, v in sorted(peak_people.items())
                                                   if v},
        "critical_facilities_down": [
            {"id": aid, "name": assets[aid].name, "type": assets[aid].type,
             "first_failed_hour": facility_first[aid], "outage_hours": facility_hours[aid]}
            for aid in sorted(facility_hours, key=lambda a: (facility_first[a], a))],
        "hazard_effect": hazard_profile(scenario.hazard).description,
        "affected_assets": affected_assets(network, scenario, result.timeline),
    }
    return result


def affected_assets(network: Network, scenario: Scenario, timeline: list[dict]) -> list[dict]:
    """Every asset that leaves normal operation: when it goes on backup, fails and recovers."""
    rows = []
    for aid, asset in network.assets.items():
        states = [snap["states"][aid] for snap in timeline]
        if all(s == OPERATIONAL for s in states):
            continue
        on_backup = next((t for t, s in enumerate(states) if s == ON_BACKUP), None)
        failed = next((t for t, s in enumerate(states) if s == FAILED), None)
        last_bad = max(t for t, s in enumerate(states) if s != OPERATIONAL)
        restored = last_bad + 1 if last_bad + 1 < len(states) else None
        rows.append({"id": aid, "name": asset.name, "type": asset.type, "zone": asset.zone,
                     "service": asset.service, "critical": asset.type in CRITICAL_TYPES,
                     "backup_hours": initial_backup_hours(network, scenario, aid),
                     "on_backup_from": on_backup, "fails_at": failed, "restored_at": restored})
    return sorted(rows, key=lambda r: (min(h for h in (r["on_backup_from"], r["fails_at"])
                                          if h is not None), r["id"]))


def candidate_interventions(network: Network, scenario: Scenario,
                            baseline: SimulationResult) -> list[Intervention]:
    affected = {e["asset"] for e in baseline.ledger if e["to"] != OPERATIONAL}
    options = [Intervention("repair", a, expedited_repair_hour(network, scenario, a))
               for a in damaged_assets(scenario)]
    options += [Intervention("backup", a, 12) for a in sorted(affected)
                if network.assets[a].type in CRITICAL_TYPES]
    options += [Intervention("alternate", e.id) for e in network.edges.values() if not e.active]
    return options


def rank_options(network: Network, baseline: SimulationResult, options: list[dict]) -> list[dict]:
    """Transparent weighted score; the no-action baseline competes on equal terms."""
    base_unserved = baseline.impact["total_unserved_service_person_hours"]
    base_critical = baseline.impact["critical_facility_outage_hours"]
    rows = [{"label": "No action (baseline)", "kind": "baseline", "cost": 0,
             "feasible": True, "impact": baseline.impact}] + options
    max_cost = max((r["cost"] for r in rows if r["feasible"]), default=0) or 1
    for row in rows:
        if not row["feasible"]:
            row["score"] = None
            continue
        impact = row["impact"]
        avoided_unserved = ((base_unserved - impact["total_unserved_service_person_hours"])
                            / base_unserved if base_unserved else 0)
        avoided_critical = ((base_critical - impact["critical_facility_outage_hours"])
                            / base_critical if base_critical else 0)
        row["avoided_unserved_pct"] = round(100 * avoided_unserved, 1)
        row["avoided_critical_pct"] = round(100 * avoided_critical, 1)
        row["score"] = round(SCORE_WEIGHTS["unserved"] * avoided_unserved
                             + SCORE_WEIGHTS["critical"] * avoided_critical
                             + SCORE_WEIGHTS["cost"] * (1 - row["cost"] / max_cost), 1)
    feasible = sorted((r for r in rows if r["feasible"]), key=lambda r: -r["score"])
    for i, row in enumerate(feasible, 1):
        row["rank"] = i
    return feasible + [r for r in rows if not r["feasible"]]


def explain(network: Network, scenario: Scenario, baseline: SimulationResult,
            ranked: list[dict]) -> str:
    """Template explanation citing ledger event ids; no generated facts."""
    ledger = baseline.ledger
    hazard = [e for e in ledger if e["type"] == "hazard"]
    lines = [f"Hour 0: {', '.join(e['asset_name'] for e in hazard)} failed due to "
             f"{scenario.hazard} (events {', '.join(str(e['id']) for e in hazard)}). "
             f"Hazard assumption: {hazard_profile(scenario.hazard).description}"]
    for e in [e for e in ledger if e["type"] == "state" and e["to"] == FAILED][:4]:
        lines.append(f"Hour {e['t']}: {e['asset_name']} failed - {e['cause']}, "
                     f"{e['rule']} (event {e['id']}).")
    backups = [e for e in ledger if e["to"] == ON_BACKUP]
    if backups:
        lines.append(f"{len(backups)} asset(s) ran on backup power before either recovering "
                     f"or failing (events {', '.join(str(e['id']) for e in backups)}).")
    imp = baseline.impact
    recovery = (f"full recovery at hour {imp['recovery_hour']}" if imp["recovered_within_horizon"]
                else f"no full recovery within the {scenario.horizon_hours}h horizon")
    lines.append(f"Baseline impact: {imp['total_unserved_service_person_hours']:,} unserved "
                 f"service person-hours, {imp['critical_facility_outage_hours']} critical-facility "
                 f"outage hours, {recovery}.")
    best = ranked[0]
    if best["kind"] == "baseline":
        lines.append("No tested intervention improved on the baseline under the scoring weights.")
    else:
        lines.append(f"Best option: {best['label']} - avoids {best['avoided_unserved_pct']}% of "
                     f"unserved service-hours and {best['avoided_critical_pct']}% of critical "
                     f"outage hours at cost {best['cost']:,} (score {best['score']}).")
    lines.append("Model-based estimate on synthetic data; planner review required.")
    return " ".join(lines)
