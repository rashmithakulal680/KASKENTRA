"""Demo city dataset, loaded from CSV files in data/.

All values are illustrative: the locations are example points in Mangaluru, not real
utility assets. Edit data/assets.csv and data/dependencies.csv to model another area.
"""

import csv
from pathlib import Path

import networkx as nx

from app.simulator import Asset, Edge, Network

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
ASSET_TYPES = {"substation", "pump", "treatment", "telecom", "hospital", "eoc", "shelter",
               "residential"}


def load_assets(path: Path = DATA_DIR / "assets.csv") -> dict[str, Asset]:
    assets = {}
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row["type"] not in ASSET_TYPES:
                raise ValueError(f"{path.name}: unknown asset type {row['type']!r} for {row['id']}")
            if row["id"] in assets:
                raise ValueError(f"{path.name}: duplicate asset id {row['id']}")
            assets[row["id"]] = Asset(
                row["id"], row["name"], row["type"], row["service"], float(row["capacity"]),
                int(row["people_served"]), int(row["backup_hours"]), int(row["repair_hours"]),
                int(row["x"]), int(row["y"]), float(row["lat"]), float(row["lon"]), row["zone"])
    return assets


def load_edges(assets: dict[str, Asset],
               path: Path = DATA_DIR / "dependencies.csv") -> dict[str, Edge]:
    edges = {}
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            for end in ("supplier", "dependent"):
                if row[end] not in assets:
                    raise ValueError(f"{path.name}: {row['id']} refers to unknown asset {row[end]}")
            if row["active"] not in ("yes", "no"):
                raise ValueError(f"{path.name}: active must be yes or no for {row['id']}")
            edges[row["id"]] = Edge(row["id"], row["supplier"], row["dependent"],
                                    float(row["load"]), row["active"] == "yes")
    return edges


def build_network() -> Network:
    assets = load_assets()
    edges = load_edges(assets)
    graph = nx.DiGraph()
    graph.add_nodes_from(assets)
    graph.add_edges_from((e.supplier, e.dependent) for e in edges.values())
    return Network(assets=assets, edges=edges, graph=graph)
