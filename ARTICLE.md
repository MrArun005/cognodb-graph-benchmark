# I benchmarked five graph databases and accidentally measured the Indian Ocean

There is a moment in every benchmark where you should stop and check whether you
are measuring the thing you think you are measuring. Mine arrived like this:

```
1-hop traversal   245.98 ms
2-hop traversal   246.50 ms
3-hop traversal   307.57 ms
```

Look at the first two numbers. A 2-hop traversal visits everything a 1-hop
traversal visits, and then keeps going. It cannot be the same speed. Unless the
number in front of you isn't traversal latency at all.

It wasn't. It was the Arabian Sea, a stretch of the Indian Ocean, the Suez, most
of the Mediterranean, and the Atlantic.

---

## The speed of light is a benchmark participant

My CognoDB instance lived in Google's `us-east4` region — Ashburn, Virginia. I was
in Bengaluru. That's about 13,500 km as the crow flies and rather more as the
fibre runs.

So I measured the emptiest query I could think of:

```cypher
RETURN 1
```

That query does no work. No index, no scan, no disk. Its cost is pure round trip.

```
raw TCP connect       p50 = 245.8 ms
warm "RETURN 1"       p50 = 243.1 ms
```

Those two numbers being identical is the entire story. Opening a socket and
running a query cost the same, which means the database spent approximately zero
time on the query. Every millisecond was transit.

Now put that next to the real work. Once I got a client close to the engines, a
1-hop traversal on this dataset costs **0.33 ms**. A 3-hop costs **0.87 ms**.

I was trying to measure a 0.33 ms event with a 243 ms ruler that wobbled by
±15 ms. That is like timing a sprinter with a calendar.

**The first thing any benchmark should measure is its own measurement error.** If
you don't know your noise floor, you don't have results — you have decoration. My
harness now records a no-op baseline before every single phase and refuses to call
its own output publishable when the floor is too high:

```
CAVEAT: MEASUREMENT INVALID FOR PUBLICATION: network baseline p50 is 244.686 ms,
which exceeds the server-side cost of most workloads.
```

I would rather ship a benchmark that tells you when to distrust it.

---

## So what happens when you *can* see straight?

I ran the four self-hosted comparators — Neo4j, Memgraph, FalkorDB and ArangoDB —
in containers on the same machine as the client, every one of them pinned to
exactly what CognoDB's free tier gets: **half a vCPU and 512 MB of RAM**. Same
dataset (352,768 citations between 27,769 physics papers), same queries, same
pre-sampled start nodes.

Here is the table everyone wants:

| Workload | Neo4j | Memgraph | FalkorDB | ArangoDB |
|---|---|---|---|---|
| 1-hop p50 | 1.49 ms | 0.36 ms | **0.33 ms** | 0.84 ms |
| 3-hop p50 | 3.89 ms | 1.28 ms | **0.87 ms** | 13.50 ms |
| Ingest (rels/s) | 12,154 | **96,325** | 30,845 | 6,210 |

And here is the table that actually explains it:

| | Memory used | % of cap | median p95/p50 | worst p95/p50 |
|---|---|---|---|---|
| Neo4j | 503 MiB | **98%** | **3.6×** | 31.5× |
| ArangoDB | 383 MiB | 75% | 2.5× | 11.6× |
| Memgraph | 144 MiB | 28% | 1.5× | 6.0× |
| FalkorDB | 128 MiB | 25% | **1.3×** | 12.3× |

Read the memory column, then the tail column. They are in the same order.

That is not an engine being slow. That is an engine being *strangled*. A JVM at
98% of its ceiling garbage-collects constantly, and every collection lands on some
unlucky query.

Here is where I have to correct myself in public. My first run measured Neo4j's
1-hop p95 at **52×** its median, and I wrote a confident paragraph about it. Then I
re-ran the whole thing. 1-hop came back at 2.3× — and 3-hop spiked to 31× instead.
The pauses are real and they are big. But *which query gets hit is random*, which
is exactly what you'd expect from garbage collection and exactly what a
single-workload headline number hides. One sample is an anecdote, even when it's
your own.

Look at the very first write batches of the load, too:

```
Neo4j, first 4 batches:  774.4 ms   195.9 ms   104.8 ms   492.8 ms
Neo4j, last 3 batches:    14.8 ms    15.5 ms    14.3 ms
```

A 774 ms first batch settling to 15 ms — a 52× warm-up ramp. If you had
benchmarked Neo4j with a small dataset, you'd have measured mostly JVM warm-up
and concluded something wildly untrue.

And here is the uncomfortable part. I ran that same load twice. The first time
Neo4j managed 7,334 rels/s; the second, 12,154. Memgraph went from 73,460 to
96,325. Same hardware, same data, same code — swings of 66% and 31%. Any ranking
that depends on differences smaller than that is noise wearing a table.

**The lesson isn't "Neo4j is bad."** Give it 4 GB and this inverts. The lesson is
that on a small tier, *the binding constraint is memory headroom, not engine
design* — and a median tells you nothing about that. Your users don't feel your
p50. They feel your p95.

---

## The concurrency scaling that wasn't

The brief asked for a concurrency sweep: 1, 10, and 40 clients. Here's Memgraph:

| Clients | QPS |
|---|---|
| 1 | 2,587 |
| 10 | 2,364 |
| 40 | 2,275 |

It got *slower*. Not because of a bug — because of arithmetic. Half a vCPU is
saturated by the first client. Clients two through forty aren't adding capacity,
they're queueing for the same half core and paying scheduler overhead.

Meanwhile CognoDB, measured from 13,500 km away, scaled beautifully:

| Clients | QPS |
|---|---|
| 1 | 3.3 |
| 10 | 35.1 |
| 40 | 118.0 |

Near-linear! Almost 36× from 1 to 40 clients! And it means nothing about the
database. Each client spends 243 ms waiting on the network, doing nothing. Adding
clients just fills that dead air. **That curve measures my ability to hide
latency, not CognoDB's ability to serve.**

Two platforms, two beautiful graphs, two completely different phenomena, and a
benchmark that didn't record its baseline would have presented both as
"throughput scaling."

FalkorDB gave the most honest answer of the lot: it ships with a 25-query queue
limit, so at 40 clients it dropped to 347 QPS and started refusing work. My
harness originally *hung* there — a shared Redis socket across 40 threads
deadlocked. Adding a socket timeout turned a silent hang into 13 recorded errors,
which is a result. A benchmark that hangs is a benchmark that lies by omission.

---

## Query languages disagree about what "the same query" means

I deliberately included ArangoDB, which speaks AQL rather than Cypher. It cost me
an entire second adapter and a hand-written query translation. It was worth every
minute, because of this:

```cypher
-- Cypher: labels on both endpoints force a real scan
MATCH (:Paper)-[:CITES]->(:Paper) RETURN count(*)
```

```aql
// AQL: answerable from collection metadata
RETURN COUNT(FOR e IN cites RETURN 1)
```

ArangoDB: **17 ms**. The Cypher engines: 85–185 ms. CognoDB: ~1,613 ms.

Same question. Completely different amount of work. ArangoDB didn't win that row;
it answered a different row. It's flagged `not_comparable` in the code itself, so
nobody can quote it innocently.

I'll admit a wrong prediction here too. Before running it, I said Neo4j would
answer that query from its count-store in about a millisecond. It took 185 ms —
the count-store only applies to *unlabelled* patterns, and I'd put labels on both
ends. I was confidently wrong and the measurement corrected me. That is what
measurements are for, and pretending otherwise is how benchmarks become marketing.

---

## What I still can't tell you about CognoDB

This is the part most benchmark posts skip.

**I do not know CognoDB's traversal latency.** Not "it was slow" — I don't know.
The work costs single-digit milliseconds; my ruler had 15 ms of jitter. Any number
I published would be fiction with error bars.

What I *can* say, because it clears the noise floor by an order of magnitude:

- Its full relationship count takes ~1,613 ms of server-side time, against 17–185
  ms for the self-hosted engines. No count-store-style shortcut.
- Corrected for round trip, its ingest implies ~30,000 rels/s of real capability —
  which would put it between FalkorDB and Memgraph. That's an inference, not a
  measurement, and I've labelled it as one.
- It completed **every workload with zero failures**, including a full 352,768-
  relationship load and a 40-client concurrent sweep. Across 353 consecutive write
  batches there was no throttling cliff — burst credits held the whole way.
- It runs the official Neo4j driver unmodified. It is also emphatically *not*
  Neo4j: ten procedures registered, no `apoc.periodic.iterate`, no
  `db.awaitIndexes()`, a hard 50,000-row result cap. But vector indexes,
  full-text indexes, Louvain and Dijkstra are all there on a 512 MB tier, which
  tells you what it's actually built for.

The fix for the gap is one environment variable and one re-run from a VM in
`us-east4`. The harness was written for that from the start.

---

## Five things I'd tell anyone benchmarking a database

1. **Measure your noise floor first.** A no-op query, a hundred times. If it's
   bigger than the thing you're timing, stop and move the client.
2. **Publish p95, not just p50.** The median is the number that flatters you. The
   tail is the number your users experience.
3. **Check the resource ceiling before blaming the engine.** 98% heap utilisation
   explained more of my results than any architectural difference did.
4. **Never let a "same query" claim go unexamined across query languages.**
   Equivalent syntax is not equivalent cost.
5. **Make the harness refuse to lie.** Mine stamps its own output as unpublishable
   when the baseline is too high, flags non-comparable workloads in the code, and
   verifies row counts after every load. Honesty is a feature you implement, not
   an intention you have.

Nobody wins a benchmark run on a free tier from the wrong continent. But you can
still learn a great deal — mostly about benchmarking.

---

*Full harness, raw JSON, every caveat, and one-command reproduction:
[the repository](./README.md). Dataset: [SNAP
cit-HepTh](https://snap.stanford.edu/data/cit-HepTh.html), 27,769 nodes and
352,768 relationships, SHA-256 pinned.*
