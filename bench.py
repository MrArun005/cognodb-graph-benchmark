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
# Index DDL is NOT portable across Bolt engines even though the queries are.
# Neo4j 5 / CognoDB take named indexes; Memgraph only takes the legacy
# CREATE INDEX ON :Label(prop) form. Same logical indexes either way —
# one on :Paper(id) for lookups and edge matching, one on :Paper(year) for the
# filtered lookup — so index parity across platforms is preserved.
DDL_NEO4J_STYLE = [
    ("paper_id",
     "CREATE INDEX paper_id IF NOT EXISTS FOR (p:Paper) ON (p.id)",
     "DROP INDEX paper_id IF EXISTS"),
    ("paper_year",
     "CREATE INDEX paper_year IF NOT EXISTS FOR (p:Paper) ON (p.year)",
     "DROP INDEX paper_year IF EXISTS"),
]
DDL_MEMGRAPH_STYLE = [
    ("paper_id", "CREATE INDEX ON :Paper(id)", "DROP INDEX ON :Paper(id)"),
    ("paper_year", "CREATE INDEX ON :Paper(year)", "DROP INDEX ON :Paper(year)"),
]
DDL_BY_PLATFORM = {
    "cognodb": DDL_NEO4J_STYLE,
    "neo4j": DDL_NEO4J_STYLE,
    "aura": DDL_NEO4J_STYLE,
    "memgraph": DDL_MEMGRAPH_STYLE,
}

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
        self.ddl = DDL_BY_PLATFORM[platform]
        auth = (user, password) if user else None
        # Silence label/reltype-not-found notifications: counts() legitimately runs
        # against an empty graph before loading, and the warnings drown real output.
        self._driver = GraphDatabase.driver(
            uri, auth=auth, warn_notification_severity="OFF"
        )

    def close(self):
        self._driver.close()

    def verify(self):
        self._driver.verify_connectivity()

    def run(self, query, **params):
        with self._driver.session() as s:
            return [dict(r) for r in s.run(query, **params)]

    def timed(self, query, **params):
        """Returns (rows, latency_ms). Latency is client-observed wall clock: it
        includes network RTT, which is why the client must sit in-region.

        Opens a fresh session per call. Thread-safe, so the concurrent mixed
        workload uses this directly; single-threaded latency runs should use
        session() below to keep session setup out of the measurement.
        """
        with self._driver.session() as s:
            t = time.perf_counter()
            rows = [dict(r) for r in s.run(query, **params)]
            return rows, (time.perf_counter() - t) * 1000

    def session(self):
        """Reusable session for repeated latency measurement.

        Session acquisition is ~free relative to a WAN round trip but is NOT free
        relative to a 0.4 ms local query, where it would be a large fraction of
        the reported number. Reusing one session across iterations measures the
        query rather than the driver. Not thread-safe: one per thread.
        """
        driver_session = self._driver.session()

        class _Timed:
            def timed(_self, query, **params):
                t = time.perf_counter()
                rows = [dict(r) for r in driver_session.run(query, **params)]
                return rows, (time.perf_counter() - t) * 1000

            def close(_self):
                driver_session.close()

        return _Timed()

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
        for _, _, drop in self.ddl:
            try:
                self.run(drop)
            except Exception:
                pass  # index may not exist; wipe must stay idempotent

    def create_indexes(self):
        out = {}
        for name, create, _ in self.ddl:
            t = time.perf_counter()
            self.run(create)
            out[name] = round((time.perf_counter() - t) * 1000, 2)
        return out

    def await_indexes(self):
        """Block until indexes are queryable.

        Returns (confirmed, note). `confirmed` is False only when the platform
        gives us no way to verify readiness, in which case the caller must record
        that honestly instead of assuming the index is live. Neo4j exposes
        db.awaitIndexes; Memgraph builds indexes synchronously; CognoDB offers
        neither, so it is the only platform that gets the unverifiable caveat.
        """
        try:
            self.run("CALL db.awaitIndexes()")
            return True, "confirmed via db.awaitIndexes()"
        except Exception:
            pass
        try:
            # Memgraph: indexes are created synchronously, and SHOW INDEX INFO
            # listing them is proof they exist rather than merely being queued.
            rows = self.run("SHOW INDEX INFO")
            if rows:
                return True, f"confirmed via SHOW INDEX INFO ({len(rows)} indexes)"
        except Exception:
            pass
        return False, "not verifiable on this platform"

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
