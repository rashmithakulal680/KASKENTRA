"""Phase 1 must-fix behaviour: strict hazards, approver names, health check."""
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.simulator import HAZARDS, hazard_profile

client = TestClient(app)
BODY = {"hazard": "flood", "severity": "medium", "failed_assets": ["S1"]}


def test_hazard_profile_rejects_unknown_names():
    assert hazard_profile("flood") is HAZARDS["flood"]
    with pytest.raises(ValueError, match="unknown hazard 'tsunami'"):
        hazard_profile("tsunami")


@pytest.mark.parametrize("endpoint", ["/api/simulate", "/api/compare", "/api/plan",
                                      "/api/alerts/draft"])
def test_unknown_hazard_is_400_on_every_scenario_endpoint(endpoint):
    r = client.post(endpoint, json=dict(BODY, hazard="tsunami"))
    assert r.status_code == 400 and "unknown hazard" in r.json()["detail"]


def test_unknown_hazard_is_400_when_adding_a_report():
    body = dict(BODY, hazard="tsunami", reports=[{"kind": "asset_down", "asset": "S3", "hour": 3}])
    assert client.post("/api/reports", json=body).status_code == 400


def test_every_listed_hazard_is_accepted():
    for name in client.get("/api/hazards").json():
        assert client.post("/api/simulate", json=dict(BODY, hazard=name)).status_code == 200


@pytest.mark.parametrize("name,expected", [("   ", 422), (" a ", 422), ("", 422),
                                           ("x" * 61, 422), ("  Duty officer  ", 200)])
def test_approver_name_is_stripped_and_checked(name, expected):
    aid = client.post("/api/alerts/draft", json=BODY).json()[0]["id"]
    r = client.post(f"/api/alerts/{aid}/decision", json={"decision": "approve", "decided_by": name})
    assert r.status_code == expected
    if expected == 200:
        assert r.json()["decided_by"] == "Duty officer"


def test_health():
    r = client.get("/health")
    assert r.status_code == 200 and r.json() == {"status": "ok", "assets": 20}
