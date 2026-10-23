"""CSV dataset, SQLite records and citizen-warning drafts (KASKENTRA update 3)."""
import csv

import pytest
from fastapi.testclient import TestClient

from app import store
from app.alerts import draft_warnings
from app.data import DATA_DIR, build_network, load_assets
from app.main import app
from app.simulator import Scenario, simulate

NET = build_network()
client = TestClient(app)
BODY = {"hazard": "flood", "severity": "medium", "failed_assets": ["S1"]}


def test_dataset_loads_from_csv_with_locations():
    rows = list(csv.DictReader(open(DATA_DIR / "assets.csv", encoding="utf-8")))
    assert len(rows) == len(NET.assets) == 20
    for a in NET.assets.values():
        assert 12.8 < a.lat < 13.0 and 74.8 < a.lon < 75.0 and a.zone.startswith("Zone ")
    assert len(NET.edges) == 31 and sum(1 for e in NET.edges.values() if not e.active) == 3


def test_bad_dataset_is_rejected(tmp_path):
    bad = tmp_path / "assets.csv"
    text = (DATA_DIR / "assets.csv").read_text(encoding="utf-8").replace(",pump,", ",spaceship,", 1)
    bad.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match="unknown asset type"):
        load_assets(bad)


def test_drafts_list_each_problem_with_hours():
    sc = Scenario("flood", "medium", ("S1",), 48)
    drafts = draft_warnings(NET, sc, simulate(NET, sc))
    assert [d["zone"] for d in drafts] == ["Zone A"]
    msg = drafts[0]["message"]
    assert "power cut (Northgate Feeder, Riverside Feeder) from about hour 1 until about hour 42" in msg
    assert "water supply interruption (Northgate Feeder, Riverside Feeder)" in msg
    assert "mobile network outage (North Telecom Tower) from about hour 5 until about hour 41" in msg
    assert "estimates" in msg and "Cause: flood at North Substation." in msg


def test_drafts_only_mention_services_actually_lost():
    sc = Scenario("flood", "medium", ("S3",), 48)  # East Substation feeds the treatment plant
    drafts = {d["zone"]: d["message"] for d in draft_warnings(NET, sc, simulate(NET, sc))}
    assert "power cut" not in drafts["Zone B"]  # Zone B keeps power, loses water
    assert "water supply interruption (Harbor Feeder, Southpark Feeder)" in drafts["Zone B"]
    assert "water supply interruption" not in drafts["Zone C"]  # Zone C feeders have no pump


def test_alert_approval_flow_is_stored():
    drafts = client.post("/api/alerts/draft", json=BODY).json()
    assert len(drafts) == 1 and drafts[0]["status"] == "pending approval"
    aid = drafts[0]["id"]
    ok = client.post(f"/api/alerts/{aid}/decision", json={"decision": "approve", "decided_by": "Control room"})
    assert ok.status_code == 200 and ok.json()["status"] == "approved"
    again = client.post(f"/api/alerts/{aid}/decision", json={"decision": "reject", "decided_by": "Someone"})
    assert again.status_code == 409
    assert client.post("/api/alerts/999/decision", json={"decision": "approve", "decided_by": "Ab"}).status_code == 404
    listed = client.get("/api/alerts").json()
    assert listed[0]["id"] == aid and listed[0]["decided_by"] == "Control room"
    assert listed[0]["scenario"]["failed_assets"] == ["S1"]


def test_field_reports_are_validated_then_logged():
    good = dict(BODY, reports=[{"kind": "asset_down", "asset": "S3", "hour": 3}])
    r = client.post("/api/reports", json=good)
    assert r.status_code == 200 and r.json()["asset"] == "S3"
    bad = dict(BODY, reports=[{"kind": "repair_delayed", "asset": "S2", "hour": 3, "hours": 2}])
    assert client.post("/api/reports", json=bad).status_code == 400
    assert client.post("/api/reports", json=BODY).status_code == 400
    assert [x["asset"] for x in client.get("/api/reports").json()] == ["S3"]


def test_database_file_is_the_test_file(tmp_path):
    store.add_field_report("asset_down", "S3", 3, 0)
    assert store.DB_PATH.endswith("test.db")


def test_affected_assets_table():
    sc = Scenario("flood", "medium", ("S1",), 48)
    rows = {r["id"]: r for r in simulate(NET, sc).impact["affected_assets"]}
    assert set(rows) == {"S1", "P1", "T1", "H1", "SH1", "R1", "R2"}
    assert rows["H1"] == {**rows["H1"], "backup_hours": 8, "on_backup_from": 1, "fails_at": 9,
                          "restored_at": 42, "critical": True}
    assert rows["S1"]["fails_at"] == 0 and rows["S1"]["restored_at"] == 40
    assert rows["T1"]["fails_at"] == 5 and rows["T1"]["restored_at"] == 41
    fire = simulate(NET, Scenario("fire", "medium", ("S1",), 48)).impact["affected_assets"]
    assert next(r for r in fire if r["id"] == "R1")["restored_at"] is None  # beyond horizon


def test_compare_rows_include_timing():
    rows = client.post("/api/compare", json=BODY).json()["options"]
    repair = next(r for r in rows if r.get("kind") == "repair")
    assert repair["timing"] == "crew starts at hour 4, repair done by hour 26"


def test_draft_cause_includes_reported_failures():
    from app.simulator import FieldReport
    sc = Scenario("flood", "medium", ("S1",), 48, (FieldReport("asset_down", "S3", 3),))
    drafts = draft_warnings(NET, sc, simulate(NET, sc))
    for d in drafts:
        assert "Cause: flood at North Substation and East Substation reported failed at hour 3." in d["message"]
