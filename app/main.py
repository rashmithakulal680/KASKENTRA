from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app import store
from app.alerts import draft_warnings
from app.data import build_network
from app.planner import Resources, action_timing, plan_response
from app.simulator import (HAZARDS, FieldReport, Intervention, Scenario, candidate_interventions,
                           check_feasibility, default_repair_hour, explain, rank_options,
                           simulate)

NETWORK = build_network()
STATIC = Path(__file__).resolve().parent.parent / "static"

app = FastAPI(title="KASKENTRA", version="0.4.0")
app.mount("/static", StaticFiles(directory=STATIC), name="static")


class ReportModel(BaseModel):
    kind: Literal["repair_delayed", "asset_down", "backup_left"]
    asset: str
    hour: int = Field(ge=1, le=168)
    hours: int = Field(0, ge=0, le=168)


class ScenarioRequest(BaseModel):
    hazard: str = Field("flood", min_length=1, max_length=40)
    severity: Literal["low", "medium", "high"] = "medium"
    failed_assets: list[str] = Field(min_length=1, max_length=10)
    horizon_hours: int = Field(48, ge=1, le=168)
    reports: list[ReportModel] = Field(default_factory=list, max_length=20)


class ActionModel(BaseModel):
    kind: Literal["repair", "backup", "alternate"]
    target: str
    hours: int = Field(0, ge=0, le=168)


class PlanRequest(ScenarioRequest):
    crews: int = Field(2, ge=0, le=10)
    generators: int = Field(2, ge=0, le=10)
    budget: int = Field(60000, ge=0, le=10_000_000)
    previous_plan: list[ActionModel] = Field(default_factory=list, max_length=10)


def to_scenario(req: ScenarioRequest) -> Scenario:
    unknown = [a for a in req.failed_assets if a not in NETWORK.assets]
    unknown += [r.asset for r in req.reports if r.asset not in NETWORK.assets]
    if unknown:
        raise HTTPException(400, f"unknown asset ids: {unknown}")
    failed = tuple(dict.fromkeys(req.failed_assets))
    reports = tuple(FieldReport(r.kind, r.asset, r.hour, r.hours) for r in req.reports)
    down = [r.asset for r in reports if r.kind == "asset_down"]
    for r in reports:
        name = NETWORK.assets[r.asset].name
        if r.hour > req.horizon_hours:
            raise HTTPException(400, f"report for {name} is after the {req.horizon_hours}h horizon")
        if r.kind == "asset_down" and (r.asset in failed or down.count(r.asset) > 1):
            raise HTTPException(400, f"{name} is already reported as failed")
        if r.kind == "repair_delayed":
            if r.asset not in failed and r.asset not in down:
                raise HTTPException(400, f"{name} is not damaged, so its repair cannot be delayed")
            if r.hours < 1:
                raise HTTPException(400, "a repair delay must be at least 1 hour")
            others = tuple(o for o in reports if o is not r and not (
                o.kind == "repair_delayed" and o.asset == r.asset))
            planned = default_repair_hour(NETWORK, Scenario(req.hazard, req.severity, failed,
                                                            req.horizon_hours, others), r.asset)
            if r.hour >= planned:
                raise HTTPException(400, f"{name} is already repaired by hour {planned}; "
                                         f"a delay reported at hour {r.hour} comes too late")
    return Scenario(req.hazard, req.severity, failed, req.horizon_hours, reports)


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/network")
def network():
    return {"assets": [vars(a) for a in NETWORK.assets.values()],
            "edges": [vars(e) for e in NETWORK.edges.values()],
            "data_note": "Illustrative demo dataset (data/assets.csv, data/dependencies.csv); "
                         "locations are example points, not real utility assets."}


@app.get("/api/hazards")
def hazards():
    """Plain-language assumption behind each hazard type."""
    return {name: profile.description for name, profile in HAZARDS.items()}


@app.post("/api/simulate")
def run_simulation(req: ScenarioRequest):
    result = simulate(NETWORK, to_scenario(req))
    return {"ledger": result.ledger, "timeline": result.timeline, "impact": result.impact}


@app.post("/api/compare")
def compare(req: ScenarioRequest):
    scenario = to_scenario(req)
    baseline = simulate(NETWORK, scenario)
    options = []
    for iv in candidate_interventions(NETWORK, scenario, baseline):
        row = {"label": iv.label(NETWORK), "kind": iv.kind, "target": iv.target,
               "hours": iv.hours, "cost": iv.cost, "feasible": True,
               "timing": action_timing(NETWORK, scenario, iv)}
        reason = check_feasibility(NETWORK, scenario, iv)
        if reason:
            row.update(feasible=False, reason=reason)
        else:
            row["impact"] = simulate(NETWORK, scenario, (iv,)).impact
        options.append(row)
    ranked = rank_options(NETWORK, baseline, options)
    return {"baseline_impact": baseline.impact, "options": ranked,
            "explanation": explain(NETWORK, scenario, baseline, ranked)}


@app.post("/api/plan")
def plan(req: PlanRequest):
    """Best combination of actions within the crews, generators and budget available."""
    scenario = to_scenario(req)
    previous = []
    for a in req.previous_plan:
        known = NETWORK.edges if a.kind == "alternate" else NETWORK.assets
        if a.target not in known:
            raise HTTPException(400, f"unknown target in previous plan: {a.target}")
        previous.append(Intervention(a.kind, a.target, a.hours))
    return plan_response(NETWORK, scenario, Resources(req.crews, req.generators, req.budget),
                         tuple(previous))


class DecisionModel(BaseModel):
    decision: Literal["approve", "reject"]
    decided_by: str = Field(min_length=2, max_length=60)


@app.post("/api/reports")
def add_report(req: ScenarioRequest):
    """Validate the newest field report against the scenario, then log it in the database."""
    if not req.reports:
        raise HTTPException(400, "no field report to add")
    to_scenario(req)  # raises 400 if the new report does not fit the scenario
    r = req.reports[-1]
    return store.add_field_report(r.kind, r.asset, r.hour, r.hours)


@app.get("/api/reports")
def list_reports():
    return store.list_field_reports()


@app.post("/api/alerts/draft")
def draft_alerts(req: ScenarioRequest):
    """Create citizen-warning drafts for each affected zone; they wait for approval."""
    scenario = to_scenario(req)
    drafts = draft_warnings(NETWORK, scenario, simulate(NETWORK, scenario))
    return [store.save_alert(d["zone"], d["message"], req.model_dump()) for d in drafts]


@app.get("/api/alerts")
def list_alerts():
    return store.list_alerts()


@app.post("/api/alerts/{alert_id}/decision")
def decide(alert_id: int, req: DecisionModel):
    alert = store.get_alert(alert_id)
    if alert is None:
        raise HTTPException(404, "warning draft not found")
    decided = store.decide_alert(alert_id, req.decision, req.decided_by.strip())
    if decided is None:
        raise HTTPException(409, f"this draft is already {alert['status']}")
    return decided
