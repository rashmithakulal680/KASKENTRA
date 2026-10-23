"""Building a dataset from OpenStreetMap-style rows (tools/build_real_dataset.py).

The rows below are TEST INPUT only, written to a temp folder; no real data is shipped.
"""
import csv
import importlib.util
from pathlib import Path

import pytest

from app.alerts import draft_warnings
from app.data import build_network, data_note
from app.planner import Resources, plan_response
from app.simulator import FAILED, ON_BACKUP, Scenario, simulate

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("brd", ROOT / "tools" / "build_real_dataset.py")
brd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(brd)

ROWS = [  # category, name, lat, lon, osm
    ("substation", "Alpha Substation", 12.900, 74.850, "node/1"),
    ("substation", "", 12.850, 74.900, "way/2"),
    ("hospital", "Test Hospital North", 12.905, 74.852, "way/3"),
    ("hospital", "Test Hospital North", 12.9051, 74.8521, "node/33"),  # duplicate name
    ("hospital", "Test Hospital South", 12.852, 74.898, "way/4"),
    ("water", "Test Pumping Station", 12.903, 74.851, "node/5"),
    ("telecom", "", 12.899, 74.849, "node/6"),
]


def write_osm(folder: Path, rows=ROWS) -> Path:
    src = folder / "osm.csv"
    with open(src, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["category", "name", "lat", "lon", "osm", "voltage", "operator",
                    "substation_type", "man_made"])
        for c, n, lat, lon, osm in rows:
            w.writerow([c, n, lat, lon, osm, "", "", "", ""])
    return src


@pytest.fixture
def real(tmp_path):
    out = tmp_path / "real"
    brd.main(write_osm(tmp_path), out)
    return out


def test_names_and_coordinates_are_copied_not_invented(real):
    net = build_network(real)
    got = {(a.name, a.lat, a.lon) for a in net.assets.values()}
    assert ("Alpha Substation", 12.9, 74.85) in got
    assert ("Test Hospital South", 12.852, 74.898) in got
    assert ("Unnamed substation (OSM way/2)", 12.85, 74.9) in got
    assert ("Unnamed telecom tower (OSM node/6)", 12.899, 74.849) in got
    assert len(net.assets) == 6  # duplicate hospital dropped
    assert all(a.people_served == 0 for a in net.assets.values())


def test_assumed_links_use_nearest_substation_and_water_radius(real):
    net = build_network(real)
    by_name = {a.name: a.id for a in net.assets.values()}
    links = {(e.supplier, e.dependent) for e in net.edges.values()}
    north, south = by_name["Test Hospital North"], by_name["Test Hospital South"]
    alpha, unnamed = by_name["Alpha Substation"], by_name["Unnamed substation (OSM way/2)"]
    water = by_name["Test Pumping Station"]
    assert (alpha, north) in links and (unnamed, south) in links
    assert (water, north) in links            # about 0.3 km away
    assert (water, south) not in links        # about 7 km away, beyond 5 km
    assert all(e.active for e in net.edges.values())  # no invented alternate lines


def test_source_note_states_what_is_real_and_assumed(real):
    note = data_note(real)
    assert note.startswith("Real locations and names from OpenStreetMap")
    assert "assumptions" in note and "ODbL" in note


def test_simulation_plan_and_warnings_run_on_built_dataset(real):
    net = build_network(real)
    sub = next(a.id for a in net.assets.values() if a.name == "Alpha Substation")
    hosp = next(a.id for a in net.assets.values() if a.name == "Test Hospital North")
    sc = Scenario("flood", "medium", (sub,), 48)
    r = simulate(net, sc)
    assert r.timeline[1]["states"][hosp] == ON_BACKUP and r.timeline[9]["states"][hosp] == FAILED
    assert r.impact["people_data"] is False and r.impact["failed_asset_hours"] > 0
    plan = plan_response(net, sc, Resources())
    assert plan["best"]["score"] >= plan_response(net, sc, Resources(0, 0, 0))["best"]["score"]
    msgs = " ".join(d["message"] for d in draft_warnings(net, sc, r))
    assert "limited hospital services (Test Hospital North)" in msgs


def test_no_substation_is_an_error(tmp_path):
    src = write_osm(tmp_path, [("hospital", "Only Hospital", 12.9, 74.85, "node/9")])
    with pytest.raises(SystemExit, match="No substations"):
        brd.main(src, tmp_path / "out")


def test_demo_dataset_note_is_unchanged():
    assert data_note().startswith("Illustrative demo dataset")
