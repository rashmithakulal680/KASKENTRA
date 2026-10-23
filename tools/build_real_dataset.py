"""Turn data/osm_mangaluru.csv (real OpenStreetMap locations) into a KASKENTRA dataset.

    .\\.venv\\Scripts\\python tools\\build_real_dataset.py
    $env:KASKENTRA_DATA = "data/real"; .\\.venv\\Scripts\\python -m uvicorn app.main:app

REAL (copied from OpenStreetMap, nothing added): each asset's name, category and coordinates.
ASSUMED (OpenStreetMap does not record them; utilities do not publish them):
  - which substation powers which asset: the nearest substation in a straight line;
  - which water facility supplies a hospital: the nearest one within WATER_RADIUS_KM;
  - backup hours, repair hours and capacities: the fixed values in ASSUMPTIONS below;
  - people served: unknown, left at 0, so scores use failed asset-hours instead.
Every assumption is written to data/real/SOURCE.txt and shown in the app.
"""
import csv
import math
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WATER_RADIUS_KM = 5.0

# category -> (asset type, service, backup hours, repair hours). ASSUMED values.
ASSUMPTIONS = {
    "substation": ("substation", "power", 0, 24),
    "water": ("pump", "water", 0, 12),
    "telecom": ("telecom", "telecom", 4, 8),
    "hospital": ("hospital", "healthcare", 8, 24),
}
PREFIX = {"substation": "SUB", "water": "WAT", "telecom": "TEL", "hospital": "HOS"}
LABEL = {"substation": "substation", "water": "water facility", "telecom": "telecom tower",
         "hospital": "hospital"}
PLURAL = {"substation": "substations", "water": "water facilities", "telecom": "telecom towers",
          "hospital": "hospitals"}


def km(a, b):
    """Great-circle distance in kilometres between two (lat, lon) points."""
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    return 2 * 6371.0 * math.asin(math.sqrt(h))


def read_places(path):
    places, seen = [], set()
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row["category"] not in ASSUMPTIONS:
                continue
            name = row["name"].strip()
            key = (row["category"], name.lower()) if name else (row["category"], row["osm"])
            if key in seen:  # the same named place mapped twice (e.g. node + building)
                continue
            seen.add(key)
            places.append({"category": row["category"], "name": name, "osm": row["osm"],
                           "lat": float(row["lat"]), "lon": float(row["lon"])})
    return places


def build(places):
    subs = [p for p in places if p["category"] == "substation"]
    if not subs:
        raise SystemExit("No substations in the file, so there is nothing to simulate.")
    counters = {}
    for p in places:  # stable ids per category
        counters[p["category"]] = counters.get(p["category"], 0) + 1
        p["id"] = f"{PREFIX[p['category']]}{counters[p['category']]:02d}"
        p["label"] = p["name"] or f"Unnamed {LABEL[p['category']]} (OSM {p['osm']})"
    lats = [p["lat"] for p in places]; lons = [p["lon"] for p in places]
    span_lat = (max(lats) - min(lats)) or 1; span_lon = (max(lons) - min(lons)) or 1

    def nearest(p, pool):
        return min(pool, key=lambda q: (km((p["lat"], p["lon"]), (q["lat"], q["lon"])), q["id"]))

    assets, edges = [], []
    water = [p for p in places if p["category"] == "water"]
    for p in places:
        typ, service, backup, repair = ASSUMPTIONS[p["category"]]
        home = p if p["category"] == "substation" else nearest(p, subs)
        assets.append({"id": p["id"], "name": p["label"], "type": typ, "service": service,
                       "capacity": 0, "people_served": "", "backup_hours": backup,
                       "repair_hours": repair,
                       "x": round(60 + (p["lon"] - min(lons)) / span_lon * 780),
                       "y": round(490 - (p["lat"] - min(lats)) / span_lat * 420),
                       "lat": p["lat"], "lon": p["lon"], "zone": f"Area of {home['label']}",
                       "osm": p["osm"]})
        if p["category"] != "substation":
            edges.append((home["id"], p["id"]))
        if p["category"] == "hospital" and water:
            w = nearest(p, water)
            if km((p["lat"], p["lon"]), (w["lat"], w["lon"])) <= WATER_RADIUS_KM:
                edges.append((w["id"], p["id"]))
    return assets, edges


def write(assets, edges, out, counts):
    out.mkdir(parents=True, exist_ok=True)
    fields = ["id", "name", "type", "service", "capacity", "people_served", "backup_hours",
              "repair_hours", "x", "y", "lat", "lon", "zone", "osm"]
    with open(out / "assets.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields); w.writeheader(); w.writerows(assets)
    with open(out / "dependencies.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f); w.writerow(["id", "supplier", "dependent", "load", "active"])
        for s, d in edges:
            w.writerow([f"{s}-{d}", s, d, 1, "yes"])
    summary = ", ".join(f"{n} {LABEL[c] if n == 1 else PLURAL[c]}" for c, n in sorted(counts.items()))
    (out / "SOURCE.txt").write_text(
        f"Real locations and names from OpenStreetMap ({summary}); connections, backup and repair "
        f"times are assumptions, not utility data. Map data (c) OpenStreetMap contributors, ODbL.\n"
        f"Built {date.today().isoformat()} from data/osm_mangaluru.csv by tools/build_real_dataset.py.\n"
        f"Assumed: every asset is powered by its nearest substation (straight line); a hospital gets "
        f"water from the nearest water facility within {WATER_RADIUS_KM:g} km; backup hours "
        f"hospital 8, telecom 4, others 0; repair hours substation 24, hospital 24, water 12, "
        f"telecom 8; no alternate supply lines; population unknown.\n", encoding="utf-8")


def main(src=ROOT / "data" / "osm_mangaluru.csv", out=ROOT / "data" / "real"):
    if not Path(src).exists():
        raise SystemExit(f"{src} not found. Run tools/fetch_osm_mangaluru.py first.")
    places = read_places(src)
    assets, edges = build(places)
    counts = {}
    for p in places:
        counts[p["category"]] = counts.get(p["category"], 0) + 1
    write(assets, edges, Path(out), counts)
    print(f"Built {len(assets)} assets and {len(edges)} assumed connections in {out}")


if __name__ == "__main__":
    main(*sys.argv[1:])
