"""Download real infrastructure locations in Mangaluru from OpenStreetMap.

Run from the KASKENTRA folder (needs internet):
    .\\.venv\\Scripts\\python tools\\fetch_osm_mangaluru.py

It writes data/osm_mangaluru.csv with every mapped substation, hospital, water facility and
communication tower inside the box below. Only locations and the tags people mapped are
real; how these assets depend on each other is NOT in OpenStreetMap.
Data (c) OpenStreetMap contributors, ODbL licence.
"""
import csv
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

# south, west, north, east: roughly Mangaluru city
BBOX = (12.80, 74.80, 12.98, 74.95)
ENDPOINTS = ["https://overpass-api.de/api/interpreter",
             "https://overpass.kumi.systems/api/interpreter"]

QUERY = """
[out:json][timeout:90];
(
  nwr["power"="substation"]({b});
  nwr["amenity"="hospital"]({b});
  nwr["man_made"~"^(water_works|water_tower|pumping_station|reservoir_covered)$"]({b});
  nwr["man_made"~"^(mast|tower)$"]["tower:type"="communication"]({b});
);
out center tags;
""".format(b=",".join(str(v) for v in BBOX))


def category(tags):
    if tags.get("power") == "substation":
        return "substation"
    if tags.get("amenity") == "hospital":
        return "hospital"
    if tags.get("man_made") in ("water_works", "water_tower", "pumping_station", "reservoir_covered"):
        return "water"
    return "telecom"


def fetch():
    body = urllib.parse.urlencode({"data": QUERY}).encode()
    last = None
    for url in ENDPOINTS:
        try:
            req = urllib.request.Request(url, data=body, headers={"User-Agent": "KASKENTRA-hackathon/1.0"})
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.load(r)
        except Exception as exc:  # try the next server
            last = exc
            print(f"{url} failed: {exc}", file=sys.stderr)
    raise SystemExit(f"Could not reach OpenStreetMap ({last}). Check your internet and try again.")


def rows(data):
    out = []
    for el in data.get("elements", []):
        tags = el.get("tags", {})
        lat = el.get("lat", el.get("center", {}).get("lat"))
        lon = el.get("lon", el.get("center", {}).get("lon"))
        if lat is None or lon is None:
            continue
        out.append({"category": category(tags), "name": tags.get("name", ""),
                    "lat": round(lat, 6), "lon": round(lon, 6),
                    "osm": f"{el['type']}/{el['id']}",
                    "voltage": tags.get("voltage", ""), "operator": tags.get("operator", ""),
                    "substation_type": tags.get("substation", ""),
                    "man_made": tags.get("man_made", "")})
    return sorted(out, key=lambda r: (r["category"], r["name"] == "", r["name"], r["osm"]))


def main():
    data = rows(fetch())
    out = Path(__file__).resolve().parent.parent / "data" / "osm_mangaluru.csv"
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(data[0]) if data else ["category"])
        w.writeheader()
        w.writerows(data)
    counts = {}
    for r in data:
        counts[r["category"]] = counts.get(r["category"], 0) + 1
    print(f"Saved {len(data)} places to {out}: {counts}")


if __name__ == "__main__":
    main()
