"""Generate the README charts from results/*.json.

Palette is the validated 5-slot categorical set (adjacent-pair CVD ΔE 9.1, normal
vision 19.6, both passing). Three slots sit below 3:1 contrast on the light
surface, so the relief rule applies: every mark carries a visible value label.
Colour follows the platform, never its rank, so a chart with fewer platforms does
not repaint the survivors.
"""
import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import FixedLocator, FuncFormatter

RESULTS = Path("results")
OUT = Path("docs/charts")

SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK2 = "#52514e"
GRID = "#e3e2df"

# Fixed slot order — never cycled, never reassigned by rank.
COLOR = {
    "cognodb": "#2a78d6",
    "neo4j": "#eb6834",
    "memgraph": "#1baf7a",
    "falkordb": "#eda100",
    "arangodb": "#e87ba4",
}
LABEL = {
    "cognodb": "CognoDB c0",
    "neo4j": "Neo4j 5.26",
    "memgraph": "Memgraph 2.18",
    "falkordb": "FalkorDB 4.2",
    "arangodb": "ArangoDB 3.11",
}
ORDER = ["cognodb", "neo4j", "memgraph", "falkordb", "arangodb"]


def load(platform, phase, tag="full"):
    for n in (f"{platform}_{phase}_{tag}.json", f"{platform}_{phase}.json"):
        p = RESULTS / n
        if p.exists():
            return json.loads(p.read_text())
    return None


def style(ax, title, subtitle, ylabel):
    ax.set_facecolor(SURFACE)
    ax.figure.patch.set_facecolor(SURFACE)
    ax.set_title(title, color=INK, fontsize=13, fontweight="600", loc="left", pad=22)
    ax.text(0, 1.02, subtitle, transform=ax.transAxes, color=INK2, fontsize=9.5,
            va="bottom", ha="left")
    ax.set_ylabel(ylabel, color=INK2, fontsize=9.5)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    for s in ("top", "right", "left"):
        ax.spines[s].set_visible(False)
    ax.spines["bottom"].set_color(GRID)
    ax.tick_params(colors=INK2, labelsize=9, length=0)
    ax.tick_params(axis="both", which="minor", length=0)
    ax.yaxis.set_minor_formatter(FuncFormatter(lambda x, _: ""))


def save(fig, name):
    OUT.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT / name, dpi=170, bbox_inches="tight", facecolor=SURFACE)
    plt.close(fig)
    print(f"  -> {OUT / name}")


def chart_ingest():
    rows = []
    for p in ORDER:
        d = load(p, "load")
        if d:
            rows.append((p, d["metrics"]["relationships"]["rows_per_s"]))
    if not rows:
        return
    fig, ax = plt.subplots(figsize=(8, 4.2))
    for i, (p, v) in enumerate(rows):
        ax.bar(i, v, width=0.55, color=COLOR[p], zorder=3)
        ax.text(i, v * 1.06, f"{v:,.0f}", ha="center", va="bottom",
                color=INK, fontsize=9.5, fontweight="600")
    ax.set_xticks(range(len(rows)))
    ax.set_xticklabels([LABEL[p] for p, _ in rows], fontsize=9)
    ax.set_yscale("log")
    ax.set_ylim(1e3, max(v for _, v in rows) * 2.4)
    ax.yaxis.set_major_formatter(FuncFormatter(lambda x, _: f"{x:,.0f}"))
    style(ax, "Ingest throughput — relationships per second",
          "352,768 relationships, driver-side UNWIND batching (1,000/batch). "
          "Log scale. Higher is better.", "relationships / second (log)")
    save(fig, "ingest_throughput.png")


def chart_traversal():
    hops = ["hop1", "hop2", "hop3"]
    series = {}
    for p in ORDER:
        d = load(p, "read")
        if not d:
            continue
        vals = [(d["metrics"]["workloads"].get(h) or {}).get("p50_ms") for h in hops]
        if all(v is not None for v in vals):
            series[p] = vals
    if not series:
        return
    fig, ax = plt.subplots(figsize=(8.6, 4.4))
    n = len(series)
    w = 0.8 / n
    for i, (p, vals) in enumerate(series.items()):
        xs = [j + (i - (n - 1) / 2) * w for j in range(len(hops))]
        # 2px surface gap between adjacent fills, per the mark spec.
        ax.bar(xs, vals, width=w * 0.86, color=COLOR[p], label=LABEL[p], zorder=3)
        for x, v in zip(xs, vals):
            ax.text(x, v * 1.08, f"{v:.2f}", ha="center", va="bottom",
                    color=INK, fontsize=7.4, rotation=90)
    ax.set_xticks(range(len(hops)))
    ax.set_xticklabels(["1 hop", "2 hops", "3 hops"], fontsize=10)
    ax.set_yscale("log")
    ax.yaxis.set_major_formatter(FuncFormatter(lambda x, _: f"{x:g}"))
    style(ax, "Traversal latency by hop depth (p50)",
          "120 iterations after 20 warm-up, same 200 start nodes on every platform. "
          "Log scale. Lower is better.", "p50 latency, ms (log)")
    leg = ax.legend(frameon=False, fontsize=9, ncol=3, loc="upper left")
    for t in leg.get_texts():
        t.set_color(INK2)
    save(fig, "traversal_scaling.png")


def chart_tails():
    """The tail story: median vs p95 on the same workload. This is the chart that
    explains why a p50-only comparison would mislead."""
    rows = []
    for p in ORDER:
        d = load(p, "read")
        if not d:
            continue
        # hop1 is the workload the README quotes; keeping them consistent matters
        # more than picking whichever panel looks most dramatic.
        wl = d["metrics"]["workloads"].get("hop1") or {}
        if wl.get("p50_ms") and wl.get("p95_ms"):
            rows.append((p, wl["p50_ms"], wl["p95_ms"]))
    if not rows:
        return
    fig, ax = plt.subplots(figsize=(8.6, 4.4))
    top = max(v for _, _, v in rows)
    for i, (p, p50, p95) in enumerate(rows):
        # p50 solid, p95 hatched — identity is never colour-alone.
        ax.bar(i - 0.17, p50, width=0.30, color=COLOR[p], zorder=3)
        ax.bar(i + 0.17, p95, width=0.30, color=COLOR[p], zorder=3,
               hatch="////", edgecolor=SURFACE, linewidth=1.4)
        ax.text(i - 0.17, p50 * 1.1, f"{p50:.2f}", ha="center", va="bottom",
                color=INK, fontsize=8)
        ax.text(i + 0.17, p95 * 1.1, f"{p95:.1f}", ha="center", va="bottom",
                color=INK, fontsize=8, fontweight="600")
        # Ratio annotated at a constant height so the eye can compare them.
        ratio = p95 / p50
        rtxt = f"{ratio:.0f}x" if ratio >= 10 else f"{ratio:.1f}x"
        ax.text(i, top * 2.0, f"{rtxt} tail", ha="center",
                va="bottom", color=INK2, fontsize=9)
    ax.set_xticks(range(len(rows)))
    ax.set_xticklabels([LABEL[p] for p, _, _ in rows], fontsize=9)
    ax.set_yscale("log")
    ax.yaxis.set_major_formatter(FuncFormatter(lambda x, _: f"{x:g}"))
    ax.yaxis.set_minor_formatter(FuncFormatter(lambda x, _: ""))
    ax.tick_params(axis="y", which="minor", length=0)
    ax.set_ylim(top=top * 4.5)
    style(ax, "Median vs tail latency — 1-hop traversal",
          "Solid = p50, hatched = p95. The multiplier is p95/p50: how much worse "
          "the tail is than the median. Log scale.", "latency, ms (log)")
    save(fig, "tail_latency.png")


def chart_concurrency():
    fig, ax = plt.subplots(figsize=(8.4, 4.4))
    plotted = False
    for p in ORDER:
        d = load(p, "mixed")
        if not d:
            continue
        lv = d["metrics"]["levels"]
        xs = sorted(int(k) for k in lv)
        ys = [lv[str(x)]["qps"] for x in xs]
        ax.plot(xs, ys, color=COLOR[p], linewidth=2, marker="o", markersize=7,
                markeredgecolor=SURFACE, markeredgewidth=1.6, label=LABEL[p], zorder=3)
        # Direct label at the line end — identity without relying on the legend.
        ax.text(xs[-1] * 1.06, ys[-1], f" {LABEL[p]}  {ys[-1]:,.0f}",
                color=INK, fontsize=8.5, va="center")
        plotted = True
    if not plotted:
        plt.close(fig)
        return
    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.xaxis.set_major_locator(FixedLocator([1, 10, 40]))
    ax.xaxis.set_major_formatter(FuncFormatter(lambda x, _: f"{x:g}"))
    ax.yaxis.set_major_formatter(FuncFormatter(lambda x, _: f"{x:,.0f}"))
    ax.set_xlim(0.85, 130)
    ax.set_xlabel("concurrent clients (log)", color=INK2, fontsize=9.5)
    style(ax, "Mixed workload throughput vs client concurrency",
          "2-hop reads with a 10% write mix, 0.5 vCPU cap on every platform. "
          "A flat or falling line means the CPU cap, not the client, is the limit.",
          "sustained queries / second (log)")
    save(fig, "concurrency_sweep.png")


def chart_footprint():
    p = RESULTS / "footprint.json"
    if not p.exists():
        return
    fp = json.loads(p.read_text())["platforms"]
    rows = []
    for name in ORDER:
        v = fp.get(name, {})
        mem = v.get("memory_usage", "")
        if v.get("observable") and "/" in str(mem):
            used = float(str(mem).split("/")[0].replace("MiB", "").strip())
            rows.append((name, used, used / 512 * 100))
    if not rows:
        return
    fig, ax = plt.subplots(figsize=(8, 4.2))
    for i, (name, used, pct) in enumerate(rows):
        ax.bar(i, pct, width=0.55, color=COLOR[name], zorder=3)
        ax.text(i, pct + 2, f"{used:.0f} MiB\n{pct:.0f}%", ha="center", va="bottom",
                color=INK, fontsize=9)
    ax.axhline(100, color=INK2, linewidth=1, linestyle=(0, (4, 3)), zorder=2)
    ax.text(len(rows) - 0.5, 101.5, "512 MB cap", color=INK2, fontsize=8.5, ha="right")
    ax.set_xticks(range(len(rows)))
    ax.set_xticklabels([LABEL[n] for n, _, _ in rows], fontsize=9)
    ax.set_ylim(0, 125)
    style(ax, "Memory headroom under the 512 MB cap",
          "Resident memory while holding the loaded graph. CognoDB is absent: "
          "memory is not observable on a managed tier.",
          "% of the 512 MB cap")
    save(fig, "memory_headroom.png")


if __name__ == "__main__":
    chart_ingest()
    chart_traversal()
    chart_tails()
    chart_concurrency()
    chart_footprint()
