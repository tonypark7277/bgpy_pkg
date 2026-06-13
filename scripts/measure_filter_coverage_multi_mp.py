"""measure_filter_coverage_multi_mp.py

100,000-trial multiprocessing coverage measurement that compares false-reject
rates across all four bloom filters (max08, max09, max10, max11) in a single
run. Each worker pre-loads all four filters once, then for every sampled BGP
path checks validity under each filter.

For every checked path it ALSO compares two strategies for finding the longest
upward prefix -- the gallop+binary search (up_path_multi.UpPathMulti) and a
plain binary search (up_path_multi_binsearch.UpPathMultiBinSearch) -- on the
exact same path, recording each strategy's filt() probe count so the cheaper
search can be identified on real BGP paths.
"""

import csv
import random
from collections import defaultdict
from dataclasses import replace as dc_replace
from datetime import datetime
from multiprocessing import Pool
from pathlib import Path

from frozendict import frozendict
from tqdm import tqdm

from bgpy.as_graphs import CAIDAASGraphConstructor
from bgpy.bloom_filter_pbf_multi import load_filters
from bgpy.shared.enums import Prefixes
from bgpy.simulation_engine import BGP, SimulationEngine
from bgpy.simulation_engine.policies.up_path import up_path_multi as gallop_pol
from bgpy.simulation_engine.policies.up_path import (
    up_path_multi_binsearch as binsearch_pol,
)
from bgpy.simulation_framework import ScenarioConfig, ValidPrefix

NUM_TRIALS = 100_000
N_CPUS = 10
MAX_HOPS = [8, 9, 10, 11]
OUTPUT_CSV = Path(__file__).resolve().parent / "filter_coverage_paths.csv"

as_graph_constructor_kwargs = frozendict(
    {
        "as_graph_collector_kwargs": frozendict(
            {
                "dl_time": datetime(2026, 5, 19),
                "cache_dir": Path("/home/BGPfilter/.cache/bgpy/2026-05-29"),
            }
        ),
        "as_graph_kwargs": frozendict(
            {
                "store_customer_cone_size": True,
                "store_customer_cone_asns": False,
                "store_provider_cone_size": False,
                "store_provider_cone_asns": False,
            }
        ),
        "tsv_path": None,
    }
)


def run_chunk(args: tuple) -> dict:
    chunk_id, trial_indices, seed = args
    random.seed(seed)

    # Pre-load all four filters once per worker (mmap-backed, read-only).
    # Multiple processes safely mmap the same files simultaneously.
    # Both policy modules keep their own filter store, so inject into each.
    loaded = load_filters(MAX_HOPS)
    gallop_pol.inject_filters(loaded)
    binsearch_pol.inject_filters(loaded)

    # Each worker builds its own engine (weakrefs are not picklable).
    constructor_kwargs = dict(as_graph_constructor_kwargs)
    constructor_kwargs["tsv_path"] = None
    as_graph = CAIDAASGraphConstructor(**constructor_kwargs).run()
    all_asns = list(as_graph.as_dict.keys())
    engine = SimulationEngine(as_graph)

    scenario_config = ScenarioConfig(
        ScenarioCls=ValidPrefix,
        BasePolicyCls=BGP,
        AdoptPolicyCls=BGP,
    )

    prefix = Prefixes.PREFIX.value

    total = 0
    skipped_no_path = 0
    filter_pass:         dict[int, int] = {h: 0 for h in MAX_HOPS}
    filter_false_reject: dict[int, int] = {h: 0 for h in MAX_HOPS}
    path_len_pass:   dict[int, dict[int, int]] = {h: defaultdict(int) for h in MAX_HOPS}
    path_len_reject: dict[int, dict[int, int]] = {h: defaultdict(int) for h in MAX_HOPS}

    # up_len-search probes per strategy and number of checks, broken down by
    # path length (one check = one (path, max_hop) validation).
    up_len_gallop_by_len:    dict[int, int] = defaultdict(int)
    up_len_binsearch_by_len: dict[int, int] = defaultdict(int)
    checks_by_len:           dict[int, int] = defaultdict(int)

    # One record per checked path for the detailed CSV.
    rows: list[tuple] = []

    for _ in tqdm(trial_indices, desc=f"worker {chunk_id}", position=chunk_id, leave=False):
        victim_asn = random.choice(all_asns)
        adopter_asn = random.choice(all_asns)
        while adopter_asn == victim_asn:
            adopter_asn = random.choice(all_asns)

        scenario = ValidPrefix(
            scenario_config=scenario_config,
            percent_adoption=0,
            engine=engine,
            victim_asns=frozenset({victim_asn}),
        )
        scenario.setup_engine(engine)
        engine.run(propagation_round=0, scenario=scenario)

        adopter_as = engine.as_graph.as_dict[adopter_asn]
        ann = adopter_as.policy.local_rib.get(prefix)

        if ann is None:
            skipped_no_path += 1
            continue

        total += 1

        # Two policy instances over the same AS, validating the same path.
        up_path = gallop_pol.UpPathMulti()
        up_path.as_ = adopter_as.policy.as_
        up_path_bs = binsearch_pol.UpPathMultiBinSearch()
        up_path_bs.as_ = adopter_as.policy.as_
        stripped_ann = dc_replace(ann, as_path=ann.as_path[1:])
        path_len = len(ann.as_path)

        # as_path[0] is the validator (adopter); as_path[-1] is the origin.
        origin_asn = ann.as_path[-1]
        path_str = " ".join(str(a) for a in ann.as_path)

        hop_passes: dict[int, int] = {}
        hop_up_len: dict[int, int] = {}
        hop_up_gallop: dict[int, int] = {}
        hop_up_binsearch: dict[int, int] = {}
        hop_rest_calls: dict[int, int] = {}
        for hop in MAX_HOPS:
            gallop_pol.set_active_max_hop(hop)
            binsearch_pol.set_active_max_hop(hop)

            valid_g, up_len_g, up_g, rest_g = up_path.ann_is_valid_by_up_path(stripped_ann)
            _valid_b, _up_len_b, up_b, _rest_b = up_path_bs.ann_is_valid_by_up_path(stripped_ann)

            hop_up_len[hop] = up_len_g
            hop_up_gallop[hop] = up_g
            hop_up_binsearch[hop] = up_b
            hop_rest_calls[hop] = rest_g  # rest phase is identical for both
            up_len_gallop_by_len[path_len] += up_g
            up_len_binsearch_by_len[path_len] += up_b
            checks_by_len[path_len] += 1

            # Validity is determined by the (reference) gallop policy.
            if valid_g:
                filter_pass[hop] += 1
                path_len_pass[hop][path_len] += 1
                hop_passes[hop] = 1
            else:
                filter_false_reject[hop] += 1
                path_len_reject[hop][path_len] += 1
                hop_passes[hop] = 0

        # up_len is recorded for the deepest filter (max11), the one the
        # downstream analysis (graph 2) is drawn against. It may differ across
        # filters because each PBF only recognises segments up to its max hop.
        # Columns grouped by max_hop: pass / gallop & binsearch up_len_calls / rest_calls.
        rows.append(
            (origin_asn, adopter_asn, path_len, path_str, hop_up_len[MAX_HOPS[-1]])
            + tuple(
                val
                for h in MAX_HOPS
                for val in (
                    hop_passes[h],
                    hop_up_gallop[h],
                    hop_up_binsearch[h],
                    hop_rest_calls[h],
                )
            )
        )

    return {
        "total": total,
        "skipped_no_path": skipped_no_path,
        "filter_pass": filter_pass,
        "filter_false_reject": filter_false_reject,
        "path_len_pass": {h: dict(v) for h, v in path_len_pass.items()},
        "path_len_reject": {h: dict(v) for h, v in path_len_reject.items()},
        "up_len_gallop_by_len": dict(up_len_gallop_by_len),
        "up_len_binsearch_by_len": dict(up_len_binsearch_by_len),
        "checks_by_len": dict(checks_by_len),
        "rows": rows,
    }


def main():
    chunk_size = NUM_TRIALS // N_CPUS
    chunks = []
    for i in range(N_CPUS):
        start = i * chunk_size
        end = start + chunk_size if i < N_CPUS - 1 else NUM_TRIALS
        chunks.append((i, range(start, end), i * 12345))

    print(f"Running {NUM_TRIALS:,} trials across {N_CPUS} workers "
          f"(filters: max{MAX_HOPS[0]:02d}..max{MAX_HOPS[-1]:02d})...")

    with Pool(N_CPUS) as pool:
        results = pool.map(run_chunk, chunks)

    # Aggregate across workers
    total = 0
    skipped_no_path = 0
    filter_pass:         dict[int, int] = {h: 0 for h in MAX_HOPS}
    filter_false_reject: dict[int, int] = {h: 0 for h in MAX_HOPS}
    path_len_pass:   dict[int, dict[int, int]] = {h: defaultdict(int) for h in MAX_HOPS}
    path_len_reject: dict[int, dict[int, int]] = {h: defaultdict(int) for h in MAX_HOPS}

    up_len_gallop_by_len:    dict[int, int] = defaultdict(int)
    up_len_binsearch_by_len: dict[int, int] = defaultdict(int)
    checks_by_len:           dict[int, int] = defaultdict(int)

    for r in results:
        total += r["total"]
        skipped_no_path += r["skipped_no_path"]
        for k, v in r["up_len_gallop_by_len"].items():
            up_len_gallop_by_len[k] += v
        for k, v in r["up_len_binsearch_by_len"].items():
            up_len_binsearch_by_len[k] += v
        for k, v in r["checks_by_len"].items():
            checks_by_len[k] += v
        for h in MAX_HOPS:
            filter_pass[h]         += r["filter_pass"][h]
            filter_false_reject[h] += r["filter_false_reject"][h]
            for k, v in r["path_len_pass"][h].items():
                path_len_pass[h][k] += v
            for k, v in r["path_len_reject"][h].items():
                path_len_reject[h][k] += v

    # Write the detailed per-path CSV.
    with OUTPUT_CSV.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(
            ["origin_asn", "validator_asn", "path_length", "path", "up_len"]
            + [
                col
                for h in MAX_HOPS
                for col in (
                    f"pass_max{h:02d}",
                    f"up_len_calls_gallop_max{h:02d}",
                    f"up_len_calls_binsearch_max{h:02d}",
                    f"rest_calls_max{h:02d}",
                )
            ]
        )
        for r in results:
            writer.writerows(r["rows"])
    print(f"Wrote per-path CSV: {OUTPUT_CSV}")

    # Report
    print(f"\n=== Results ===")
    print(f"Trials:             {NUM_TRIALS:>10,}")
    print(f"No path (skipped):  {skipped_no_path:>10,}")
    print(f"Paths checked:      {total:>10,}")

    # up_len-search probe comparison (summed over all paths x all max_hops).
    gallop_total = sum(up_len_gallop_by_len.values())
    binsearch_total = sum(up_len_binsearch_by_len.values())
    n_checks = sum(checks_by_len.values())
    print(f"\nup_len search probe comparison ({n_checks:,} checks):")
    print(f"  {'strategy':>10}  {'total probes':>14}  {'avg/check':>10}")
    for label, tot in (("gallop", gallop_total), ("binsearch", binsearch_total)):
        avg = tot / n_checks if n_checks else 0.0
        print(f"  {label:>10}  {tot:>14,}  {avg:>10.3f}")
    if gallop_total:
        delta = binsearch_total - gallop_total
        print(f"  binsearch vs gallop: {delta:+,} probes "
              f"({delta / gallop_total * 100:+.2f}%)")

    # Same comparison, broken down by path length (avg probes per check).
    print(f"\nup_len search probes by path length (avg per check):")
    print(f"  {'len':>4}  {'checks':>8}  {'gallop':>8}  {'binsearch':>10}  {'delta%':>8}")
    for length in sorted(checks_by_len):
        c = checks_by_len[length]
        g = up_len_gallop_by_len[length]
        b = up_len_binsearch_by_len[length]
        ga = g / c if c else 0.0
        ba = b / c if c else 0.0
        pct = (b - g) / g * 100 if g else 0.0
        print(f"  {length:>4}  {c:>8,}  {ga:>8.3f}  {ba:>10.3f}  {pct:>7.2f}%")

    if total > 0:
        header = f"  {'max_hop':>7}  {'pass':>10}  {'false_reject':>12}  {'reject%':>8}"
        print(f"\nPer-filter summary:")
        print(header)
        for h in MAX_HOPS:
            p = filter_pass[h]
            r = filter_false_reject[h]
            pct = r / total * 100
            print(f"  max{h:02d}    {p:>10,}  {r:>12,}  {pct:>7.3f}%")

        print(f"\nPath length breakdown (false-reject %):")
        all_lengths = sorted(
            set(k for h in MAX_HOPS for k in list(path_len_pass[h]) + list(path_len_reject[h]))
        )
        hop_header = "  ".join(f"max{h:02d}%" for h in MAX_HOPS)
        print(f"  {'len':>4}  {'total':>8}  {hop_header}")
        for length in all_lengths:
            t = sum(
                path_len_pass[h].get(length, 0) + path_len_reject[h].get(length, 0)
                for h in MAX_HOPS[:1]
            )
            reject_pcts = []
            for h in MAX_HOPS:
                p = path_len_pass[h].get(length, 0)
                r = path_len_reject[h].get(length, 0)
                pct = r / (p + r) * 100 if (p + r) > 0 else 0.0
                reject_pcts.append(f"{pct:>7.2f}%")
            print(f"  {length:>4}  {t:>8,}  {'  '.join(reject_pcts)}")


if __name__ == "__main__":
    main()
