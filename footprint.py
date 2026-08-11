"""Capture the Footprint metric for every platform.

The brief asks for "whatever the platform exposes" and an explicit "not observable"
where it does not. Local containers expose both RSS and on-disk size; CognoDB
exposes node/relationship/storage counters in its web console only, which cannot be
scraped without a session, so those are read in manually via --cognodb-storage-mb.
"""
import argparse
import json
import subprocess
from pathlib import Path

import bench

# Where each engine keeps its persistent state inside the container.
DATA_DIRS = {
    "bench-neo4j": "/data",
    "bench-memgraph": "/var/lib/memgraph",
    "bench-falkordb": "/data",
    "bench-arangodb": "/var/lib/arangodb3",
}
CONTAINER_TO_PLATFORM = {
    "bench-neo4j": "neo4j",
    "bench-memgraph": "memgraph",
    "bench-falkordb": "falkordb",
    "bench-arangodb": "arangodb",
}


def sh(*cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=60).stdout.strip()
    except Exception:
        return ""


def container_footprint(name):
    running = sh("docker", "inspect", "-f", "{{.State.Status}}", name) == "running"
    if not running:
        return {"observable": False, "note": "container not running at capture time"}

    stats = sh("docker", "stats", "--no-stream", "--format",
               "{{.MemUsage}}|{{.MemPerc}}|{{.CPUPerc}}", name)
    mem_usage, mem_perc, cpu_perc = (stats.split("|") + ["", "", ""])[:3]

    disk_bytes = None
    raw = sh("docker", "exec", name, "du", "-sb", DATA_DIRS[name])
    if raw and raw.split():
        try:
            disk_bytes = int(raw.split()[0])
        except ValueError:
            pass

    return {
        "observable": True,
        "memory_usage": mem_usage,
        "memory_pct_of_cap": mem_perc,
        "cpu_pct_at_idle": cpu_perc,
        "data_dir": DATA_DIRS[name],
        "disk_bytes": disk_bytes,
        "disk_mb": round(disk_bytes / 1024 / 1024, 1) if disk_bytes else None,
        "method": "docker stats (RSS) + du -sb on the data directory",
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cognodb-storage-mb", type=float, default=None,
                    help="STORAGE value read from the CognoDB console with data loaded")
    ap.add_argument("--cognodb-baseline-mb", type=float, default=128.0,
                    help="console STORAGE when the instance held 0 nodes")
    args = ap.parse_args()

    m = bench.manifest()
    out = {
        "dataset": {"nodes": m["nodes"], "relationships": m["relationships"]},
        "platforms": {},
    }

    for container, platform in CONTAINER_TO_PLATFORM.items():
        out["platforms"][platform] = container_footprint(container)

    if args.cognodb_storage_mb is not None:
        used = args.cognodb_storage_mb - args.cognodb_baseline_mb
        cognodb = {
            "observable": True,
            "console_storage_mb": args.cognodb_storage_mb,
            "empty_instance_baseline_mb": args.cognodb_baseline_mb,
            "attributable_to_dataset_mb": round(used, 1),
            "memory_usage": "not observable — no host access, no dbms.* memory procedure",
            "method": "read manually from the CognoDB web console (no API endpoint)",
        }
    else:
        cognodb = {
            "observable": False,
            "note": ("Storage is shown in the CognoDB web console but there is no "
                     "public API for it, and the console needs an authenticated "
                     "session. Re-run with --cognodb-storage-mb <value> to record it. "
                     "Memory usage is NOT observable on any managed tier."),
        }
    out["platforms"]["cognodb"] = cognodb

    Path("results").mkdir(exist_ok=True)
    Path("results/footprint.json").write_text(json.dumps(out, indent=2))
    for p, v in out["platforms"].items():
        if v.get("observable"):
            print(f"  {p:10} mem={v.get('memory_usage', '-'):22} "
                  f"disk={v.get('disk_mb') or v.get('attributable_to_dataset_mb')} MB")
        else:
            print(f"  {p:10} NOT OBSERVABLE — {v.get('note', '')[:60]}")
    print("  -> results/footprint.json")


if __name__ == "__main__":
    main()
