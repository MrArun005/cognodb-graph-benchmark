"""Connectivity smoke test for CognoDB Cloud. Run before anything else."""
import os
import time

from dotenv import load_dotenv
from neo4j import GraphDatabase

load_dotenv()

URI = os.environ["COGNODB_URI"]
AUTH = (os.environ["COGNODB_USER"], os.environ["COGNODB_PASSWORD"])


def main():
    t0 = time.perf_counter()
    with GraphDatabase.driver(URI, auth=AUTH) as driver:
        driver.verify_connectivity()
        print(f"connected in {(time.perf_counter() - t0) * 1000:.0f} ms")

        with driver.session() as s:
            # CognoDB is Bolt-compatible but not Neo4j: most dbms.* procs are absent.
            # Probe which introspection surfaces exist — decides how we measure footprint.
            for probe in (
                "CALL dbms.components() YIELD name, versions, edition",
                "CALL dbms.procedures() YIELD name RETURN count(name) AS procs",
                "SHOW PROCEDURES YIELD name RETURN count(name) AS procs",
                "SHOW INDEXES",
                "SHOW CONSTRAINTS",
                "CALL db.labels()",
                "CALL db.schema.visualization()",
            ):
                try:
                    rows = [dict(r) for r in s.run(probe)]
                    print(f"  OK   {probe[:46]:46} -> {str(rows)[:90]}")
                except Exception as e:
                    print(f"  FAIL {probe[:46]:46} -> {type(e).__name__}")

            # round-trip latency, 10 samples
            lat = []
            for _ in range(10):
                t = time.perf_counter()
                s.run("RETURN 1 AS n").single()
                lat.append((time.perf_counter() - t) * 1000)
            lat.sort()
            print(f"RETURN 1  p50={lat[4]:.1f} ms  p95={lat[-1]:.1f} ms")

            n = s.run("MATCH (n) RETURN count(n) AS c").single()["c"]
            r = s.run("MATCH ()-[r]->() RETURN count(r) AS c").single()["c"]
            print(f"existing graph: {n} nodes, {r} relationships")


if __name__ == "__main__":
    main()
