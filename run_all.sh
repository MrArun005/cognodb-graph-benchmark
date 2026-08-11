#!/usr/bin/env bash
# One-command full benchmark: comparators up, load, read, mixed, footprint, report.
#
# Comparators are started and stopped ONE AT A TIME so no engine competes with
# another for the host's CPU while it is being measured.
set -euo pipefail

cd "$(dirname "$0")"
PY=.venv/bin/python
ITER=${ITER:-120}
WARMUP=${WARMUP:-20}
CONC=${CONC:-1,10,40}
DUR=${DUR:-12}
TAG=${TAG:-full}

# Unbuffered so progress is visible when piped to a log.
export PYTHONUNBUFFERED=1

if [ ! -f .env ]; then
  echo "ERROR: .env missing. Copy .env.example and fill in your credentials." >&2
  exit 1
fi
if [ ! -f data/MANIFEST.json ]; then
  echo "Canonical dataset missing; building it..." >&2
  mkdir -p data/raw
  ( cd data/raw
    [ -f cit-HepTh.txt.gz ]       || curl -sSLO https://snap.stanford.edu/data/cit-HepTh.txt.gz
    [ -f cit-HepTh-dates.txt.gz ] || curl -sSLO https://snap.stanford.edu/data/cit-HepTh-dates.txt.gz )
  $PY prep_dataset.py
fi

phases () {   # $1 = platform
  local p=$1
  echo "=== $p : load ==="  ; $PY run.py load  --platform "$p" --wipe --tag "$TAG"
  echo "=== $p : read ==="  ; $PY run.py read  --platform "$p" --iterations "$ITER" \
                                               --warmup "$WARMUP" --tag "$TAG"
  echo "=== $p : mixed ===" ; $PY run.py mixed --platform "$p" --concurrency "$CONC" \
                                               --duration "$DUR" --tag "$TAG"
}

# --- CognoDB (managed; nothing to start) ---------------------------------------
phases cognodb

# --- Self-hosted comparators, one container at a time --------------------------
for svc in neo4j memgraph falkordb arangodb; do
  echo "### starting $svc ###"
  docker compose up -d "$svc"
  # Poll the driver rather than a container healthcheck: it is the readiness that
  # actually matters and it works for every engine regardless of shipped tooling.
  for _ in $(seq 1 60); do
    if $PY run.py baseline --platform "$svc" >/dev/null 2>&1; then break; fi
    sleep 3
  done
  phases "$svc"
  # Footprint must be captured while this engine still holds the loaded dataset.
  $PY footprint.py >/dev/null || true
  echo "### stopping $svc ###"
  docker compose stop "$svc"
done

# Footprint needs every container up to fill in the whole table; bring them back
# briefly with their data intact, capture, then stop.
docker compose up -d neo4j memgraph falkordb arangodb
sleep 20
$PY footprint.py
docker compose stop neo4j memgraph falkordb arangodb

$PY report.py
echo
echo "Done. Results matrix: docs/RESULTS.md"
echo "NOTE: CognoDB footprint needs a manual console reading. Re-run:"
echo "  $PY footprint.py --cognodb-storage-mb <STORAGE from console> && $PY report.py"
