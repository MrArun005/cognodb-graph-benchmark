"""Turn raw SNAP cit-HepTh into the one canonical dataset every platform loads.

Emits data/nodes.csv, data/edges.csv, data/start_nodes.json and data/MANIFEST.json.
Every loader reads these files and nothing else, so "same dataset everywhere" is
enforced by construction rather than by hoping each loader agrees.
"""
import csv
import gzip
import hashlib
import json
import random
from collections import defaultdict
from pathlib import Path

RAW = Path("data/raw")
OUT = Path("data")
SEED = 42          # fixed so every platform gets the identical start nodes
N_START = 200      # start nodes sampled once, reused by every workload everywhere


def normalise(node_id: str) -> int:
    """SNAP: cross-listed papers appear as 11<true_id>. Collapse to the true id.

    The dates file documents this quirk; the edge file does not. Without this,
    the same paper exists as two distinct nodes and hop counts are wrong.
    """
    n = int(node_id)
    s = str(n)
    if len(s) == 9 and s.startswith("11"):
        return int(s[2:])
    return n


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    OUT.mkdir(exist_ok=True)

    # --- dates: paper id -> publication date ---
    dates = {}
    with gzip.open(RAW / "cit-HepTh-dates.txt.gz", "rt") as f:
        for line in f:
            if line.startswith("#"):
                continue
            pid, date = line.split()
            dates[normalise(pid)] = date

    # --- edges: dedupe and drop self-loops so all platforms see one clean set ---
    edges, seen = [], set()
    self_loops = dropped_dupes = 0
    with gzip.open(RAW / "cit-HepTh.txt.gz", "rt") as f:
        for line in f:
            if line.startswith("#"):
                continue
            a, b = line.split()
            src, dst = normalise(a), normalise(b)
            if src == dst:
                self_loops += 1
                continue
            if (src, dst) in seen:
                dropped_dupes += 1
                continue
            seen.add((src, dst))
            edges.append((src, dst))

    # --- nodes: every id appearing in any edge ---
    node_ids = sorted({n for e in edges for n in e})
    out_deg = defaultdict(int)
    for src, _ in edges:
        out_deg[src] += 1

    with open(OUT / "nodes.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["id", "date", "year"])
        for n in node_ids:
            d = dates.get(n, "")
            w.writerow([n, d, d[:4] if d else ""])

    with open(OUT / "edges.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["src", "dst"])
        w.writerows(edges)

    # --- start nodes: sampled ONCE, deterministically, shared by all platforms ---
    # Require out-degree >= 3 so multi-hop traversals actually traverse something
    # instead of dead-ending and reporting a misleadingly fast p50.
    eligible = sorted(n for n in node_ids if out_deg[n] >= 3)
    start_nodes = random.Random(SEED).sample(eligible, min(N_START, len(eligible)))
    (OUT / "start_nodes.json").write_text(json.dumps(start_nodes, indent=2))

    with_dates = sum(1 for n in node_ids if n in dates)
    manifest = {
        "dataset": "SNAP cit-HepTh (Arxiv HEP-TH paper citation network)",
        "source": "https://snap.stanford.edu/data/cit-HepTh.html",
        "raw_files": {
            "cit-HepTh.txt.gz": sha256(RAW / "cit-HepTh.txt.gz"),
            "cit-HepTh-dates.txt.gz": sha256(RAW / "cit-HepTh-dates.txt.gz"),
        },
        "canonical_files": {
            "nodes.csv": sha256(OUT / "nodes.csv"),
            "edges.csv": sha256(OUT / "edges.csv"),
        },
        "nodes": len(node_ids),
        "relationships": len(edges),
        "nodes_with_date": with_dates,
        "nodes_without_date": len(node_ids) - with_dates,
        "self_loops_removed": self_loops,
        "duplicate_edges_removed": dropped_dupes,
        "start_nodes": {"count": len(start_nodes), "seed": SEED, "min_out_degree": 3},
        "schema": {
            "node": "(:Paper {id: int, date: string, year: int})",
            "relationship": "(:Paper)-[:CITES]->(:Paper)",
        },
    }
    (OUT / "MANIFEST.json").write_text(json.dumps(manifest, indent=2))

    for k, v in manifest.items():
        if not isinstance(v, dict):
            print(f"  {k}: {v}")
    print(f"  degree>=3 eligible start nodes: {len(eligible)}")
    print(f"  max out-degree: {max(out_deg.values())}")


if __name__ == "__main__":
    main()
