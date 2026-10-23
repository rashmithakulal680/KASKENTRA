"""Response planning under limits, field reports and re-planning (KASKENTRA update 2)."""
import pytest
from fastapi.testclient import TestClient

from app.data import build_network
from app.main import app
from app.planner import RESOURCE_USE, Resources, plan_response
from app.simulator import (FAILED, ON_BACKUP, OPERATIONAL, FieldReport, Intervention, Scenario,
                           default_repair_hour, expedited_repair_hour, simulate)

NET = build_network()
FLOOD_S1 = Scenario("flood", "medium", ("S1",), 48)
client = TestClient(app)


def labels(plan):
    return [a["label"] for a in plan["actions"]]


# ---------- planning under limits ----------

def test_no_resources_means_no_action():
    r = plan_response(NET, FLOOD_S1, Resources(0, 0, 0))
    assert r["best"]["actions"] == [] and r["plans_checked"] == 1


def test_plans_respect_every_limit():
    for res in (Resources(1, 0, 40000), Resources(1, 1, 60000), Resources(2, 2, 60000),
                Resources(3, 3, 200000)):
        r = plan_response(NET, FLOOD_S1, res)
        for p in [r["best"]] + r["alternatives"]:
            assert p["crews_used"] <= res.crews and p["generators_used"] <= res.generators
            assert p["cost"] <= res.budget
            assert p["cost"] == sum(a["cost"] for a in p["actions"])
            assert p["crews_used"] == sum(1 for a in p["actions"] if RESOURCE_USE[a["kind"]] == "crews")


def test_best_plan_is_at_least_as_good_as_doing_nothing_and_sorted():
    r = plan_response(NET, FLOOD_S1, Resources())
    plans = [r["best"]] + r["alternatives"]
    assert all(a["score"] >= b["score"] for a, b in zip(plans, plans[1:]))
    nothing = plan_response(NET, FLOOD_S1, Resources(0, 0, 0))["best"]
    assert r["best"]["score"] >= nothing["score"]


def test_demo_plan_with_default_resources():
    r = plan_response(NET, FLOOD_S1, Resources(2, 2, 60000))
    assert labels(r["best"]) == [
        "Send an extra crew to North Substation (repair done by hour 26)",
        "Activate alternate supply East Substation -> North Telecom Tower"]
    assert r["best"]["cost"] == 60000 and r["best"]["impact"]["recovery_hour"] == 28


def test_one_crew_and_40k_picks_the_repair():
    r = plan_response(NET, FLOOD_S1, Resources(1, 0, 40000))
    assert labels(r["best"]) == ["Send an extra crew to North Substation (repair done by hour 26)"]


def test_planning_is_deterministic():
    a = plan_response(NET, FLOOD_S1, Resources())
    b = plan_response(NET, FLOOD_S1, Resources())
    assert a == b


# ---------- field reports ----------

def test_repair_delay_report_moves_repair():
    sc = Scenario("flood", "medium", ("S1",), 48, (FieldReport("repair_delayed", "S1", 2, 6),))
    assert default_repair_hour(NET, sc, "S1") == 46
    assert expedited_repair_hour(NET, sc, "S1") == 4 + 6 + 22
    r = simulate(NET, sc)
    assert r.timeline[45]["states"]["S1"] == FAILED and r.timeline[46]["states"]["S1"] == OPERATIONAL
    field = [e for e in r.ledger if e["type"] == "field"]
    assert len(field) == 1 and field[0]["t"] == 2 and field[0]["basis"] == "reported"


def test_asset_down_report_fails_asset_at_reported_hour():
    sc = Scenario("flood", "medium", ("S1",), 48, (FieldReport("asset_down", "S3", 3),))
    r = simulate(NET, sc)
    assert r.timeline[2]["states"]["S3"] == OPERATIONAL
    assert all(r.timeline[t]["states"]["S3"] == FAILED for t in range(3, 43))
    assert r.timeline[43]["states"]["S3"] == OPERATIONAL  # 3 + 4 crew delay + 36 work
    assert r.timeline[4]["states"]["W1"] == ON_BACKUP  # treatment plant loses East Substation power


def test_backup_left_report_shortens_backup():
    sc = Scenario("flood", "medium", ("S1",), 48, (FieldReport("backup_left", "H1", 2, 2),))
    r = simulate(NET, sc)
    assert [r.timeline[t]["states"]["H1"] for t in (2, 3, 4)] == [ON_BACKUP, ON_BACKUP, FAILED]


def test_ledger_separates_reported_planned_and_predicted():
    sc = Scenario("flood", "medium", ("S1",), 48, (FieldReport("asset_down", "T2", 5),))
    r = simulate(NET, sc, (Intervention("backup", "SH1", 12),))
    kinds = {(e["type"], e["basis"]) for e in r.ledger}
    assert ("hazard", "reported") in kinds and ("field", "reported") in kinds
    assert ("intervention", "planned") in kinds and ("state", "predicted") in kinds


def test_extra_crew_for_a_later_failure_does_not_freeze_it_early():
    sc = Scenario("flood", "medium", ("S1",), 48, (FieldReport("asset_down", "T2", 5),))
    fast = expedited_repair_hour(NET, sc, "T2")
    r = simulate(NET, sc, (Intervention("repair", "T2", fast),))
    assert r.timeline[4]["states"]["T2"] == OPERATIONAL
    assert r.timeline[5]["states"]["T2"] == FAILED
    assert r.timeline[fast]["states"]["T2"] == OPERATIONAL


# ---------- re-planning ----------

def test_replanning_when_east_substation_also_fails():
    first = plan_response(NET, FLOOD_S1, Resources())["best"]
    previous = tuple(Intervention(a["kind"], a["target"], a["hours"]) for a in first["actions"])
    sc = Scenario("flood", "medium", ("S1",), 48, (FieldReport("asset_down", "S3", 3),))
    r = plan_response(NET, sc, Resources(), previous)
    assert r["previous_plan"]["feasible"] is False
    assert any("alternate supplier is itself damaged" in p for p in r["previous_plan"]["problems"])
    assert labels(r["best"]) != labels(first)


def test_previous_plan_cannot_beat_a_reported_delay():
    first = plan_response(NET, FLOOD_S1, Resources())["best"]
    previous = tuple(Intervention(a["kind"], a["target"], a["hours"]) for a in first["actions"])
    sc = Scenario("flood", "medium", ("S1",), 48, (FieldReport("repair_delayed", "S1", 2, 6),))
    r = plan_response(NET, sc, Resources(), previous)
    old = r["previous_plan"]
    repair = next(a for a in old["actions"] if a["kind"] == "repair")
    assert repair["hours"] == 32  # recomputed: the delay applies to the extra crew too
    assert old["score"] <= r["best"]["score"]


# ---------- API ----------

def test_api_plan_and_replan():
    body = {"hazard": "flood", "severity": "medium", "failed_assets": ["S1"]}
    first = client.post("/api/plan", json=body).json()
    assert first["best"]["cost"] <= 60000 and first["plans_checked"] > 1
    prev = [{"kind": a["kind"], "target": a["target"], "hours": a["hours"]}
            for a in first["best"]["actions"]]
    body2 = dict(body, reports=[{"kind": "asset_down", "asset": "S3", "hour": 3}],
                 previous_plan=prev)
    second = client.post("/api/plan", json=body2).json()
    assert second["previous_plan"]["feasible"] is False
    sim = client.post("/api/simulate", json=body2).json()
    assert any(e["type"] == "field" for e in sim["ledger"])


@pytest.mark.parametrize("reports,expected", [
    ([{"kind": "asset_down", "asset": "ZZ", "hour": 3}], 400),
    ([{"kind": "asset_down", "asset": "S1", "hour": 3}], 400),          # already failed
    ([{"kind": "asset_down", "asset": "S3", "hour": 99}], 400),         # after horizon
    ([{"kind": "repair_delayed", "asset": "S2", "hour": 3, "hours": 4}], 400),  # not damaged
    ([{"kind": "repair_delayed", "asset": "S1", "hour": 3, "hours": 0}], 400),  # no delay
    ([{"kind": "repair_delayed", "asset": "S1", "hour": 45, "hours": 4}], 400),  # already repaired
    ([{"kind": "asset_down", "asset": "S3", "hour": 0}], 422),          # hour must be >= 1
    ([{"kind": "explode", "asset": "S3", "hour": 3}], 422),
])
def test_api_rejects_bad_reports(reports, expected):
    body = {"failed_assets": ["S1"], "reports": reports}
    assert client.post("/api/simulate", json=body).status_code == expected
    assert client.post("/api/plan", json=body).status_code == expected


def test_api_rejects_bad_plan_inputs():
    assert client.post("/api/plan", json={"failed_assets": ["S1"], "crews": -1}).status_code == 422
    bad_prev = {"failed_assets": ["S1"], "previous_plan": [{"kind": "alternate", "target": "S1"}]}
    assert client.post("/api/plan", json=bad_prev).status_code == 400
