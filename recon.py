"""Baseline recon: what procedures exist, and how much of our latency is just network."""
import os
import socket
import statistics
import time

from dotenv import load_dotenv
from neo4j import GraphDatabase

load_dotenv()

URI = os.environ["COGNODB_URI"]
HOST = URI.split("://")[1].split(":")[0]
AUTH = (os.environ["COGNODB_USER"], os.environ["COGNODB_PASSWORD"])


def tcp_rtt(host, port=7687, n=15):
    """Raw TCP connect time — the physical floor, no Bolt or TLS involved."""
    out = []
    for _ in range(n):
        s = socket.socket()
        s.settimeout(10)
        t = time.perf_counter()
        try:
            s.connect((host, port))
            out.append((time.perf_counter() - t) * 1000)
        except OSError:
            pass
        finally:
            s.close()
    return out


def pct(v, p):
    v = sorted(v)
    return v[min(int(len(v) * p / 100), len(v) - 1)]


def main():
    print(f"host: {HOST}")
    print(f"resolves to: {socket.gethostbyname(HOST)}")

    rtt = tcp_rtt(HOST)
    if rtt:
        print(f"raw TCP connect: p50={pct(rtt,50):.1f} ms  min={min(rtt):.1f} ms")

    with GraphDatabase.driver(URI, auth=AUTH) as driver:
        with driver.session() as s:
            print("\navailable procedures:")
            for r in s.run("CALL dbms.procedures() YIELD name"):
                print(f"  {r['name']}")

            # 100 no-op queries on a warm pooled connection = per-query overhead floor.
            # Every latency we report later sits on top of this number.
            s.run("RETURN 1").single()  # warm the pool
            noop = []
            for _ in range(100):
                t = time.perf_counter()
                s.run("RETURN 1 AS n").single()
                noop.append((time.perf_counter() - t) * 1000)

            print(
                f"\nRETURN 1 (warm, n=100): "
                f"p50={pct(noop,50):.1f}  p95={pct(noop,95):.1f}  "
                f"min={min(noop):.1f}  stdev={statistics.stdev(noop):.1f} ms"
            )
            print(
                "\n>>> This is the measurement floor. Any query latency we report on "
                "CognoDB\n    from this client is (real DB work + this number)."
            )


if __name__ == "__main__":
    main()
