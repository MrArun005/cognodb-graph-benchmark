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
    noop = "RETURN 1 AS n"

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
        """Delete all data, relationships first, in small batches.

        DETACH DELETE on a node drags every attached relationship into the same
        transaction, so batching by NODE is misleading: 5,000 nodes can pull in
        hundreds of thousands of relationships and blow
        db.memory.transaction.total.max (128 MiB here) even though the node batch
        looks modest. Deleting relationships first, in their own small batches,
        keeps every transaction bounded regardless of graph density.
        """
        while True:
            rows = self.run(
                "MATCH ()-[r]->() WITH r LIMIT 10000 DELETE r RETURN count(*) AS c"
            )
            if not rows or rows[0]["c"] == 0:
                break
        while True:
            rows = self.run(
                "MATCH (n) WITH n LIMIT 10000 DELETE n RETURN count(*) AS c"
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
# AQL variants express the SAME logical question, not a transliteration. Each is
# written to accept the identical bind parameters as its Cypher twin (the document
# handle is built inside the query with CONCAT) so run.py needs no per-platform
# parameter mapping. Semantic differences that could not be eliminated are called
# out per workload and repeated in the README caveats.
READ_WORKLOADS = {
    "hop1": {
        "cypher": "MATCH (p:Paper {id:$id})-[:CITES]->(a) RETURN count(DISTINCT a) AS c",
        "aql": "RETURN COUNT(FOR v IN 1..1 OUTBOUND CONCAT('papers/', @id) cites "
               "RETURN DISTINCT v._id)",
        "param": "start_node",
    },
    "hop2": {
        "cypher": "MATCH (p:Paper {id:$id})-[:CITES]->()-[:CITES]->(a) "
                  "RETURN count(DISTINCT a) AS c",
        "aql": "RETURN COUNT(FOR v IN 2..2 OUTBOUND CONCAT('papers/', @id) cites "
               "RETURN DISTINCT v._id)",
        "param": "start_node",
    },
    "hop3": {
        "cypher": "MATCH (p:Paper {id:$id})-[:CITES]->()-[:CITES]->()-[:CITES]->(a) "
                  "RETURN count(DISTINCT a) AS c",
        "aql": "RETURN COUNT(FOR v IN 3..3 OUTBOUND CONCAT('papers/', @id) cites "
               "RETURN DISTINCT v._id)",
        "param": "start_node",
    },
    "point_lookup": {
        "cypher": "MATCH (p:Paper {id:$id}) RETURN p.id AS id, p.date AS date",
        # _key is the primary index in ArangoDB, so this is the closest equivalent
        # to an indexed point lookup on :Paper(id).
        "aql": "FOR p IN papers FILTER p._key == TO_STRING(@id) "
               "RETURN {id: p.id, date: p.date}",
        "param": "start_node",
    },
    "indexed_filter": {
        "cypher": "MATCH (p:Paper) WHERE p.year = $year RETURN count(p) AS c",
        "aql": "RETURN COUNT(FOR p IN papers FILTER p.year == @year RETURN 1)",
        "param": "year",
    },
    "agg_group_by": {
        "cypher": "MATCH (p:Paper) WHERE p.year IS NOT NULL "
                  "RETURN p.year AS y, count(*) AS c ORDER BY y",
        "aql": "FOR p IN papers FILTER p.year != null COLLECT y = p.year "
               "WITH COUNT INTO c SORT y RETURN {y: y, c: c}",
        "param": None,
    },
    "agg_rel_count": {
        "cypher": "MATCH (:Paper)-[:CITES]->(:Paper) RETURN count(*) AS c",
        # NOT equivalent in cost. The Cypher form constrains the label at BOTH
        # endpoints, forcing a real traversal of every relationship. ArangoDB can
        # satisfy a full-collection count from collection metadata, so its number
        # here measures a metadata read, not a scan. Reported but flagged as
        # non-comparable rather than quietly presented alongside the others.
        "aql": "RETURN COUNT(FOR e IN cites RETURN 1)",
        "param": None,
        "not_comparable": {
            "aql": "ArangoDB may answer a full-collection count from metadata; the "
                   "Cypher form is label-constrained at both endpoints and must scan."
        },
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

# AQL equivalents of the mixed-workload write: insert a scratch vertex plus one
# edge to an existing paper, same as the Cypher form.
MIXED_WRITE_AQL = """
LET s = FIRST(INSERT {sid: @sid} INTO scratch RETURN NEW)
INSERT {_from: s._id, _to: CONCAT('papers/', @id)} INTO scratch_cites
RETURN 1
"""
MIXED_CLEANUP_AQL = """
LET n = COUNT(FOR d IN scratch LIMIT 5000 REMOVE d IN scratch RETURN 1)
LET e = COUNT(FOR d IN scratch_cites LIMIT 5000 REMOVE d IN scratch_cites RETURN 1)
RETURN {c: n + e}
"""

MIXED_BY_DIALECT = {
    "cypher": (MIXED_WRITE, MIXED_CLEANUP),
    "aql": (MIXED_WRITE_AQL, MIXED_CLEANUP_AQL),
}


# -------------------------------------------------------------- falkordb adapter
class FalkorAdapter:
    """FalkorDB: Cypher over the Redis protocol, GraphBLAS execution engine.

    Speaks the same Cypher as the Bolt engines, so it reuses READ_WORKLOADS
    verbatim — only the transport differs. Exposes the same interface as
    BoltAdapter so run.py needs no special-casing.
    """

    dialect = "cypher"
    noop = "RETURN 1 AS n"
    GRAPH = "bench"

    def __init__(self, platform, host, port, password=None):
        import threading

        from falkordb import FalkorDB

        self.platform = platform
        # socket_timeout is essential, not cosmetic: FalkorDB ships
        # MAX_QUEUED_QUERIES=25, so a 40-client sweep pushes past its queue and a
        # blocking socket waits forever. With a timeout the harness records a
        # failure — which is a real, reportable result — instead of deadlocking.
        self._conn = {
            "host": host,
            "port": int(port),
            "password": password or None,
            "socket_timeout": 30,
            "socket_connect_timeout": 15,
        }
        self._client = FalkorDB(**self._conn)
        # Per-thread client+graph. A single Redis connection shared across the
        # mixed workload's threads interleaves writes on one socket, desyncs the
        # protocol and hangs — not an error, a silent deadlock. Thread-local
        # connections are the only safe way to run the concurrency sweep here.
        self._local = threading.local()

    @property
    def _g(self):
        g = getattr(self._local, "graph", None)
        if g is None:
            from falkordb import FalkorDB

            client = FalkorDB(**self._conn)
            g = client.select_graph(self.GRAPH)
            self._local.client, self._local.graph = client, g
        return g

    def close(self):
        pass  # redis client pools connections; nothing to release explicitly

    def verify(self):
        self._g.query("RETURN 1")

    def run(self, query, **params):
        res = self._g.query(query, params=params or None)
        return self._rows(res)

    @staticmethod
    def _rows(res):
        """Normalise FalkorDB's positional result_set into dicts keyed by header,
        so callers see the same shape the Bolt adapter returns."""
        header = [h[1] if isinstance(h, (list, tuple)) else h for h in (res.header or [])]
        return [dict(zip(header, row)) for row in (res.result_set or [])]

    def timed(self, query, **params):
        t = time.perf_counter()
        rows = self.run(query, **params)
        return rows, (time.perf_counter() - t) * 1000

    def session(self):
        return self  # connection is already persistent; nothing extra to reuse

    def wipe(self):
        try:
            self._g.delete()  # drops the whole graph key, indexes included
        except Exception:
            pass
        # Drop this thread's cached handle so the next access re-selects the graph.
        self._local.graph = None

    def create_indexes(self):
        out = {}
        for name, prop in (("paper_id", "id"), ("paper_year", "year")):
            t = time.perf_counter()
            try:
                self.run(f"CREATE INDEX FOR (p:Paper) ON (p.{prop})")
            except Exception as e:
                if "already indexed" not in str(e).lower():
                    raise
            out[name] = round((time.perf_counter() - t) * 1000, 2)
        return out

    def await_indexes(self):
        """FalkorDB builds indexes synchronously and reports one row per LABEL,
        with all indexed properties grouped together — so a row count is not an
        index count. Assert the actual properties instead, which is the thing
        index parity across platforms depends on."""
        try:
            rows = self.run("CALL db.indexes()")
            props, statuses = set(), set()
            for r in rows:
                for p in r.get("properties") or []:
                    props.add(str(p))
                statuses.add(str(r.get("status")))
            missing = {"id", "year"} - props
            if missing:
                return False, f"indexes MISSING for {sorted(missing)} (found {sorted(props)})"
            return True, f"confirmed indexed properties {sorted(props)}, status {sorted(statuses)}"
        except Exception as e:
            return False, f"not verifiable ({type(e).__name__})"

    def counts(self):
        n = self.run("MATCH (n:Paper) RETURN count(n) AS c")
        r = self.run("MATCH (:Paper)-[:CITES]->(:Paper) RETURN count(*) AS c")
        return (n[0]["c"] if n else 0), (r[0]["c"] if r else 0)

    def load_nodes(self, rows_iter, batch_size=BATCH_SIZE):
        return self._load(LOAD_NODES, rows_iter, batch_size)

    def load_edges(self, rows_iter, batch_size=BATCH_SIZE):
        return self._load(LOAD_EDGES, rows_iter, batch_size)

    def _load(self, query, rows_iter, batch_size):
        per_batch, total = [], 0
        t0 = time.perf_counter()
        for batch in batched(rows_iter, batch_size):
            t = time.perf_counter()
            self.run(query, rows=batch)
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


# -------------------------------------------------------------- arangodb adapter
class ArangoAdapter:
    """ArangoDB: multi-model document store with a graph layer, queried in AQL.

    The only non-Cypher engine in the comparison, included deliberately — a panel
    of four Cypher engines would not test whether findings are language-specific.
    Same interface as the other adapters; see READ_WORKLOADS for the AQL variants
    and the documented non-equivalences.
    """

    dialect = "aql"
    noop = "RETURN 1"   # AQL has no AS alias in a bare RETURN
    DB = "bench"

    def __init__(self, platform, url, user, password):
        import threading

        from arango import ArangoClient

        self.platform = platform
        self._auth = (user, password)
        self._client = ArangoClient(hosts=url)
        sys_db = self._client.db("_system", username=user, password=password)
        if not sys_db.has_database(self.DB):
            sys_db.create_database(self.DB)
        # python-arango wraps a requests.Session, which is not thread-safe; the
        # concurrency sweep needs one database handle per thread.
        self._local = threading.local()
        self._ensure_collections()

    @property
    def _db(self):
        db = getattr(self._local, "db", None)
        if db is None:
            db = self._client.db(self.DB, username=self._auth[0], password=self._auth[1])
            self._local.db = db
        return db

    def _ensure_collections(self):
        if not self._db.has_collection("papers"):
            self._db.create_collection("papers")
        for name in ("cites", "scratch_cites"):
            if not self._db.has_collection(name):
                self._db.create_collection(name, edge=True)
        if not self._db.has_collection("scratch"):
            self._db.create_collection("scratch")

    def close(self):
        pass

    def verify(self):
        self._db.properties()

    def run(self, query, **params):
        cursor = self._db.aql.execute(query, bind_vars=params or None)
        rows = list(cursor)
        # Normalise scalar results (AQL "RETURN COUNT(...)") into the dict shape
        # the Bolt/Falkor adapters return, so reporting code stays uniform.
        return [r if isinstance(r, dict) else {"c": r} for r in rows]

    def timed(self, query, **params):
        t = time.perf_counter()
        rows = self.run(query, **params)
        return rows, (time.perf_counter() - t) * 1000

    def session(self):
        return self  # HTTP connection pooling is handled by the client

    def wipe(self):
        for name in ("papers", "cites", "scratch", "scratch_cites"):
            if self._db.has_collection(name):
                self._db.collection(name).truncate()
        try:
            papers = self._db.collection("papers")
            for idx in papers.indexes():
                if idx["type"] != "primary":
                    papers.delete_index(idx["id"])
        except Exception:
            pass

    def create_indexes(self):
        out = {}
        papers = self._db.collection("papers")
        # _key carries ArangoDB's mandatory primary index, which is the structural
        # equivalent of the :Paper(id) index on the Cypher engines — recorded as 0 ms
        # because it cannot be created or dropped, not because it was skipped.
        out["paper_id"] = 0.0
        t = time.perf_counter()
        papers.add_persistent_index(fields=["year"], name="paper_year", in_background=False)
        out["paper_year"] = round((time.perf_counter() - t) * 1000, 2)
        return out

    def await_indexes(self):
        try:
            idx = self._db.collection("papers").indexes()
            fields = {tuple(i.get("fields") or []) for i in idx}
            if ("year",) not in fields:
                return False, f"year index MISSING (found {sorted(map(list, fields))})"
            return True, (f"confirmed: primary index on _key + persistent index on year "
                          f"({len(idx)} indexes, created with in_background=False)")
        except Exception as e:
            return False, f"not verifiable ({type(e).__name__})"

    def counts(self):
        return self._db.collection("papers").count(), self._db.collection("cites").count()

    def load_nodes(self, rows_iter, batch_size=BATCH_SIZE):
        q = ("FOR row IN @rows INSERT {_key: TO_STRING(row.id), id: row.id, "
             "date: row.date, year: row.year} INTO papers")
        return self._load(q, rows_iter, batch_size)

    def load_edges(self, rows_iter, batch_size=BATCH_SIZE):
        q = ("FOR row IN @rows INSERT {_from: CONCAT('papers/', row.src), "
             "_to: CONCAT('papers/', row.dst)} INTO cites")
        return self._load(q, rows_iter, batch_size)

    def _load(self, query, rows_iter, batch_size):
        per_batch, total = [], 0
        t0 = time.perf_counter()
        for batch in batched(rows_iter, batch_size):
            t = time.perf_counter()
            self.run(query, rows=batch)
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


# ------------------------------------------------------------------ construction
def from_env(platform: str):
    """Build an adapter from environment variables. Secrets never touch the repo."""
    p = platform.upper()
    if platform in ("cognodb", "neo4j", "memgraph", "aura"):
        uri = os.environ[f"{p}_URI"]
        return BoltAdapter(
            platform, uri, os.environ.get(f"{p}_USER", ""), os.environ.get(f"{p}_PASSWORD", "")
        )
    if platform == "arangodb":
        return ArangoAdapter(
            platform,
            os.environ.get("ARANGO_URL", "http://localhost:8529"),
            os.environ.get("ARANGO_USER", "root"),
            os.environ["ARANGO_PASSWORD"],
        )
    if platform == "falkordb":
        return FalkorAdapter(
            platform,
            os.environ.get("FALKORDB_HOST", "localhost"),
            os.environ.get("FALKORDB_PORT", "6380"),
            os.environ.get("FALKORDB_PASSWORD", ""),
        )
    raise SystemExit(f"no adapter for platform {platform!r}")
