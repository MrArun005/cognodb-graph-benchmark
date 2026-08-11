"""Generate docs/RESULTS.md — every required metric, for every platform, from the
JSON emitted by run.py. Nothing here is hand-typed: if a number is missing the
table says so rather than leaving a plausible-looking blank.
"""
import json
from pathlib import Path

RESULTS = Path("results")
PLATFORMS = ["cognodb", "neo4j", "memgraph", "falkordb", "arangodb"]
LABELS = {
    "cognodb": "CognoDB c0",
    "neo4j": "Neo4j 5.26",
    "memgraph": "Memgraph 2.18",
    "falkordb": "FalkorDB 4.2",
    "arangodb": "ArangoDB 3.11",
}
WORKLOADS = ["hop1", "hop2", "hop3", "point_lookup", "indexed_filter",
             "agg_group_by", "agg_rel_count"]
WL_LABELS = {
    "hop1": "1-hop traversal",
    "hop2": "2-hop traversal",
    "hop3": "3-hop traversal",
    "point_lookup": "Point lookup (indexed id)",
    "indexed_filter": "Indexed filter (year)",
    "agg_group_by": "Aggregation (group-by year)",
    "agg_rel_count": "Aggregation (count all rels)",
}


def load(platform, phase, tag="full"):
    for name in (f"{platform}_{phase}_{tag}.json", f"{platform}_{phase}.json"):
        p = RESULTS / name
        if p.exists():
            return json.loads(p.read_text())
    return None


def fmt(v, suffix=""):
    if v is None:
        return "n/a"
    if isinstance(v, (int, float)):
        return f"{v:,.2f}{suffix}" if isinstance(v, float) else f"{v:,}{suffix}"
    return str(v)


def table(headers, rows):
    out = ["| " + " | ".join(headers) + " |",
           "|" + "|".join(["---"] * len(headers)) + "|"]
    for r in rows:
        out.append("| " + " | ".join(str(c) for c in r) + " |")
    return "\n".join(out) + "\n"


def section_baseline(md):
    md.append("## 0. Measurement floor (client → server no-op latency)\n")
    md.append("Every latency below sits on top of this number. It is measured with "
              "the same reused session as the workloads, so it can be subtracted.\n")
    rows = []
    for p in PLATFORMS:
        d = load(p, "read") or load(p, "load")
        if not d:
            rows.append([LABELS[p], "not run", "-", "-", "-"])
            continue
        b = d["metrics"].get("network_baseline_noop", {})
        rows.append([LABELS[p], fmt(b.get("p50_ms")), fmt(b.get("p95_ms")),
                     fmt(b.get("min_ms")), fmt(b.get("stdev_ms"))])
    md.append(table(["Platform", "no-op p50 (ms)", "p95", "min", "stdev"], rows))


def section_ingest(md):
    md.append("## 1. Data loading — ingest throughput\n")
    md.append(f"Identical canonical dataset, identical batch size, driver-side "
              f"`UNWIND` batching on every platform.\n")
    rows = []
    for p in PLATFORMS:
        d = load(p, "load")
        if not d:
            rows.append([LABELS[p], "not run", "-", "-", "-", "-"])
            continue
        m = d["metrics"]
        n, r = m["nodes"], m["relationships"]
        rows.append([
            LABELS[p],
            fmt(n["rows_per_s"]), fmt(r["rows_per_s"]),
            fmt(n["wall_s"], " s"), fmt(r["wall_s"], " s"),
            fmt(m.get("total_wall_s"), " s"),
        ])
    md.append(table(["Platform", "Nodes/s", "Rels/s", "Node load", "Rel load",
                     "Total wall clock"], rows))

    md.append("\n### Ingest stability (per-batch latency, 1,000 rows per batch)\n")
    rows = []
    for p in PLATFORMS:
        d = load(p, "load")
        if not d:
            continue
        b = d["metrics"]["relationships"]["batch_latency"]
        first = d["metrics"]["relationships"]["first_10_batches_ms"][:3]
        last = d["metrics"]["relationships"]["last_10_batches_ms"][-3:]
        rows.append([LABELS[p], fmt(b.get("p50_ms")), fmt(b.get("p95_ms")),
                     fmt(b.get("max_ms")), str(first), str(last)])
    md.append(table(["Platform", "batch p50 (ms)", "p95", "max",
                     "first 3 batches", "last 3 batches"], rows))


def section_reads(md):
    md.append("\n## 2. Read workloads — p50 / p95 latency (ms)\n")
    md.append("120 measured iterations after 20 warm-up iterations, per workload, "
              "per platform. Start nodes are the same 200 pre-sampled nodes "
              "everywhere (seed 42, out-degree ≥ 3).\n")

    for stat, title in (("p50_ms", "p50"), ("p95_ms", "p95")):
        md.append(f"\n### {title} latency (ms)\n")
        rows = []
        for w in WORKLOADS:
            row = [WL_LABELS[w]]
            for p in PLATFORMS:
                d = load(p, "read")
                if not d:
                    row.append("not run")
                    continue
                wl = d["metrics"]["workloads"].get(w, {})
                row.append(fmt(wl.get(stat)))
            rows.append(row)
        md.append(table(["Workload"] + [LABELS[p] for p in PLATFORMS], rows))

    md.append("\n### First execution of each query shape (ms)\n")
    md.append("Plan-compilation cost on a warm connection — **not** a process "
              "cold start. Reported separately from the warm percentiles above.\n")
    rows = []
    for w in WORKLOADS:
        row = [WL_LABELS[w]]
        for p in PLATFORMS:
            d = load(p, "read")
            row.append(fmt((d["metrics"]["workloads"].get(w, {}) or {})
                           .get("first_exec_of_query_ms")) if d else "not run")
        rows.append(row)
    md.append(table(["Workload"] + [LABELS[p] for p in PLATFORMS], rows))

    md.append("\n### Failed iterations\n")
    rows = []
    for p in PLATFORMS:
        d = load(p, "read")
        if not d:
            continue
        tot = sum((d["metrics"]["workloads"].get(w, {}) or {}).get("failures", 0)
                  for w in WORKLOADS)
        rows.append([LABELS[p], tot, d["metrics"].get("iterations")])
    md.append(table(["Platform", "Total failures", "Iterations per workload"], rows))


def section_mixed(md):
    md.append("\n## 3. Mixed workload — concurrent read/write throughput\n")
    md.append("2-hop traversal reads with a 10% write mix. Writes target a "
              "dedicated `:Scratch` label so the benchmark dataset is never "
              "mutated; counts are re-verified after every sweep.\n")
    rows = []
    for p in PLATFORMS:
        d = load(p, "mixed")
        if not d:
            rows.append([LABELS[p], "not run", "-", "-", "-", "-", "-"])
            continue
        for lvl, v in sorted(d["metrics"]["levels"].items(), key=lambda kv: int(kv[0])):
            rows.append([
                LABELS[p], v["clients"], fmt(v["qps"]),
                fmt(v["read_latency"].get("p95_ms")),
                fmt(v["write_latency"].get("p95_ms")),
                v["reads"], v["writes"], v["errors"],
            ])
    md.append(table(["Platform", "Clients", "QPS", "read p95 (ms)",
                     "write p95 (ms)", "reads", "writes", "errors"], rows))


def section_footprint(md):
    md.append("\n## 4. Footprint\n")
    p = RESULTS / "footprint.json"
    if not p.exists():
        md.append("_Not captured._\n")
        return
    fp = json.loads(p.read_text())
    rows = []
    for name in PLATFORMS:
        v = fp["platforms"].get(name, {})
        if not v.get("observable"):
            rows.append([LABELS[name], "not observable", "not observable",
                         v.get("note", "")[:70]])
            continue
        rows.append([
            LABELS[name],
            v.get("memory_usage", "-"),
            (f"{v.get('disk_mb')} MB" if v.get("disk_mb") is not None
             else f"{v.get('attributable_to_dataset_mb')} MB (console)"),
            v.get("method", "")[:70],
        ])
    md.append(table(["Platform", "Memory (RSS)", "On-disk", "How measured"], rows))


def section_caveats(md):
    md.append("\n## 5. Caveats recorded by the harness\n")
    md.append("These are emitted programmatically during the runs, not written "
              "afterwards.\n")
    any_found = False
    for p in PLATFORMS:
        items = []
        for phase in ("load", "read", "mixed"):
            d = load(p, phase)
            if d:
                items += [f"**{phase}** — {c}" for c in d.get("caveats", [])]
        if items:
            any_found = True
            md.append(f"\n### {LABELS[p]}\n")
            for c in items:
                md.append(f"- {c}\n")
    if not any_found:
        md.append("_None recorded._\n")


def main():
    md = ["# Results matrix\n",
          "_Generated by `report.py` from the JSON in `results/`. "
          "Do not edit by hand._\n\n"]
    section_baseline(md)
    section_ingest(md)
    section_reads(md)
    section_mixed(md)
    section_footprint(md)
    section_caveats(md)

    Path("docs").mkdir(exist_ok=True)
    out = Path("docs/RESULTS.md")
    out.write_text("".join(md))
    print(f"  -> {out} ({len(''.join(md).splitlines())} lines)")


if __name__ == "__main__":
    main()
