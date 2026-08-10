"""Shared benchmark harness: dataset access, timing, stats, and the Bolt adapter.

One adapter serves every Bolt/Cypher platform (CognoDB, Neo4j, Memgraph). Non-Bolt
engines get their own adapter class exposing the same methods, so the workload
definitions and reporting stay platform-independent.
"""
from __future__ import annotations

import csv
import json
import os
import statistics
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path

DATA = Path("data")
RESULTS = Path("results")

# Same on every platform. Changing this invalidates cross-platform comparison.
BATCH_SIZE = 1_000


# --------------------------------------------------------------------------- data
def read_nodes():
    with open(DATA / "nodes.csv") as f:
        for r in csv.DictReader(f):
            yield {
                "id": int(r["id"]),
                "date": r["date"] or None,
                "year": int(r["year"]) if r["year"] else None,
            }


def read_edges():
    with open(DATA / "edges.csv") as f:
        for r in csv.DictReader(f):
            yield {"src": int(r["src"]), "dst": int(r["dst"])}


def start_nodes():
    return json.loads((DATA / "start_nodes.json").read_text())


def manifest():
    return json.loads((DATA / "MANIFEST.json").read_text())


def batched(it, n):
    batch = []
    for item in it:
        batch.append(item)
        if len(batch) == n:
            yield batch
            batch = []
    if batch:
        yield batch


# -------------------------------------------------------------------------- stats
def pct(values, p):
    """Nearest-rank percentile. Exact and dependency-free; no interpolation so the
    reported number is always an actually-observed measurement."""
    if not values:
        return None
    v = sorted(values)
    k = max(0, min(len(v) - 1, int(round(p / 100 * len(v) + 0.5)) - 1))
    return v[k]


def summarise(latencies_ms):
    if not latencies_ms:
        return {"n": 0}
    return {
        "n": len(latencies_ms),
        "p50_ms": round(pct(latencies_ms, 50), 3),
        "p95_ms": round(pct(latencies_ms, 95), 3),
        "p99_ms": round(pct(latencies_ms, 99), 3),
        "min_ms": round(min(latencies_ms), 3),
        "max_ms": round(max(latencies_ms), 3),
        "mean_ms": round(statistics.fmean(latencies_ms), 3),
        "stdev_ms": round(statistics.stdev(latencies_ms), 3) if len(latencies_ms) > 1 else 0.0,
    }


@dataclass
class Result:
    platform: str
    phase: str
    metrics: dict = field(default_factory=dict)
    caveats: list = field(default_factory=list)

    def write(self, tag=""):
        RESULTS.mkdir(exist_ok=True)
        name = f"{self.platform}_{self.phase}{('_' + tag) if tag else ''}.json"
        (RESULTS / name).write_text(json.dumps(asdict(self), indent=2, default=str))
        return RESULTS / name


# ------------------------------------------------------------------ bolt adapter
# Schema and DDL are identical across Bolt platforms. Plain indexes (not unique
# constraints) are used deliberately: node ids are already unique by construction
# in prep_dataset.py, and plain secondary indexes are the one index type every
# engine in this comparison supports with equivalent semantics.
DDL = [
    ("paper_id", "CREATE INDEX paper_id IF NOT EXISTS FOR (p:Paper) ON (p.id)"),
    ("paper_year", "CREATE INDEX paper_year IF NOT EXISTS FOR (p:Paper) ON (p.year)"),
]

LOAD_NODES = """
UNWIND $rows AS row
CREATE (p:Paper {id: row.id, date: row.date, year: row.year})
"""

LOAD_EDGES = """
UNWIND $rows AS row
MATCH (a:Paper {id: row.src})
MATCH (b:Paper {id: row.dst})
CREATE (a)-[:CITES]->(b)
"""


class BoltAdapter:
    """Covers CognoDB, Neo4j and Memgraph — all speak Bolt + Cypher."""

    dialect = "cypher"

    def __init__(self, platform: str, uri: str, user: str, password: str):
        from neo4j import GraphDatabase

        self.platform = platform
        self.uri = uri
        auth = (user, password) if user else None
        self._driver = GraphDatabase.driver(uri, auth=auth)

    def close(self):
        self._driver.close()

    def verify(self):
        self._driver.verify_connectivity()

    def run(self, query, **params):
        with self._driver.session() as s:
            return [dict(r) for r in s.run(query, **params)]

    def timed(self, query, **params):
        """Returns (rows, latency_ms). Latency is client-observed wall clock: it
        includes network RTT, which is why the client must sit in-region."""
        with self._driver.session() as s:
            t = time.perf_counter()
            rows = [dict(r) for r in s.run(query, **params)]
            return rows, (time.perf_counter() - t) * 1000

    # -- lifecycle -----------------------------------------------------------
    def wipe(self):
        """Delete all data. Batched, because a single DETACH DELETE of 350k
        relationships will exhaust 512 MB of heap."""
        while True:
            rows = self.run(
                "MATCH (n) WITH n LIMIT 5000 DETACH DELETE n RETURN count(*) AS c"
            )
            if not rows or rows[0]["c"] == 0:
                break
        for name, _ in DDL:
            try:
                self.run(f"DROP INDEX {name} IF EXISTS")
            except Exception:
                pass

    def create_indexes(self):
        out = {}
        for name, ddl in DDL:
            t = time.perf_counter()
            self.run(ddl)
            out[name] = round((time.perf_counter() - t) * 1000, 2)
        return out

    def counts(self):
        n = self.run("MATCH (n:Paper) RETURN count(n) AS c")[0]["c"]
        r = self.run("MATCH (:Paper)-[:CITES]->(:Paper) RETURN count(*) AS c")[0]["c"]
        return n, r

    def load_nodes(self, rows_iter, batch_size=BATCH_SIZE):
        return self._load(LOAD_NODES, rows_iter, batch_size)

    def load_edges(self, rows_iter, batch_size=BATCH_SIZE):
        return self._load(LOAD_EDGES, rows_iter, batch_size)

    def _load(self, query, rows_iter, batch_size):
        """Load in fixed batches, recording per-batch latency so throttling and
        degradation over the run are visible rather than averaged away."""
        per_batch, total = [], 0
        t0 = time.perf_counter()
        with self._driver.session() as s:
            for batch in batched(rows_iter, batch_size):
                t = time.perf_counter()
                s.run(query, rows=batch).consume()
                per_batch.append((time.perf_counter() - t) * 1000)
                total += len(batch)
        wall = time.perf_counter() - t0
        return {
            "rows": total,
            "wall_s": round(wall, 3),
            "rows_per_s": round(total / wall, 1) if wall else None,
            "batch_size": batch_size,
            "batches": len(per_batch),
            "batch_latency": summarise(per_batch),
            "first_10_batches_ms": [round(x, 1) for x in per_batch[:10]],
            "last_10_batches_ms": [round(x, 1) for x in per_batch[-10:]],
        }


# ---------------------------------------------------------------------- workloads
# Hop depths are written as explicit chained patterns rather than -[:CITES*2]->.
# Variable-length path semantics (relationship-uniqueness rules) differ between
# engines, so an explicit chain is the only way to guarantee all five platforms
# execute the identical logical traversal.
#
# Every traversal returns an aggregate, never rows. Two reasons: CognoDB caps
# results at 50,000 rows (a 3-hop fan-out exceeds that), and returning rows would
# measure result serialisation instead of traversal.
READ_WORKLOADS = {
    "hop1": {
        "cypher": "MATCH (p:Paper {id:$id})-[:CITES]->(a) RETURN count(DISTINCT a) AS c",
        "param": "start_node",
    },
    "hop2": {
        "cypher": "MATCH (p:Paper {id:$id})-[:CITES]->()-[:CITES]->(a) "
                  "RETURN count(DISTINCT a) AS c",
        "param": "start_node",
    },
    "hop3": {
        "cypher": "MATCH (p:Paper {id:$id})-[:CITES]->()-[:CITES]->()-[:CITES]->(a) "
                  "RETURN count(DISTINCT a) AS c",
        "param": "start_node",
    },
    "point_lookup": {
        "cypher": "MATCH (p:Paper {id:$id}) RETURN p.id AS id, p.date AS date",
        "param": "start_node",
    },
    "indexed_filter": {
        "cypher": "MATCH (p:Paper) WHERE p.year = $year RETURN count(p) AS c",
        "param": "year",
    },
    "agg_group_by": {
        "cypher": "MATCH (p:Paper) WHERE p.year IS NOT NULL "
                  "RETURN p.year AS y, count(*) AS c ORDER BY y",
        "param": None,
    },
    "agg_rel_count": {
        "cypher": "MATCH (:Paper)-[:CITES]->(:Paper) RETURN count(*) AS c",
        "param": None,
    },
}

# Years present in the dataset (see MANIFEST). Cycled so the filtered lookup does
# not repeatedly hit one cached selectivity.
FILTER_YEARS = [1994, 1996, 1998, 1999, 2000, 2001, 2002]

# Mixed-workload write. Targets a dedicated label/reltype so the benchmark dataset
# is never mutated: :Paper and :CITES counts stay identical before and after, which
# keeps read numbers comparable across concurrency levels and across platforms.
MIXED_WRITE = """
MATCH (target:Paper {id: $id})
CREATE (s:Scratch {sid: $sid})-[:SCRATCH_CITES]->(target)
"""
MIXED_CLEANUP = "MATCH (s:Scratch) WITH s LIMIT 5000 DETACH DELETE s RETURN count(*) AS c"


# ------------------------------------------------------------------ construction
def from_env(platform: str):
    """Build an adapter from environment variables. Secrets never touch the repo."""
    p = platform.upper()
    if platform in ("cognodb", "neo4j", "memgraph", "aura"):
        uri = os.environ[f"{p}_URI"]
        return BoltAdapter(
            platform, uri, os.environ.get(f"{p}_USER", ""), os.environ.get(f"{p}_PASSWORD", "")
        )
    raise SystemExit(f"no adapter for platform {platform!r}")
