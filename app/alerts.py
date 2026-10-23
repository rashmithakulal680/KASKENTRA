"""Citizen-warning drafts generated from the simulated timeline.

Drafts are grouped by zone, written in plain language, marked as estimates, and must be
approved by an authorised person. KASKENTRA never sends a warning by itself.
"""

from app.simulator import FAILED, Network, Scenario, SimulationResult, damaged_assets

SERVICE_WORDS = {"power": "power cut", "water": "water supply interruption"}
ADVICE = {
    "power cut": "Charge phones and keep torches ready.",
    "water supply interruption": "Store drinking water now.",
    "mobile network outage": "Save emergency numbers and keep calls short.",
    "emergency shelter unavailable": "Follow official instructions for alternative shelters.",
    "limited hospital services": "For emergencies call 112 and check which hospital is open.",
}


def outage_window(result: SimulationResult, asset_id: str) -> tuple[int, int | None] | None:
    """First hour the asset is failed and the hour it works again (None = not within horizon)."""
    states = [snap["states"][asset_id] for snap in result.timeline]
    start = next((t for t, s in enumerate(states) if s == FAILED and t > 0), None)
    if start is None:
        return None
    end = next((t for t in range(start, len(states)) if states[t] != FAILED), None)
    return start, end


def service_losses(network: Network, result: SimulationResult, asset_id: str,
                   window: tuple[int, int | None]) -> dict[str, int]:
    """Service -> first hour (within the outage) when every supplier of it was failed.

    Read from the hour-by-hour states, so a feeder that first loses power and an hour
    later also loses water is reported with both.
    """
    start, end = window
    stop = end if end is not None else len(result.timeline)
    first: dict[str, int] = {}
    for t in range(start, stop):
        snap = result.timeline[t]
        suppliers: dict[str, list[str]] = {}
        for e in network.edges.values():
            if e.dependent == asset_id and e.id in snap["active_edges"]:
                suppliers.setdefault(network.assets[e.supplier].service, []).append(e.supplier)
        for service, ids in suppliers.items():
            if service not in first and all(snap["states"][i] == FAILED for i in ids):
                first[service] = t
    return first


def problems(network, asset, result, window) -> list[tuple[str, int]]:
    """(problem, first hour) pairs for one asset during its outage."""
    if asset.type == "telecom":
        return [("mobile network outage", window[0])]
    if asset.type == "shelter":
        return [("emergency shelter unavailable", window[0])]
    if asset.type == "hospital":
        return [("limited hospital services", window[0])]
    lost = service_losses(network, result, asset.id, window)
    found = [(SERVICE_WORDS[s], max(h, window[0])) for s, h in lost.items() if s in SERVICE_WORDS]
    return found or [("power cut", window[0])]


def draft_warnings(network: Network, scenario: Scenario, result: SimulationResult) -> list[dict]:
    """One draft per zone; one sentence per problem, with the places and estimated hours."""
    zones: dict[str, dict[tuple, list[str]]] = {}
    for aid, asset in network.assets.items():
        if (asset.type not in ("residential", "telecom", "shelter", "hospital")
                or aid in damaged_assets(scenario)):
            continue
        window = outage_window(result, aid)
        if window is None:
            continue
        for problem, start in problems(network, asset, result, window):
            zones.setdefault(asset.zone or "the affected area", {}).setdefault(
                (start, window[1], problem), []).append(asset.name)
    cause = " and ".join(
        [f"{scenario.hazard} at {network.assets[a].name}" for a in scenario.failed_assets]
        + [f"{network.assets[r.asset].name} reported failed at hour {r.hour}"
           for r in scenario.reports if r.kind == "asset_down"])
    drafts = []
    for zone, groups in zones.items():
        sentences, advice = [], []
        for (start, end, problem), places in sorted(groups.items(),
                                                    key=lambda g: (g[0][0], g[0][2])):
            until = (f"until about hour {end}" if end is not None
                     else f"possibly beyond hour {scenario.horizon_hours}")
            sentences.append(f"Possible {problem} ({', '.join(sorted(places))}) "
                             f"from about hour {start} {until}.")
            advice.append(ADVICE[problem])
        message = (f"{zone}: {' '.join(sentences)} Cause: {cause}. "
                   f"Hours are counted from the start of the incident and are estimates. "
                   f"{' '.join(dict.fromkeys(advice))}")
        first = min(k[0] for k in groups)
        drafts.append({"zone": zone, "message": message, "first_hour": first})
    return sorted(drafts, key=lambda d: (d["first_hour"], d["zone"]))
