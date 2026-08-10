# Platform specs — as advertised by each provider

Recorded verbatim from each platform's own console/docs. This table sets the
resource cap every other platform must be held to.

## CognoDB Cloud — free tier (c0)

Read from https://console.cognodb.com/instances/<instance-id> on 2026-08-10.

| Field | Value |
|---|---|
| Plan / size | Free · `c0` |
| Memory | **512 MB** |
| vCPU | burst to **0.5 vCPU** (shared burstable) |
| Storage | 1 GiB |
| Disk IOPS | up to 500 |
| Max connections | 200 |
| **Max result rows** | **50,000** |
| Region | us-east4 (Northern Virginia, GCP) |
| Engine version | v0.9.11 |
| Topology | Standalone |
| Monthly cost | $0 |
| Instance created | 2026-08-10 |

### Baseline footprint (empty graph)

| Metric | Value |
|---|---|
| Nodes | 0 |
| Relationships | 0 |
| Storage used | **128 MB / 1 GiB** |
| Connections | 0 |

**128 MB is consumed at zero nodes.** All reported data sizes are therefore
`observed_storage - 128 MB`, and usable space for the dataset is ~896 MB.

### Discrepancy noted

The assignment PDF states the free tier is **256 MB RAM**; both cognodb.com and
the live console state **512 MB**. We hold every comparator to the
console-verified **512 MB**, since that is what the instance under test
actually has. Documented here so the choice is auditable rather than silent.

### Engine capability probe (via Bolt)

CognoDB is Bolt-compatible but is **not** Neo4j. `dbms.components()`,
`SHOW PROCEDURES` and `db.schema.visualization()` are all absent. Exactly ten
procedures are registered:

```
apoc.create.relationship          gds.graph.project
db.index.fulltext.queryNodes      gds.graph.drop
db.index.vector.queryNodes        gds.louvain.stream
db.labels                         gds.shortestPath.dijkstra
db.relationshipTypes              dbms.procedures
```

Consequences for methodology:

- **No `apoc.periodic.iterate`** → bulk load must be driver-side `UNWIND`
  batching. This is a fairness *benefit*: driver batching is the one load
  method available identically on all five platforms.
- `SHOW INDEXES` / `SHOW CONSTRAINTS` work → indexed-lookup metric is measurable.
- Footprint is **not** observable via the driver, but **is** observable via the
  console (nodes, relationships, storage used).
- v0.9.11 is pre-1.0 software. Recorded as an honest caveat.

## Cap imposed on all comparators

| Resource | Cap |
|---|---|
| CPU | 0.5 vCPU (`--cpus=0.5`) |
| Memory | 512 MB (`--memory=512m`) |
| Disk | 1 GiB |
| Region | us-east4 |

Comparator specs are filled in as each is provisioned.
