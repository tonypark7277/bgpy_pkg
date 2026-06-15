"""measure_aspawn_lookups.py

Compute the per-path verification cost of ASPAwN (ASPA-with-Neighbors / ASRA
algo B) so it can be used as a BASELINE against the bloom-filter up_path
approach in plot_filter_coverage.py (graph 2).

ASPAwN does not call filt(); it performs hop-level lookups. To put it on the
same y-axis ("lookups/probes per validation") we count, for every path, the
number of membership tests ASPAwN performs assuming UNIVERSAL ASPAwN
deployment:

  * wN neighbour sweep  -- every AS checks its path-neighbour on each side
                           (one neighbor_asns membership test each). Real BGP
                           path edges are always neighbours, so this never
                           short-circuits: exactly 2*(L-1) tests.
  * ASPA up/down ramps  -- _get_max_up_ramp_length (and, for routes received
                           from a provider, _get_max_down_ramp_length) walk the
                           reversed path doing one provider_asns lookup per hop
                           until customer->provider monotonicity breaks. The
                           early stop uses the REAL relationships, so the count
                           matches what ASPA would actually do.

We do NOT re-run the propagation: every path was already sampled and stored in
filter_coverage_paths.csv. We only rebuild the CAIDA AS graph to look up
relationships, and we recover each route's receive relationship from the first
two ASes of the stored path (validator <- next hop).

Both plain ASPA (ramp checks only) and ASPAwN (ramp checks + wN neighbour
sweep) are reported so the bloom-filter approach can be compared against each.

Output: aspawn_lookups.csv with one row per input path:
  origin_asn, validator_asn, path_length, path, recv_rel,
  aspa_lookups, aspawn_lookups

Mirrors the cost model in:
  bgpy/simulation_engine/policies/aspa/aspa.py
  bgpy/simulation_engine/policies/aspa/aspawn.py
"""

import csv
from datetime import datetime
from pathlib import Path

from frozendict import frozendict
from tqdm import tqdm

from bgpy.as_graphs import CAIDAASGraphConstructor
from bgpy.shared.enums import Relationships

HERE = Path(__file__).resolve().parent
INPUT_CSV = HERE / "filter_coverage_paths.csv"
OUTPUT_CSV = HERE / "aspawn_lookups.csv"

# --- CAIDA AS-graph snapshot config -----------------------------------------
# The MONTH of DL_TIME selects which CAIDA snapshot is used (the day is
# ignored). Pinned here for reproducibility; keep in sync with
# build_path_filter.py and the coverage measurement so every artifact
# describes the same topology.
DL_TIME = datetime(2026, 5, 19)
# Where to cache the downloaded snapshot.
#   None  -> use bgpy's default cache dir (~/.cache/bgpy/<today>); it is
#            created and the snapshot downloaded automatically on first run,
#            so a fresh clone with no existing cache just works.
#   Path  -> reuse a specific existing cache dir instead of downloading.
CACHE_DIR = None  # e.g. Path("/home/BGPfilter/.cache/bgpy/2026-05-29")


def _build_constructor_kwargs():
    """Assemble the CAIDAASGraphConstructor kwargs from the config above.

    cache_dir is only pinned when CACHE_DIR is set; otherwise it is omitted so
    bgpy falls back to its default cache dir (download-on-demand).
    """
    collector_kwargs = {"dl_time": DL_TIME}
    if CACHE_DIR is not None:
        collector_kwargs["cache_dir"] = CACHE_DIR
    return frozendict(
        {
            "as_graph_collector_kwargs": frozendict(collector_kwargs),
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


as_graph_constructor_kwargs = _build_constructor_kwargs()


def recv_relationship(as_path: tuple[int, ...], as_dict) -> Relationships:
    """How the validator (as_path[0]) received the route from as_path[1].

    ASPA's _valid_ann branches on this: PROVIDERS -> downstream (up+down ramp),
    CUSTOMERS/PEERS -> upstream (up ramp only). Recovered from the graph since
    every path edge is a real BGP adjacency.
    """
    if len(as_path) < 2:
        return Relationships.ORIGIN
    v = as_dict.get(as_path[0])
    if v is None:
        return Relationships.UNKNOWN
    nxt = as_path[1]
    if nxt in v.provider_asns:
        return Relationships.PROVIDERS
    if nxt in v.peer_asns:
        return Relationships.PEERS
    if nxt in v.customer_asns:
        return Relationships.CUSTOMERS
    return Relationships.UNKNOWN


def _ramp_calls(reversed_path: tuple[int, ...], as_dict, *, down: bool) -> int:
    """provider_check calls one ASPA ramp makes, with the real early stop.

    up ramp:   reversed_path[i] -> reversed_path[i+1]   (i = 0 .. n-2)
    down ramp: reversed_path[i] -> reversed_path[i-1]   (i = n-1 .. 1)
    Each iteration is one provider_asns lookup; stop when the current AS exists
    and the partner is NOT among its providers (mirrors _provider_check, which
    returns True -- no stop -- when the current AS is absent).
    """
    n = len(reversed_path)
    calls = 0
    rng = range(n - 1, 0, -1) if down else range(n - 1)
    for i in rng:
        partner = reversed_path[i - 1] if down else reversed_path[i + 1]
        calls += 1
        cur = as_dict.get(reversed_path[i])
        if cur is not None and partner not in cur.provider_asns:
            break
    return calls


def lookup_counts(
    as_path: tuple[int, ...], recv_rel: Relationships, as_dict
) -> tuple[int, int]:
    """Hop-level lookups (aspa, aspawn) for `as_path` under universal adoption.

      aspa   = ASPA ramp provider checks only.
      aspawn = ASPA ramp provider checks + wN neighbour sweep.

    Both assume the route is valid (the common case the baseline targets), so
    the wN sweep -- whose edges are real adjacencies -- never short-circuits and
    contributes exactly 2*(L-1) tests.
    """
    n = len(as_path)
    if n < 2:
        return 0, 0

    # ASPA ramps. _upstream_check does the up ramp; routes from a provider go
    # through _downstream_check, which also does the down ramp.
    reversed_path = as_path[::-1]
    ramp = _ramp_calls(reversed_path, as_dict, down=False)  # up ramp
    if recv_rel == Relationships.PROVIDERS:
        ramp += _ramp_calls(reversed_path, as_dict, down=True)  # down ramp

    aspa = ramp
    aspawn = ramp + 2 * (n - 1)  # + wN neighbour sweep
    return aspa, aspawn


def main() -> None:
    print("Building CAIDA AS graph ...")
    constructor_kwargs = dict(as_graph_constructor_kwargs)
    constructor_kwargs["tsv_path"] = None
    as_graph = CAIDAASGraphConstructor(**constructor_kwargs).run()
    as_dict = as_graph.as_dict

    print(f"Reading paths from {INPUT_CSV} ...")
    with INPUT_CSV.open(newline="") as fin, OUTPUT_CSV.open("w", newline="") as fout:
        reader = csv.DictReader(fin)
        writer = csv.writer(fout)
        writer.writerow(
            ["origin_asn", "validator_asn", "path_length", "path",
             "recv_rel", "aspa_lookups", "aspawn_lookups"]
        )
        for row in tqdm(reader, desc="aspa/aspawn"):
            as_path = tuple(int(a) for a in row["path"].split())
            recv_rel = recv_relationship(as_path, as_dict)
            aspa, aspawn = lookup_counts(as_path, recv_rel, as_dict)
            writer.writerow(
                [row["origin_asn"], row["validator_asn"], row["path_length"],
                 row["path"], recv_rel.name, aspa, aspawn]
            )

    print(f"Wrote {OUTPUT_CSV}")


if __name__ == "__main__":
    main()
