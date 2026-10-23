"""Hazard profiles, realistic repair and readable impact (KASKENTRA foundation fixes)."""
from app.data import build_network
from app.simulator import (FAILED, ON_BACKUP, HAZARDS, Intervention, Scenario,
                           candidate_interventions, check_feasibility, default_repair_hour,
                           expedited_repair_hour, simulate)

NET = build_network()


def repair_hour(result, asset):
    return next(e["t"] for e in result.ledger if e["asset"] == asset and e["to"] != FAILED
                and e["from"] == FAILED)


def test_hazards_change_results():
    hours = {h: repair_hour(simulate(NET, Scenario(h, "medium", ("S1",), 72)), "S1")
             for h in HAZARDS}
    assert hours["equipment fault"] == 36
    assert hours["flood"] == 40      # crews start 4h late
    assert hours["storm"] == 38      # crews start 2h late
    assert hours["fire"] == 2 + 45   # 2h to make safe, repairs 25% slower
    assert hours["heatwave"] == 36   # heat does not slow repairs


def test_heatwave_shortens_backup():
    normal = simulate(NET, Scenario("equipment fault", "medium", ("S1",), 48))
    heat = simulate(NET, Scenario("heatwave", "medium", ("S1",), 48))
    # City Hospital has 8h of backup normally, 6h in a heatwave.
    assert normal.timeline[9]["states"]["H1"] == FAILED and normal.timeline[8]["states"]["H1"] == ON_BACKUP
    assert heat.timeline[7]["states"]["H1"] == FAILED and heat.timeline[6]["states"]["H1"] == ON_BACKUP


def test_heatwave_derates_alternate_capacity():
    # S3 carries 90 units; +20 fits in 120 but not in 96 (80% of 120).
    assert check_feasibility(NET, Scenario("equipment fault", "medium", ("S1",)),
                             Intervention("alternate", "ALT-S3-P1")) is None
    reason = check_feasibility(NET, Scenario("heatwave", "medium", ("S1",)),
                               Intervention("alternate", "ALT-S3-P1"))
    assert "overloaded" in reason and "heat" in reason


def test_flood_delays_generator_delivery():
    sc = Scenario("flood", "high", ("S1",), 48)
    r = simulate(NET, sc, (Intervention("backup", "SH1", 12),))
    delivered = next(e for e in r.ledger if e["type"] == "intervention" and e["asset"] == "SH1")
    assert delivered["t"] == 4  # 2h normal + 2h flood delay


def test_expedited_repair_is_realistic():
    sc = Scenario("flood", "medium", ("S1",), 72)
    assert default_repair_hour(NET, sc, "S1") == 40
    assert expedited_repair_hour(NET, sc, "S1") == 4 + 22  # access delay + 60% of 36h
    repairs = [iv for iv in candidate_interventions(NET, sc, simulate(NET, sc))
               if iv.kind == "repair"]
    assert [iv.hours for iv in repairs] == [26]
    assert check_feasibility(NET, sc, repairs[0]) is None


def test_readable_impact():
    imp = simulate(NET, Scenario("equipment fault", "medium", ("S1",), 48)).impact
    peak = imp["peak_people_without_service_by_service"]
    assert peak["residential"] == 17000 and peak["telecom"] == 15000
    assert peak["healthcare"] == 400 and peak["shelter"] == 800
    down = {f["id"]: f for f in imp["critical_facilities_down"]}
    assert set(down) == {"H1", "SH1"}
    assert down["SH1"]["first_failed_hour"] == 1 and down["H1"]["first_failed_hour"] == 9
    assert imp["critical_facility_outage_hours"] == sum(f["outage_hours"] for f in down.values())
    assert imp["hazard_effect"].startswith("Local equipment fault")
