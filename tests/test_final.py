"""Final version: independent score check, plan-vs-no-action data, AI briefing guard."""
import json
import os
import urllib.error
from itertools import combinations

import networkx as nx
import pytest
from fastapi.testclient import TestClient

from app import summary
from app.data import build_network
from app.main import NETWORK, app
from app.planner import MAX_ACTIONS, Resources, fits, plan_response
from app.planner import score as planner_score
from app.simulator import (HAZARDS, Asset, Edge, Intervention, Network, Scenario,
                           candidate_interventions, check_feasibility, rank_options, simulate)
from app.verify import indian, recount, verify_plan

client = TestClient(app)


def konaje_network() -> Network:
    """Konaje and the three hospitals it feeds in the real Mangaluru dataset, with the
    same assumed values (substation repair 24 h, hospital backup 8 h, no population)."""
    def asset(aid, name, typ, service, backup, repair):
        return Asset(aid, name, typ, service, 0, 0, backup, repair, 0, 0, 12.8, 74.9, "Area of Konaje")
    assets = {"SUB": asset("SUB", "Konaje", "substation", "power", 0, 24),
              "H1": asset("H1", "A B Shetty", "hospital", "healthcare", 8, 24),
              "H2": asset("H2", "K S Hegde Hospital", "hospital", "healthcare", 8, 24),
              "H3": asset("H3", "K S Hegde Medical College", "hospital", "healthcare", 8, 24)}
    edges = {f"SUB-{h}": Edge(f"SUB-{h}", "SUB", h, 1) for h in ("H1", "H2", "H3")}
    graph = nx.DiGraph(); graph.add_nodes_from(assets)
    graph.add_edges_from((e.supplier, e.dependent) for e in edges.values())
    return Network(assets, edges, graph)


FLOOD = Scenario("flood", "medium", ("SUB",), 48)


# ---------- the Konaje demo, worked out by hand ----------

def test_konaje_no_action_matches_hand_calculation():
    # repair = 0 + 4 h flood delay + 24 h x 1.5 = hour 40; hospitals: backup hours 1-8,
    # no service hours 9-40 (32 h each), back at hour 41.
    # asset-hours = Konaje hours 1-39 (39) + 3 x 32 = 135; hospital hours = 96.
    impact = simulate(konaje_network(), FLOOD).impact
    assert impact["failed_asset_hours"] == 39 + 3 * 32 == 135
    assert impact["critical_facility_outage_hours"] == 96
    assert impact["recovery_hour"] == 41


def test_konaje_plan_matches_hand_calculation():
    net = konaje_network()
    plan = plan_response(net, FLOOD, Resources(crews=2, generators=2, budget=60000))
    best = plan["best"]
    assert [a["label"] for a in best["actions"]] == [
        "Send an extra crew to Konaje (repair done by hour 26)",
        "Deploy 12h backup power to A B Shetty"]
    # 4 options -> 16 groups; 2 need 3 generators, 3 cost 76,000 > 60,000 -> 11
    assert plan["plans_checked"] == 11
    assert best["cost"] == 40000 + 1500 * 12 == 58000
    # Konaje back at 26 (down 1-25 = 25 h); two hospitals down 9-26 (18 h each);
    # A B Shetty has 8 + 12 h of backup, down 21-26 (6 h): 25 + 18 + 18 + 6 = 67
    assert best["impact"]["failed_asset_hours"] == 67
    assert best["impact"]["critical_facility_outage_hours"] == 42
    assert best["impact"]["recovery_hour"] == 27
    # 60 x 68/135 + 30 x 54/96 + 10 x (1 - 58000/60000) = 30.22 + 16.88 + 0.33 = 47.43
    assert best["score"] == 47.4
    v = plan["verification"]
    assert v["all_match"] and v["score"] == 47.4
    assert [p["points"] for p in v["parts"]] == [30.22, 16.88, 0.33]


def test_konaje_single_option_scores_match_hand_calculation():
    net = konaje_network()
    base = simulate(net, FLOOD)
    rows = [{"label": iv.label(net), "kind": iv.kind, "cost": iv.cost, "feasible": True,
             "impact": simulate(net, FLOOD, (iv,)).impact}
            for iv in candidate_interventions(net, FLOOD, base)]
    scores = {row["label"]: row["score"] for row in rank_options(net, base, rows)}
    # crew: 60 x 56/135 + 30 x 42/96 + 10 x 0 = 37.98; generator: 60 x 12/135 + 30 x 12/96
    # + 10 x (1 - 18000/40000) = 14.58; no action: 10
    assert scores["Send an extra crew to Konaje (repair done by hour 26)"] == 38.0
    assert scores["Deploy 12h backup power to A B Shetty"] == 14.6
    assert scores["No action (baseline)"] == 10.0
    # with the step-4 budget the cost part is the planner's rule (share of budget left):
    # crew: 24.89 + 13.13 + 10 x (1 - 40000/60000) = 41.35; generator: 5.33 + 3.75 + 7 = 16.08
    with_budget = {row["label"]: row["score"] for row in rank_options(net, base, rows, 60000)}
    assert with_budget["Send an extra crew to Konaje (repair done by hour 26)"] == 41.3
    assert with_budget["Deploy 12h backup power to A B Shetty"] == 16.1
    assert with_budget["No action (baseline)"] == 10.0


def test_compare_api_uses_budget_cost_rule():
    body = demo_request(budget=60000)
    r = client.post("/api/compare", json=body).json()
    assert r["weights"] == {"unserved": 60, "critical": 30, "cost": 10}
    assert r["budget"] == 60000
    plan = client.post("/api/plan", json=dict(body, crews=1, generators=0)).json()
    # a one-action plan and the same single option must score identically
    if len(plan["best"]["actions"]) == 1:
        a = plan["best"]["actions"][0]
        row = next(o for o in r["options"] if o.get("kind") == a["kind"] and o.get("target") == a["target"])
        assert row["score"] == plan["best"]["score"]


# ---------- independent recheck over many scenarios ----------

def all_scenarios(net):
    subs = [a for a, x in net.assets.items() if x.type == "substation"]
    for sub in subs:
        for hazard in HAZARDS:
            for severity in ("low", "medium", "high"):
                yield Scenario(hazard, severity, (sub,), 48)


def test_every_plan_combination_rechecks_and_best_is_really_best():
    net = build_network()
    checked = 0
    for sc in all_scenarios(net):
        res = Resources(2, 2, 60000)
        plan = plan_response(net, sc, res)
        assert plan["verification"]["all_match"], sc
        base = simulate(net, sc)
        feasible = [iv for iv in candidate_interventions(net, sc, base)
                    if check_feasibility(net, sc, iv) is None]
        best_seen = None
        for size in range(0, min(MAX_ACTIONS, len(feasible)) + 1):
            for combo in combinations(feasible, size):
                if not fits(combo, res):
                    continue
                result = simulate(net, sc, combo)
                cost = sum(iv.cost for iv in combo)
                engine = {"impact": result.impact, "cost": cost,
                          **planner_score(base.impact, result.impact, cost, res.budget)}
                v = verify_plan(net, base.timeline, result.timeline, combo, res.budget, engine)
                assert v["all_match"], (sc, combo, v["checks"])
                best_seen = v["score"] if best_seen is None else max(best_seen, v["score"])
                checked += 1
        assert plan["best"]["score"] == best_seen, sc
    assert checked > 100


def test_recount_catches_a_wrong_number():
    net = konaje_network()
    plan = plan_response(net, FLOOD, Resources())
    tampered = dict(plan["best"], score=plan["best"]["score"] + 1)
    actions = (Intervention("repair", "SUB", 26), Intervention("backup", "H1", 12))
    v = verify_plan(net, plan["baseline_timeline"], plan["best_timeline"], actions, 60000, tampered)
    assert not v["all_match"]
    assert [c["what"] for c in v["checks"] if not c["match"]] == ["Score"]


def test_recount_reads_only_the_timeline():
    net = konaje_network()
    timeline = simulate(net, FLOOD).timeline
    counts = recount(net, timeline)
    assert counts["failed_asset_hours"] == 135
    assert counts["facility_hours"] == {"H1": 32, "H2": 32, "H3": 32}


def test_indian_grouping():
    assert [indian(n) for n in (0, 999, 58000, 135000, 1283000, 12345678)] == [
        "0", "999", "58,000", "1,35,000", "12,83,000", "1,23,45,678"]


# ---------- API: data for the plan-vs-no-action visuals ----------

def demo_request(**extra):
    sub = next(a for a, x in NETWORK.assets.items() if x.type == "substation")
    return {"hazard": "flood", "severity": "medium", "failed_assets": [sub], "horizon_hours": 48,
            **extra}


def test_plan_api_returns_both_timelines_and_verification():
    r = client.post("/api/plan", json=demo_request()).json()
    assert len(r["baseline_timeline"]) == len(r["best_timeline"]) == 49
    assert r["verification"]["all_match"]
    replay = simulate(NETWORK, Scenario("flood", "medium", tuple(demo_request()["failed_assets"]), 48))
    assert r["baseline_timeline"] == replay.timeline


# ---------- AI briefing: numbers come from code, AI only words them ----------

def konaje_facts():
    net = konaje_network()
    return summary.facts_from_plan(net, FLOOD, plan_response(net, FLOOD, Resources()))


def test_no_key_uses_template():
    out = summary.summarise(konaje_facts())
    assert out["source"] == "template" and "GROQ_API_KEY" in out["note"]
    assert "hour 41" in out["text"] and "hour 27" in out["text"] and "₹58,000" in out["text"]
    # a generator for any one of the three hospitals scores the same 47.4: say so openly
    assert konaje_facts()["recommended_plan"]["equally_good_plans"] == [
        "Send an extra crew to Konaje (repair done by hour 26) + Deploy 12h backup power to K S Hegde Hospital",
        "Send an extra crew to Konaje (repair done by hour 26) + Deploy 12h backup power to K S Hegde Medical College"]
    assert "Equally good by the score" in out["text"]


def test_ai_switch_off(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    monkeypatch.setenv("KASKENTRA_AI", "off")
    monkeypatch.setattr(summary, "call_groq", lambda *a: pytest.fail("must not call Groq"))
    assert summary.summarise(konaje_facts())["source"] == "template"


def test_ai_text_with_only_computed_numbers_is_used(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    good = ("A flood stopped Konaje. With no action the three hospitals lose service from hour 9 "
            "and everything is back at hour 41. The plan sends an extra crew and a generator for "
            "₹58,000, so recovery comes at hour 27 and outage drops by 56.2%. These are estimates "
            "for the duty officer to approve.")
    monkeypatch.setattr(summary, "call_groq", lambda facts, key, model: good)
    out = summary.summarise(konaje_facts())
    assert out["source"] == "ai" and out["text"] == good and out["model"] == summary.DEFAULT_MODEL


def test_ai_text_with_invented_number_is_rejected(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    bad = "Recovery comes at hour 27, saving about 60% of outage and 2,000 patients."
    monkeypatch.setattr(summary, "call_groq", lambda *a: bad)
    out = summary.summarise(konaje_facts())
    assert out["source"] == "template"
    assert "2000" in out["note"] and "60" in out["note"]
    assert out["rejected_ai_text"] == bad


@pytest.mark.parametrize("error", [
    urllib.error.HTTPError(summary.GROQ_URL, 429, "Too Many Requests", {}, None),
    urllib.error.URLError("no internet"), TimeoutError()])
def test_groq_errors_fall_back_to_template(monkeypatch, error):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")

    def fail(*a):
        raise error
    monkeypatch.setattr(summary, "call_groq", fail)
    out = summary.summarise(konaje_facts())
    assert out["source"] == "template" and out["text"] == summary.template_summary(konaje_facts())


def test_empty_ai_reply_falls_back(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    monkeypatch.setattr(summary, "call_groq", lambda *a: "")
    assert summary.summarise(konaje_facts())["source"] == "template"


def test_groq_request_shape(monkeypatch):
    """The request sent to Groq: the computed facts only, the key in the header."""
    sent = {}

    class Reply:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return json.dumps({"choices": [{"message": {"content": " ok "}}]}).encode()

    def fake_urlopen(request, timeout):
        sent["url"], sent["timeout"] = request.full_url, timeout
        sent["auth"] = request.get_header("Authorization")
        sent["body"] = json.loads(request.data)
        return Reply()
    monkeypatch.setattr(summary.urllib.request, "urlopen", fake_urlopen)
    facts = konaje_facts()
    assert summary.call_groq(facts, "k123", "openai/gpt-oss-20b") == "ok"
    assert sent["url"] == "https://api.groq.com/openai/v1/chat/completions"
    assert sent["auth"] == "Bearer k123"
    assert sent["body"]["model"] == "openai/gpt-oss-20b"
    assert sent["body"]["reasoning_effort"] == "low"
    assert json.loads(sent["body"]["messages"][1]["content"]) == facts


def test_summary_api_without_key():
    r = client.post("/api/summary", json=demo_request())
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "template" and body["facts"]["score_independently_rechecked"]


def test_env_file_does_not_override_real_environment(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("# comment\nKASKENTRA_TEST_A=from-file\nKASKENTRA_TEST_B='quoted'\n", encoding="utf-8")
    monkeypatch.setenv("KASKENTRA_TEST_A", "real")
    monkeypatch.delenv("KASKENTRA_TEST_B", raising=False)
    summary.load_env_file(env)
    assert os.environ["KASKENTRA_TEST_A"] == "real"
    assert os.environ["KASKENTRA_TEST_B"] == "quoted"
    monkeypatch.delenv("KASKENTRA_TEST_B")
