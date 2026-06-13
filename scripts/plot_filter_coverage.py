"""plot_filter_coverage.py

Draw three figures from filter_coverage_paths.csv (produced by
measure_filter_coverage_multi_mp.py):

  1. Coverage (pass) rate vs path length, one line per filter (max08..max11).
  2. Box plot of per-path validation cost (filt() calls) for PASSED paths,
     using the BINARY-SEARCH up_len strategy, max11 only.
  3. up_len-search technique comparison: gallop vs plain binary search,
     mean filt() calls spent finding the longest upward prefix, by path length.

CSV schema (per max_hop h in 08,09,10,11):
  origin_asn, validator_asn, path_length, path, up_len,
  pass_max{h}, up_len_calls_gallop_max{h},
  up_len_calls_binsearch_max{h}, rest_calls_max{h}

Total filt() calls for one validation = up_len_calls_<strategy> + rest_calls.
"""

from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
CSV_PATH = HERE / "filter_coverage_paths.csv"
ASPAWN_CSV = HERE / "aspawn_lookups.csv"   # baseline, produced by measure_aspawn_lookups.py
OUT_DIR = HERE / "plots"

MAX_HOPS = [8, 9, 10, 11]
ANALYSIS_HOP = 11          # graphs 2 & 3 are drawn against this filter
MIN_SAMPLES = 30           # path lengths with fewer samples are dropped from box plots


def load() -> pd.DataFrame:
    df = pd.read_csv(CSV_PATH)
    print(f"Loaded {len(df):,} rows from {CSV_PATH}")
    # Optional ASPAwN baseline cost, joined per path (graph 2 overlay).
    if ASPAWN_CSV.exists():
        base = pd.read_csv(
            ASPAWN_CSV, usecols=["path", "aspa_lookups", "aspawn_lookups"]
        )
        df = df.merge(base, on="path", how="left")
        print(f"Merged ASPA/ASPAwN baselines from {ASPAWN_CSV}")
    else:
        print(f"(no {ASPAWN_CSV.name}; graph 2 will skip the ASPA baselines)")
    return df


# ---------------------------------------------------------------------------
# Graph 1: coverage (pass) rate vs path length, one line per filter.
# ---------------------------------------------------------------------------
def plot_coverage_rate(df: pd.DataFrame) -> None:
    fig, ax = plt.subplots(figsize=(9, 6))
    for h in MAX_HOPS:
        col = f"pass_max{h:02d}"
        grp = df.groupby("path_length")[col].agg(["mean", "count"])
        grp = grp[grp["count"] >= MIN_SAMPLES]
        ax.plot(
            grp.index, grp["mean"] * 100,
            marker="o", label=f"{h}",
        )
    ax.set_xlabel("Path length (# of ASes)")
    ax.set_ylabel("Coverage rate (passed %)")
    ax.set_title("Coverage Rate per Threshold")
    ax.set_ylim(0, 101)
    ax.grid(True, alpha=0.3)
    ax.legend(title="Threshold")
    fig.tight_layout()
    out = OUT_DIR / "graph1_coverage_rate.png"
    fig.savefig(out, dpi=150)
    plt.close(fig)
    print(f"Wrote {out}")


# ---------------------------------------------------------------------------
# Graph 2: box plot of UpPathFilter validation cost (binary-search filt() calls)
# for passed paths, max11, grouped by path length, with ASPA-family baselines.
#   2-1: UpPathFilter boxes vs ASPA and ASPAwN baselines.
#   2-2: UpPathFilter boxes vs ASPA only (ASPAwN's large counts otherwise squash
#        the boxes, so this view keeps the box detail readable).
#   2-3: same as 2-2 plus a log2(n) + 2 reference trend line.
#   2-4: same as 2-3 but with c = 2.27.
# ---------------------------------------------------------------------------
ASPA_BASE = ("aspa_lookups", "green", "ASPA (mean lookups, full deployment)")
ASPAWN_BASE = ("aspawn_lookups", "red", "ASPAwN (mean lookups, full deployment)")


def _validation_cost_box(
    df: pd.DataFrame, baselines, out_name: str, title: str,
    trend_log2: bool = False, trend_c: float = 2,
) -> None:
    h = ANALYSIS_HOP
    total_calls = (
        df[f"up_len_calls_binsearch_max{h:02d}"] + df[f"rest_calls_max{h:02d}"]
    )
    passed = df[df[f"pass_max{h:02d}"] == 1].assign(total_calls=total_calls)

    kept_lengths, data, labels = [], [], []
    for L in sorted(passed["path_length"].unique()):
        vals = passed.loc[passed["path_length"] == L, "total_calls"].values
        if len(vals) >= MIN_SAMPLES:
            kept_lengths.append(L)
            data.append(vals)
            labels.append(f"{L}\n(n={len(vals):,})")

    fig, ax = plt.subplots(figsize=(max(9, len(data) * 0.8), 6))
    ax.boxplot(
        data, labels=labels, showmeans=True, showfliers=True,
        flierprops=dict(marker=".", markersize=3, alpha=0.3),
    )

    # Baselines: mean ASPA-family lookups for the same passed paths, as lines.
    # boxplot positions are 1-indexed, so align the lines to 1..len(data).
    xs = range(1, len(kept_lengths) + 1)
    line_handles = []
    for col, color, label in baselines:
        if col in passed.columns and passed[col].notna().any():
            baseline = [
                passed.loc[passed["path_length"] == L, col].mean()
                for L in kept_lengths
            ]
            (line,) = ax.plot(
                xs, baseline, color=color, marker="o", linewidth=2, label=label
            )
            line_handles.append(line)

    # Optional log2(n) + c reference trend (n = path length), as a dashed line.
    if trend_log2:
        c = trend_c
        trend = [np.log2(L) + c for L in kept_lengths]
        (tline,) = ax.plot(
            xs, trend, color="gray", linestyle="--", linewidth=2,
            label=rf"Theoretical Trend ($\log_2(n) + {c}$)",
        )
        line_handles.append(tline)

    # Legend: a proxy box for the UpPathFilter box plot plus the lines.
    box_proxy = plt.Rectangle((0, 0), 1, 1, facecolor="white", edgecolor="black")
    ax.legend(
        [box_proxy, *line_handles],
        ["UpPathFilter", *[h.get_label() for h in line_handles]],
    )

    ax.set_xlabel("Path length (# of ASes)   [n = #passed paths]")
    ax.set_ylabel("# of filter query")
    ax.set_title(title)
    ax.grid(True, axis="y", alpha=0.3)
    fig.tight_layout()
    out = OUT_DIR / out_name
    fig.savefig(out, dpi=150)
    plt.close(fig)
    print(f"Wrote {out}")


def plot_validation_cost_box(df: pd.DataFrame) -> None:
    # 2-1: UpPathFilter vs ASPA + ASPAwN.
    _validation_cost_box(
        df, [ASPA_BASE, ASPAWN_BASE],
        "graph2_1_validation_cost_box.png",
        "Validation Cost: UpPathFilter vs ASPA / ASPAwN",
    )
    # 2-2: UpPathFilter vs ASPA only (boxes stay readable).
    _validation_cost_box(
        df, [ASPA_BASE],
        "graph2_2_validation_cost_box_aspa.png",
        "Validation Cost: UpPathFilter vs ASPA",
    )
    # 2-3: same as 2-2 plus a log2(n) + 2 reference trend.
    _validation_cost_box(
        df, [ASPA_BASE],
        "graph2_3_validation_cost_box_log2.png",
        "Validation Cost: UpPathFilter vs ASPA",
        trend_log2=True, trend_c=2,
    )
    # 2-4: same as 2-3 but with c = 2.27.
    _validation_cost_box(
        df, [ASPA_BASE],
        "graph2_4_validation_cost_box_log2.png",
        "Validation Cost: UpPathFilter vs ASPA",
        trend_log2=True, trend_c=2.27,
    )


# ---------------------------------------------------------------------------
# Graph 4: box plot of the REST-phase cost only (downward/peer checks after the
# up_len search), for passed paths, max11, grouped by path length.
# ---------------------------------------------------------------------------
def plot_rest_cost_box(df: pd.DataFrame) -> None:
    h = ANALYSIS_HOP
    rest_col = f"rest_calls_max{h:02d}"
    passed = df[df[f"pass_max{h:02d}"] == 1]

    data, labels = [], []
    for L in sorted(passed["path_length"].unique()):
        vals = passed.loc[passed["path_length"] == L, rest_col].values
        if len(vals) >= MIN_SAMPLES:
            data.append(vals)
            labels.append(f"{L}\n(n={len(vals):,})")

    fig, ax = plt.subplots(figsize=(max(9, len(data) * 0.8), 6))
    ax.boxplot(
        data, labels=labels, showmeans=True, showfliers=True,
        flierprops=dict(marker=".", markersize=3, alpha=0.3),
    )

    # Trend lines:
    #  - mean of the per-path-length means (each length weighted equally)
    #  - overall mean over all passed paths (weighted by path count per length)
    per_len_means = [vals.mean() for vals in data]
    grand_mean = np.mean(per_len_means)
    overall_mean = passed[rest_col].mean()
    ax.axhline(
        grand_mean, color="red", linestyle="--", linewidth=2,
        label=f"Mean of per-length means = {grand_mean:.2f}",
    )
    ax.axhline(
        overall_mean, color="blue", linestyle=":", linewidth=2,
        label=f"Overall mean (all paths) = {overall_mean:.2f}",
    )
    ax.legend()

    ax.set_xlabel("Path length (# of ASes)   [n = #passed paths]")
    ax.set_ylabel("# of filter query")
    ax.set_title("Rest-Phase Validation Cost")
    ax.grid(True, axis="y", alpha=0.3)
    fig.tight_layout()
    out = OUT_DIR / "graph4_rest_cost_box.png"
    fig.savefig(out, dpi=150)
    plt.close(fig)
    print(f"Wrote {out}")


# ---------------------------------------------------------------------------
# Graph 3: up_len-search technique comparison (gallop vs binary search), as a
# grouped box plot of the filt() calls spent finding the longest upward prefix,
# by path length. max11, valid (passed) paths only.
# ---------------------------------------------------------------------------
def plot_uplen_technique_comparison(df: pd.DataFrame) -> None:
    h = ANALYSIS_HOP
    g_col = f"up_len_calls_gallop_max{h:02d}"
    b_col = f"up_len_calls_binsearch_max{h:02d}"

    techniques = [
        ("gallop + binary search", g_col, "#1f77b4"),
        ("plain binary search", b_col, "#ff7f0e"),
    ]
    offsets = [-0.2, 0.2]
    box_w = 0.34

    lengths, labels = [], []
    for L in sorted(df["path_length"].unique()):
        n = (df["path_length"] == L).sum()
        if n >= MIN_SAMPLES:
            lengths.append(L)
            labels.append(f"{L}\n(n={n:,})")
    x = np.arange(len(lengths))

    fig, ax = plt.subplots(figsize=(max(10, len(lengths) * 1.0), 6))
    for (name, col, color), off in zip(techniques, offsets):
        data = [df.loc[df["path_length"] == L, col].values for L in lengths]
        bp = ax.boxplot(
            data, positions=x + off, widths=box_w,
            patch_artist=True, showmeans=True, showfliers=True,
            flierprops=dict(marker=".", markersize=3,
                            markerfacecolor=color, markeredgecolor=color,
                            alpha=0.3),
        )
        for patch in bp["boxes"]:
            patch.set_facecolor(color)
            patch.set_alpha(0.7)

    ax.set_xticks(x)
    ax.set_xticklabels(labels)
    ax.set_xlabel("Path length (# of ASes)")
    ax.set_ylabel("# of filter query")
    ax.set_title("Peak Search Cost by Technique")
    ax.grid(True, axis="y", alpha=0.3)
    handles = [
        plt.Rectangle((0, 0), 1, 1, facecolor=c, alpha=0.7) for _, _, c in techniques
    ]
    ax.legend(handles, [t[0] for t in techniques], title="Technique")
    fig.tight_layout()
    out = OUT_DIR / "graph3_uplen_technique_comparison.png"
    fig.savefig(out, dpi=150)
    plt.close(fig)
    print(f"Wrote {out}")

    # Overall averages for the console.
    g_avg, b_avg = df[g_col].mean(), df[b_col].mean()
    print(
        f"Overall up_len search (max{h:02d}): "
        f"gallop {g_avg:.3f}  vs  binsearch {b_avg:.3f} calls/path "
        f"({(b_avg - g_avg) / g_avg * 100:+.2f}% binsearch vs gallop)"
    )


def _uplen_kept_lengths(df: pd.DataFrame) -> list[int]:
    counts = df.groupby("path_length").size()
    return [int(L) for L, n in counts.items() if n >= MIN_SAMPLES]


# Graph 3-1: mean filt() calls vs path length, with a 25-75 percentile band.
def plot_uplen_mean_iqr(df: pd.DataFrame) -> None:
    h = ANALYSIS_HOP
    techniques = [
        ("gallop + binary search", f"up_len_calls_gallop_max{h:02d}", "#1f77b4"),
        ("plain binary search", f"up_len_calls_binsearch_max{h:02d}", "#ff7f0e"),
    ]
    lengths = sorted(_uplen_kept_lengths(df))

    fig, ax = plt.subplots(figsize=(9, 6))
    for name, col, color in techniques:
        by_len = df[df["path_length"].isin(lengths)].groupby("path_length")[col]
        means = by_len.mean().reindex(lengths)
        q25 = by_len.quantile(0.25).reindex(lengths)
        q75 = by_len.quantile(0.75).reindex(lengths)
        ax.plot(lengths, means, marker="o", color=color, label=name)
        ax.fill_between(lengths, q25, q75, color=color, alpha=0.2)
    ax.set_xlabel("Path length (# of ASes)")
    ax.set_ylabel("# of filter query")
    ax.set_title("Peak Search Algorithm Comparison")
    ax.grid(True, alpha=0.3)
    ax.legend(title="Algorithm")
    fig.tight_layout()
    out = OUT_DIR / "graph3_1_uplen_mean_iqr.png"
    fig.savefig(out, dpi=150)
    plt.close(fig)
    print(f"Wrote {out}")


# Graph 3-2: per-technique distribution heatmap (call count x path length,
# colour = fraction of paths at that length with that call count).
def plot_uplen_distribution_heatmap(df: pd.DataFrame) -> None:
    h = ANALYSIS_HOP
    techniques = [
        ("gallop + binary search", f"up_len_calls_gallop_max{h:02d}"),
        ("plain binary search", f"up_len_calls_binsearch_max{h:02d}"),
    ]
    lengths = sorted(_uplen_kept_lengths(df))
    sub = df[df["path_length"].isin(lengths)]
    max_calls = int(max(sub[c].max() for _, c in techniques))
    call_vals = list(range(1, max_calls + 1))

    fig, axes = plt.subplots(1, 2, figsize=(15, 6), sharey=True)
    im = None
    for ax, (name, col) in zip(axes, techniques):
        M = np.zeros((len(call_vals), len(lengths)))
        for j, L in enumerate(lengths):
            vc = sub.loc[sub["path_length"] == L, col].value_counts(normalize=True)
            for i, c in enumerate(call_vals):
                M[i, j] = vc.get(c, 0.0)
        im = ax.imshow(M, origin="lower", aspect="auto", cmap="viridis", vmin=0, vmax=1)
        ax.set_xticks(range(len(lengths)))
        ax.set_xticklabels(lengths)
        ax.set_yticks(range(len(call_vals)))
        ax.set_yticklabels(call_vals)
        ax.set_xlabel("Path length (# of ASes)")
        ax.set_title(name)
    axes[0].set_ylabel("# of filter query")
    fig.colorbar(im, ax=axes, label="fraction of paths (per path length)", shrink=0.8)
    fig.suptitle("Peak Search Cost Distribution")
    out = OUT_DIR / "graph3_2_uplen_distribution_heatmap.png"
    fig.savefig(out, dpi=150)
    plt.close(fig)
    print(f"Wrote {out}")


# Graph 3-3: stacked proportion bars of call-count buckets (1/2/3/4+) per path
# length, two bars (gallop vs binsearch) side by side.
def plot_uplen_proportion_stacked(df: pd.DataFrame) -> None:
    h = ANALYSIS_HOP
    techniques = [
        ("gallop", f"up_len_calls_gallop_max{h:02d}", -0.2),
        ("binsearch", f"up_len_calls_binsearch_max{h:02d}", 0.2),
    ]
    buckets = [("1", 1), ("2", 2), ("3", 3), ("4+", None)]
    cmap = plt.get_cmap("viridis")
    colors = [cmap(t) for t in np.linspace(0.15, 0.9, len(buckets))]

    lengths = sorted(_uplen_kept_lengths(df))
    sub = df[df["path_length"].isin(lengths)]
    x = np.arange(len(lengths))
    bar_w = 0.36

    fig, ax = plt.subplots(figsize=(max(10, len(lengths) * 1.0), 6))
    for name, col, off in techniques:
        bottoms = np.zeros(len(lengths))
        for (blabel, bval), color in zip(buckets, colors):
            fracs = []
            for L in lengths:
                vals = sub.loc[sub["path_length"] == L, col]
                if bval is None:
                    frac = (vals >= 4).mean()
                else:
                    frac = (vals == bval).mean()
                fracs.append(frac)
            fracs = np.array(fracs)
            ax.bar(x + off, fracs, bar_w, bottom=bottoms, color=color,
                   edgecolor="white", linewidth=0.3)
            bottoms += fracs
        # technique label under each group
        for xi in x:
            ax.text(xi + off, -0.03, name, ha="center", va="top",
                    fontsize=7, rotation=90, color="dimgray")

    ax.set_xticks(x)
    ax.set_xticklabels(lengths)
    ax.set_xlabel("Path length (# of ASes)   [left=gallop, right=binsearch]")
    ax.set_ylabel("fraction of paths")
    ax.set_ylim(0, 1)
    ax.set_title("Peak Search Call-Count Distribution by Technique")
    handles = [
        plt.Rectangle((0, 0), 1, 1, facecolor=c) for c in colors
    ]
    ax.legend(handles, [b[0] for b in buckets], title="# of filter query",
              loc="upper left", ncol=len(buckets))
    fig.tight_layout()
    out = OUT_DIR / "graph3_3_uplen_proportion_stacked.png"
    fig.savefig(out, dpi=150)
    plt.close(fig)
    print(f"Wrote {out}")


# ---------------------------------------------------------------------------
# Console report: how many validations finished faster than ASPA using the
# binary-search filtering strategy, compared against the MEASURED per-path ASPA
# lookup cost (aspa_lookups, from aspawn_lookups.csv) -- NOT the n-1 assumption.
# The measured cost reflects the real up/down-ramp work, which is ~n for routes
# received from a provider (both ramps run) and exactly n-1 for upstream routes.
#   binary-search filter cost = up_len_calls_binsearch + rest_calls
#                               (rest_calls already includes the _is_peer_link
#                                probe, so this is the full validation cost)
# "Faster" means strictly fewer probes than ASPA (cost < aspa_lookups).
# ---------------------------------------------------------------------------
def report_faster_than_aspa(df: pd.DataFrame) -> None:
    if "aspa_lookups" not in df.columns:
        print("\n=== Faster than ASPA: skipped (no aspa_lookups; run "
              "measure_aspawn_lookups.py first) ===")
        return

    # Only paths that have a measured ASPA baseline can be compared.
    df = df[df["aspa_lookups"].notna()]
    aspa = df["aspa_lookups"]

    print("\n=== Faster than ASPA (binary-search filtering, measured baseline) ===")
    print("  ASPA cost = measured aspa_lookups ; "
          "binsearch cost = up_len_calls_binsearch + rest_calls")
    print(f"  {'filter':>7}  {'checks':>10}  {'faster <':>10}  "
          f"{'equal =':>9}  {'slower >':>9}  {'faster%':>8}  {'equal%':>7}")
    n = len(df)
    for h in MAX_HOPS:
        cost = df[f"up_len_calls_binsearch_max{h:02d}"] + df[f"rest_calls_max{h:02d}"]
        faster = int((cost < aspa).sum())
        equal = int((cost == aspa).sum())
        slower = int((cost > aspa).sum())
        print(f"  max{h:02d}  {n:>10,}  {faster:>10,}  {equal:>9,}  "
              f"{slower:>9,}  {faster / n * 100:>7.2f}%  {equal / n * 100:>6.2f}%")

    # By path length, for the analysis filter (max11).
    h = ANALYSIS_HOP
    cost = df[f"up_len_calls_binsearch_max{h:02d}"] + df[f"rest_calls_max{h:02d}"]
    tmp = df.assign(
        _faster=(cost < aspa).astype(int),
        _equal=(cost == aspa).astype(int),
    )
    print(f"\n  by path length (max{h:02d}):")
    print(f"    {'len':>4}  {'checks':>10}  {'faster':>10}  {'faster%':>8}  {'equal%':>7}")
    grp = tmp.groupby("path_length")[["_faster", "_equal"]]
    for L in sorted(grp.groups):
        sub = grp.get_group(L)
        m = len(sub)
        f_cnt, e_cnt = int(sub["_faster"].sum()), int(sub["_equal"].sum())
        print(f"    {L:>4}  {m:>10,}  {f_cnt:>10,}  "
              f"{f_cnt / m * 100:>7.2f}%  {e_cnt / m * 100:>6.2f}%")


def main() -> None:
    OUT_DIR.mkdir(exist_ok=True)
    df = load()

    # Console answer: validations cheaper than ASPA, over ALL sampled paths.
    report_faster_than_aspa(df)

    # Graph 1 uses ALL sampled paths (it measures pass vs reject).
    plot_coverage_rate(df)

    # Every other graph is about cost on VALID paths only -> keep passed ones
    # (validity is defined by the analysis filter, max11).
    valid = df[df[f"pass_max{ANALYSIS_HOP:02d}"] == 1]
    print(f"{len(valid):,} valid (passed max{ANALYSIS_HOP:02d}) paths for graphs 2-4")
    plot_validation_cost_box(valid)
    plot_rest_cost_box(valid)
    plot_uplen_technique_comparison(valid)
    plot_uplen_mean_iqr(valid)
    plot_uplen_distribution_heatmap(valid)
    plot_uplen_proportion_stacked(valid)
    print(f"\nDone. Figures in {OUT_DIR}/")


if __name__ == "__main__":
    main()
