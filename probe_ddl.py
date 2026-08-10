"""Probe which DDL / query syntax CognoDB v0.9.11 accepts.

Determines the loader design. Runs against a scratch label so it cannot disturb
benchmark data, and cleans up after itself.
"""
import os

from dotenv import load_dotenv
from neo4j import GraphDatabase

load_dotenv()
URI = os.environ["COGNODB_URI"]
AUTH = (os.environ["COGNODB_USER"], os.environ["COGNODB_PASSWORD"])

PROBES = [
    ("index (IF NOT EXISTS, named)",
     "CREATE INDEX probe_idx IF NOT EXISTS FOR (p:ProbeTmp) ON (p.pid)"),
    ("index (unnamed, legacy)", "CREATE INDEX ON :ProbeTmp2(pid)"),
    ("unique constraint",
     "CREATE CONSTRAINT probe_uq IF NOT EXISTS FOR (p:ProbeTmp) REQUIRE p.pid IS UNIQUE"),
    ("UNWIND + CREATE",
     "UNWIND [{pid:1},{pid:2}] AS row CREATE (p:ProbeTmp {pid: row.pid})"),
    ("UNWIND + MERGE",
     "UNWIND [{pid:3}] AS row MERGE (p:ProbeTmp {pid: row.pid})"),
    ("UNWIND + MATCH + CREATE rel",
     "UNWIND [{a:1,b:2}] AS row MATCH (x:ProbeTmp {pid:row.a}), (y:ProbeTmp {pid:row.b}) "
     "CREATE (x)-[:PROBE_REL]->(y)"),
    ("parameterised UNWIND ($rows)",
     "UNWIND $rows AS row CREATE (p:ProbeTmp {pid: row.pid})"),
    ("EXPLAIN", "EXPLAIN MATCH (p:ProbeTmp) RETURN p"),
    ("PROFILE", "PROFILE MATCH (p:ProbeTmp) RETURN count(p)"),
    ("variable-length 1..3", "MATCH (p:ProbeTmp)-[:PROBE_REL*1..3]->(q) RETURN count(q)"),
    ("shortestPath", "MATCH (a:ProbeTmp{pid:1}),(b:ProbeTmp{pid:2}), "
                     "p=shortestPath((a)-[:PROBE_REL*..5]->(b)) RETURN length(p)"),
    ("count store aggregate", "MATCH (p:ProbeTmp) RETURN count(p) AS c"),
    ("group-by aggregate",
     "MATCH (p:ProbeTmp) RETURN p.pid AS k, count(*) AS c ORDER BY c DESC LIMIT 5"),
    ("CALL {} subquery", "CALL { MATCH (p:ProbeTmp) RETURN count(p) AS c } RETURN c"),
    ("USING PERIODIC COMMIT", "USING PERIODIC COMMIT 100 UNWIND [1] AS x RETURN x"),
    ("db.awaitIndexes", "CALL db.awaitIndexes()"),
    ("SHOW INDEXES verbose", "SHOW INDEXES YIELD name, state, populationPercent"),
]


def main():
    with GraphDatabase.driver(URI, auth=AUTH) as driver:
        with driver.session() as s:
            for label, q in PROBES:
                try:
                    params = {"rows": [{"pid": 99}]} if "$rows" in q else {}
                    rows = [dict(r) for r in s.run(q, **params)]
                    print(f"  OK   {label:34} {str(rows)[:70]}")
                except Exception as e:
                    msg = str(e).split("}")[-1].strip()[:80]
                    print(f"  FAIL {label:34} {type(e).__name__}: {msg}")

            print("\ncleanup:")
            for q in (
                "MATCH (p:ProbeTmp) DETACH DELETE p",
                "MATCH (p:ProbeTmp2) DETACH DELETE p",
                "DROP INDEX probe_idx IF EXISTS",
                "DROP CONSTRAINT probe_uq IF EXISTS",
            ):
                try:
                    s.run(q).consume()
                    print(f"  OK   {q}")
                except Exception as e:
                    print(f"  FAIL {q} -> {type(e).__name__}")

            n = s.run("MATCH (n) RETURN count(n) AS c").single()["c"]
            print(f"\nnodes remaining (must be 0): {n}")


if __name__ == "__main__":
    main()
