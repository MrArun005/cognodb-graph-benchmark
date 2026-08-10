"""Benchmark CLI.

  python run.py baseline --platform cognodb
  python run.py load     --platform cognodb [--limit-edges N] [--wipe]

Every phase records a network baseline (100 no-op queries) alongside its results,
so any reported latency can be interpreted net of client<->server round trip.
"""
import argparse
import itertools
import platform as pyplatform
import socket
import time

from dotenv import load_dotenv

import bench

load_dotenv()


def network_baseline(db, n=100):
    """No-op query latency = the floor under every other measurement here.

    Measured on a reused session, exactly like the read workloads, so that
    subtracting this baseline from a workload p50 is apples-to-apples.
    """
    s = db.session()
    try:
        s.timed("RETURN 1 AS n")  # warm the pool
        lat = [s.timed("RETURN 1 AS n")[1] for _ in range(n)]
    finally:
        s.close()
    return bench.summarise(lat)


def environment():
    return {
        "client_host": socket.gethostname(),
        "client_platform": pyplatform.platform(),
        "python": pyplatform.python_version(),
        "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def cmd_baseline(args, db):
    base = network_baseline(db)
    r = bench.Result(args.platform, "baseline", {"environment": environment(), "noop": base})
    print(f"  no-op p50={base['p50_ms']} ms  p95={base['p95_ms']} ms  min={base['min_ms']} ms")
    return r


def cmd_load(args, db):
    caveats = []

    if args.wipe:
        print("  wiping existing data...")
        t = time.perf_counter()
        db.wipe()
        print(f"  wiped in {time.perf_counter() - t:.1f}s")

    n0, r0 = db.counts()
    if n0 or r0:
        raise SystemExit(f"refusing to load into non-empty graph ({n0} nodes, {r0} rels). Use --wipe")

    base = network_baseline(db)
    print(f"  network baseline: p50={base['p50_ms']} ms")

    print("  creating indexes...")
    idx = db.create_indexes()
    confirmed, note = db.await_indexes()
    if confirmed:
        print(f"  indexes ready ({note})")
    else:
        # Fall back to a fixed settle time and say so, rather than silently
        # assuming the index is live and reporting index-backed latencies.
        time.sleep(args.index_settle)
        caveats.append(
            f"Index readiness NOT verifiable on this platform ({note}); no "
            f"db.awaitIndexes and SHOW INDEXES.state is null. Waited "
            f"{args.index_settle}s after DDL before loading. Load and first-read "
            f"numbers may include index build cost."
        )
        print(f"  indexes NOT confirmable; waited {args.index_settle}s")
    index_readiness = {"confirmed": confirmed, "method": note}

    print("  loading nodes...")
    nodes = db.load_nodes(bench.read_nodes(), args.batch_size)
    print(f"    {nodes['rows']} nodes in {nodes['wall_s']}s = {nodes['rows_per_s']}/s")

    edges_iter = bench.read_edges()
    if args.limit_edges:
        edges_iter = itertools.islice(edges_iter, args.limit_edges)
        caveats.append(
            f"VALIDATION RUN: edges capped at {args.limit_edges}. Not a publishable number."
        )

    print("  loading relationships...")
    edges = db.load_edges(edges_iter, args.batch_size)
    print(f"    {edges['rows']} rels in {edges['wall_s']}s = {edges['rows_per_s']}/s")

    n1, r1 = db.counts()
    print(f"  verified in database: {n1} nodes, {r1} relationships")

    m = bench.manifest()
    expected_n = m["nodes"]
    expected_r = args.limit_edges or m["relationships"]
    if (n1, r1) != (expected_n, expected_r):
        caveats.append(
            f"COUNT MISMATCH: expected {expected_n} nodes / {expected_r} rels, "
            f"found {n1} / {r1}. Results are not trustworthy until resolved."
        )
        print(f"  !! MISMATCH: expected {expected_n}/{expected_r}")

    return bench.Result(
        args.platform,
        "load",
        {
            "environment": environment(),
            "network_baseline_noop": base,
            "dataset": {k: m[k] for k in ("dataset", "nodes", "relationships")},
            "index_creation_ms": idx,
            "index_readiness": index_readiness,
            "nodes": nodes,
            "relationships": edges,
            "total_wall_s": round(nodes["wall_s"] + edges["wall_s"], 3),
            "verified_counts": {"nodes": n1, "relationships": r1},
        },
        caveats,
    )


def _params_for(workload, starts, i):
    """Deterministic parameter selection: iteration i always gets the same value on
    every platform, so no platform benefits from an easier set of start nodes."""
    kind = bench.READ_WORKLOADS[workload]["param"]
    if kind == "start_node":
        return {"id": starts[i % len(starts)]}
    if kind == "year":
        return {"year": bench.FILTER_YEARS[i % len(bench.FILTER_YEARS)]}
    return {}


def cmd_read(args, db):
    starts = bench.start_nodes()
    n, r = db.counts()
    print(f"  graph under test: {n} nodes, {r} relationships")

    base = network_baseline(db)
    print(f"  network baseline: p50={base['p50_ms']} ms  (subtract to get server time)")

    out, cold = {}, {}
    sess = db.session()
    for name, spec in bench.READ_WORKLOADS.items():
        q = spec[db.dialect]

        # Cold: the very first execution, before any warm-up. Reported separately
        # because a single cold sample is not a percentile and must not be pooled
        # with warm measurements.
        try:
            _, cold_ms = sess.timed(q, **_params_for(name, starts, 0))
            cold[name] = round(cold_ms, 3)
        except Exception as e:
            cold[name] = f"FAILED: {type(e).__name__}"

        for i in range(args.warmup):
            sess.timed(q, **_params_for(name, starts, i))

        lat, failures = [], 0
        for i in range(args.iterations):
            try:
                _, ms = sess.timed(q, **_params_for(name, starts, i))
                lat.append(ms)
            except Exception:
                failures += 1

        s = bench.summarise(lat)
        s["failures"] = failures
        # NOT a process cold start: the connection is already established and
        # earlier workloads have warmed the engine. This is the first execution of
        # THIS query shape, so it captures query planning/compilation cost only.
        s["first_exec_of_query_ms"] = cold[name]
        if base["p50_ms"] is not None and s.get("p50_ms") is not None:
            s["p50_net_of_rtt_ms"] = round(s["p50_ms"] - base["p50_ms"], 3)
        out[name] = s
        print(f"    {name:16} p50={s.get('p50_ms')} p95={s.get('p95_ms')} "
              f"cold={cold[name]} fail={failures}")
    sess.close()

    caveats = []
    if base["p50_ms"] and base["p50_ms"] > 20:
        caveats.append(
            f"MEASUREMENT INVALID FOR PUBLICATION: network baseline p50 is "
            f"{base['p50_ms']} ms, which exceeds the server-side cost of most "
            f"workloads. Latency differences below ~{base['stdev_ms']} ms "
            f"(baseline stdev) are indistinguishable from network jitter. "
            f"Re-run from a client in the same region as the database."
        )
    return bench.Result(
        args.platform,
        "read",
        {
            "environment": environment(),
            "network_baseline_noop": base,
            "iterations": args.iterations,
            "warmup": args.warmup,
            "start_nodes_used": len(starts),
            "verified_counts": {"nodes": n, "relationships": r},
            "workloads": out,
        },
        caveats,
    )


def cmd_mixed(args, db):
    """Concurrent read/write throughput at each concurrency level in the sweep."""
    import threading

    starts = bench.start_nodes()
    levels = [int(x) for x in args.concurrency.split(",")]
    read_q = bench.READ_WORKLOADS[args.mixed_read][db.dialect]
    write_ratio = args.write_ratio

    base = network_baseline(db)
    print(f"  network baseline: p50={base['p50_ms']} ms")
    print(f"  read workload: {args.mixed_read}, write ratio: {write_ratio:.0%}")

    results = {}
    for clients in levels:
        stop = threading.Event()
        lat_r, lat_w, errors = [], [], []
        lock = threading.Lock()

        def worker(wid):
            local_r, local_w, local_e = [], [], 0
            i = 0
            # Each thread gets its own session; the driver pools connections.
            while not stop.is_set():
                is_write = (i % 100) < int(write_ratio * 100)
                try:
                    if is_write:
                        _, ms = db.timed(
                            bench.MIXED_WRITE,
                            id=starts[(wid + i) % len(starts)],
                            sid=f"{wid}-{i}",
                        )
                        local_w.append(ms)
                    else:
                        _, ms = db.timed(read_q, **_params_for(args.mixed_read, starts, wid + i))
                        local_r.append(ms)
                except Exception as e:
                    local_e += 1
                    if local_e < 3:
                        errors.append(f"{type(e).__name__}: {str(e)[:120]}")
                i += 1
            with lock:
                lat_r.extend(local_r)
                lat_w.extend(local_w)

        threads = [threading.Thread(target=worker, args=(w,)) for w in range(clients)]
        t0 = time.perf_counter()
        for t in threads:
            t.start()
        time.sleep(args.duration)
        stop.set()
        for t in threads:
            t.join(timeout=30)
        wall = time.perf_counter() - t0

        total = len(lat_r) + len(lat_w)
        results[str(clients)] = {
            "clients": clients,
            "duration_s": round(wall, 2),
            "reads": len(lat_r),
            "writes": len(lat_w),
            "total_ops": total,
            "qps": round(total / wall, 1),
            "read_latency": bench.summarise(lat_r),
            "write_latency": bench.summarise(lat_w),
            "errors": len(errors),
            "error_samples": errors[:3],
        }
        print(f"    clients={clients:3}  qps={results[str(clients)]['qps']:8}  "
              f"read p95={results[str(clients)]['read_latency'].get('p95_ms')}  "
              f"write p95={results[str(clients)]['write_latency'].get('p95_ms')}  "
              f"errors={len(errors)}")

    print("  cleaning up scratch writes...")
    removed = 0
    while True:
        rows = db.run(bench.MIXED_CLEANUP)
        c = rows[0]["c"] if rows else 0
        removed += c
        if not c:
            break
    n, r = db.counts()
    print(f"  removed {removed} scratch nodes; dataset now {n} nodes, {r} rels")

    caveats = []
    m = bench.manifest()
    if (n, r) != (m["nodes"], m["relationships"]):
        caveats.append(
            f"Dataset not restored after mixed workload: {n}/{r} vs expected "
            f"{m['nodes']}/{m['relationships']}."
        )
    if base["p50_ms"] and base["p50_ms"] > 20:
        caveats.append(
            f"Throughput is network-bound: baseline RTT p50 {base['p50_ms']} ms caps "
            f"per-client QPS at ~{1000 / base['p50_ms']:.1f}. Concurrency scaling here "
            f"measures the client's ability to hide RTT, not server capacity. "
            f"Re-run in-region."
        )
    return bench.Result(
        args.platform,
        "mixed",
        {
            "environment": environment(),
            "network_baseline_noop": base,
            "read_workload": args.mixed_read,
            "write_ratio": write_ratio,
            "duration_per_level_s": args.duration,
            "scratch_nodes_removed": removed,
            "verified_counts_after": {"nodes": n, "relationships": r},
            "levels": results,
        },
        caveats,
    )


COMMANDS = {
    "baseline": cmd_baseline,
    "load": cmd_load,
    "read": cmd_read,
    "mixed": cmd_mixed,
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=COMMANDS)
    ap.add_argument("--platform", required=True)
    ap.add_argument("--batch-size", type=int, default=bench.BATCH_SIZE)
    ap.add_argument("--limit-edges", type=int, default=0)
    ap.add_argument("--index-settle", type=float, default=3.0)
    ap.add_argument("--wipe", action="store_true")
    ap.add_argument("--tag", default="")
    ap.add_argument("--iterations", type=int, default=120,
                    help="measured iterations per read workload (brief suggests >=100)")
    ap.add_argument("--warmup", type=int, default=20)
    ap.add_argument("--concurrency", default="1,10,40",
                    help="comma-separated client counts for the mixed sweep")
    ap.add_argument("--duration", type=float, default=20.0,
                    help="seconds per concurrency level")
    ap.add_argument("--write-ratio", type=float, default=0.10)
    ap.add_argument("--mixed-read", default="hop2", choices=list(bench.READ_WORKLOADS))
    args = ap.parse_args()

    db = bench.from_env(args.platform)
    try:
        db.verify()
        print(f"[{args.platform}] {args.command}")
        result = COMMANDS[args.command](args, db)
        path = result.write(args.tag)
        print(f"  -> {path}")
        for c in result.caveats:
            print(f"  CAVEAT: {c}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
