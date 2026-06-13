"""UpPath policy with multi-filter support.

Drop-in replacement for up_path.py that supports pre-loading all four
bloom filters (max08..max11) and switching between them at runtime.

Typical usage in a worker process:

    from bgpy.simulation_engine.policies.up_path.up_path_multi import (
        UpPathMulti, inject_filters, set_active_max_hop,
    )
    from bgpy.bloom_filter_pbf_multi import load_filters

    filters = load_filters()       # pre-load 08..11 once per worker
    inject_filters(filters)        # wire them into the module globals

    set_active_max_hop(10)         # choose which filter filt() uses
    # ... run simulation, call up_path_inst.ann_is_valid_by_up_path(ann) ...

    set_active_max_hop(11)         # switch to a different filter
    # ... run again on the same paths ...
"""

from typing import TYPE_CHECKING

from bgpy.simulation_engine.policies.rov import ROV
from bgpy.bloom_filter_pbf_multi import load_filters, query_with

if TYPE_CHECKING:
    from bgpy.shared.enums import Relationships
    from bgpy.simulation_engine import Announcement as Ann


# Per-process filter store. Populated by inject_filters() or lazily on
# first filt() call (defaults to max10 to match original behaviour).
_bloom_filters: dict[int, tuple] = {}
_active_max_hop: int = 10

# Per-phase probe counters. Each filt() call (and each _is_peer_link() lookup,
# which we treat as one equivalent probe) increments the bucket named by the
# current phase label. Plain module-level state is fine: every forked worker
# has its own copy and we reset/read it within a single
# ann_is_valid_by_up_path() call. ann_is_valid_by_up_path() splits work into
# the "up_len" phase (finding the longest upward prefix) and the "rest" phase.
_filt_counts: dict[str, int] = {}
_filt_phase: str = "default"


def set_filt_phase(name: str) -> None:
    """Label subsequent probes so they accumulate under `name`."""
    global _filt_phase
    _filt_phase = name


def reset_filt_counts() -> None:
    """Clear all per-phase counters. Called at the start of each validation."""
    global _filt_counts, _filt_phase
    _filt_counts = {}
    _filt_phase = "default"


def get_phase_count(name: str) -> int:
    """Return how many probes were attributed to phase `name`."""
    return _filt_counts.get(name, 0)


def _count_probe() -> None:
    """Record one probe against the current phase."""
    global _filt_counts
    _filt_counts[_filt_phase] = _filt_counts.get(_filt_phase, 0) + 1


def inject_filters(loaded: dict[int, tuple]) -> None:
    """Replace the process-wide filter store with a pre-loaded dict.

    `loaded` is the dict returned by load_filters() from
    bloom_filter_pbf_multi. Call once per worker before any filt() call.
    """
    global _bloom_filters
    _bloom_filters = loaded


def set_active_max_hop(n: int) -> None:
    """Select which filter filt() queries. `n` must be in _bloom_filters."""
    global _active_max_hop
    _active_max_hop = n


def filt(path: tuple[int, ...]) -> bool:
    """True iff `path` is a known strictly-upward segment (every hop c->p).

    Queries the filter selected by set_active_max_hop() (default: max10).
    A segment shorter than 2 ASNs is trivially upward (short-circuits True).
    False negatives are impossible; True may be a false positive at build FPR.

    Every call counts as one probe against the current phase (set_filt_phase),
    including the short-circuit case, since it is still a logical path probe.
    """
    _count_probe()

    if len(path) < 2:
        return True

    if _active_max_hop not in _bloom_filters:
        # Lazy fallback: load just the active filter on first use.
        _bloom_filters.update(load_filters([_active_max_hop]))

    bf, meta = _bloom_filters[_active_max_hop]
    return query_with(bf, meta, path)


def longest_upward_prefix(full_path: tuple[int, ...]) -> int:
    """filt(full_path[:L]) 가 True인 가장 큰 L.

    All probes here are attributed to the "up_len" phase.
    """
    n = len(full_path)
    # gallop: prefix(hi)가 invalid가 될 때까지 2배씩, lo는 마지막 valid
    set_filt_phase("up_len")
    lo, hi = 1, 2
    while hi < n and filt(full_path[:hi]):
        lo, hi = hi, hi * 2
    hi = min(hi, n)
    if hi == n and filt(full_path[:n]):
        return n
    # (lo, hi) 안에서 경계 이진 탐색 — 선형 꼬리 없음
    while lo + 1 < hi:
        mid = (lo + hi) // 2
        if filt(full_path[:mid]):
            lo = mid
        else:
            hi = mid
    return lo


class UpPathMulti(ROV):

    name: str = "UpPathMulti"

    def _valid_ann(self, ann: "Ann", recv_rel: "Relationships") -> bool:
        """Returns announcement validity

        return false if invalid by filter confirmation of segmented path
        otherwise additionally use standard ROV to determine validity
        """

        # ann_is_valid_by_up_path returns (is_valid, up_len, up_len_calls,
        # rest_calls); up_len and the counts are for measurement only, so drop
        # them here.
        is_valid, _up_len, _up_len_calls, _rest_calls = self.ann_is_valid_by_up_path(ann)

        # if not valid by up_path (can be confirmed by filter), return false
        if not is_valid:
            return False
        # if valid by up_path, additionally use standard ROV to determine check origin
        else:
            return super()._valid_ann(ann, recv_rel)


    def ann_is_valid_by_up_path(self, ann: "Ann") -> tuple[bool, int, int, int]:
        """Validate the path and report probe counts in two phases.

        Returns (is_valid, up_len, up_len_calls, rest_calls):
          - up_len:       length of the longest upward prefix that was found
          - up_len_calls: probes spent finding the longest upward prefix
          - rest_calls:   probes spent on the remaining downward/peer checks
                          (_is_peer_link counts as one probe)
        """
        reset_filt_counts()

        # Receiver at index 0, origin at the last index.
        full_path = (self.as_.asn, *ann.as_path)
        n = len(full_path)

        # Phase "up_len": gallop + binary search for the longest upward prefix.
        up_len = longest_upward_prefix(full_path)

        # Phase "rest": everything after the up_len search.
        set_filt_phase("rest")

        # Whole path is upward (peak at the origin) -> valley free.
        if up_len == n:
            is_valid = True
        else:
            peak = full_path[up_len - 1] # peak
            next_as = full_path[up_len] # peak 다음 peer이거나 혹은 peak의 customer야 함

            # case 1: peak->next_as is downward
            downward_reversed = full_path[up_len - 1: ][::-1] # peak부터 끝까지 뒤집어 valid upward 검증

            if filt(downward_reversed):
                is_valid = True
            # case 2: peak->next_as is peer
            elif self._is_peer_link(peak, next_as):
                # peak와 그 다음이 peer라면, peer 이후 구간만 뒤집어 upward 검증.
                after_peer = full_path[up_len:] # peer 다음 AS부터 origin까지
                # up_len == n-1이면 after_peer는 origin 하나뿐(단일 AS 구간)이라
                # 자명하게 upward이므로 filt() 호출을 생략해 probe 한 번을 아낀다.
                if len(after_peer) < 2:
                    is_valid = True
                else:
                    is_valid = filt(after_peer[::-1])
            else:
                is_valid = False

        return is_valid, up_len, get_phase_count("up_len"), get_phase_count("rest")

    def _is_peer_link(self, asn1: int, asn2: int) -> bool:
        """True iff asn1 and asn2 are connected by a peer (p2p) link.

        Looks asn1 up in the AS graph and checks whether asn2 is among its
        peers. The relation is symmetric, so checking one direction suffices.
        Returns False if asn1 is not present in the graph.

        Counted as one probe against the current phase, on par with a filt()
        call, since it is an equivalent lookup in the validity check.
        """
        _count_probe()
        as1 = self.as_.as_graph.as_dict.get(asn1)
        if as1 is None:
            return False
        return asn2 in as1.peer_asns
