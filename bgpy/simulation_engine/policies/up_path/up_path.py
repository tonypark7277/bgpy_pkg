from typing import TYPE_CHECKING

from bgpy.simulation_engine.policies.rov import ROV

if TYPE_CHECKING:
    from bgpy.shared.enums import Relationships
    from bgpy.simulation_engine import Announcement as Ann

## due to change of filter caller
from bgpy.bloom_filter_pbf_user import _load_for_read
from bgpy.bloom_filter_pbf_user import _encode_key
import struct
from multiprocessing import Value as _mp_Value


# Counts how many times `filt` is called during a simulation run.
#
# The simulation parallelizes trials across a multiprocessing.Pool, so each
# worker is a *forked* process (Linux default). A plain module-level int would
# be copied into every fork and the per-worker totals would be lost when the
# pool tears the workers down. Backing the counter with a multiprocessing.Value
# (shared memory) means every forked worker increments the *same* counter, so
# the parent can read the grand total after the simulation finishes.
#
# It must be created here at import time (before the Pool forks) for the shared
# memory to be inherited by the workers.
#### filter counter
_filt_call_count = _mp_Value("q", 0)


def get_filt_call_count() -> int:
    """Return the total number of `filt` calls so far across all processes."""
    return _filt_call_count.value


def reset_filt_call_count() -> None:
    """Reset the global `filt` call counter back to zero."""
    with _filt_call_count.get_lock():
        _filt_call_count.value = 0
####


# Process-wide Bloom filter over the set of known strictly-upward path
# segments (built once, offline, by build_bloom_filter.py). It is loaded
# lazily on first use and then kept open (mmap-backed) for the lifetime of
# the process so every `filt` probe is just a handful of memory reads.
# _bloom_filter = None

# def _get_bloom_filter():
#     """Return the shared, lazily-loaded BloomFilter, opening it on first use."""
#     global _bloom_filter

#     if _bloom_filter is None:
#         # Imported lazily so simply importing this module (e.g. for tests that
#         # never touch the filter) does not require the .bloom file to exist.
#         from bgpy.bloom_filter_pbf_user import _load_for_read

#         _bloom_filter = _load_for_read()
#     return _bloom_filter

_bloom_filter = None
_bloom_meta = None

def _get_bloom_filter():
    """Return the shared, lazily-loaded BloomFilter, opening it on first use."""
    global _bloom_filter
    global _bloom_meta

    _bloom_filter, _bloom_meta = _load_for_read()

    return _bloom_filter, _bloom_meta


# def filt(path: tuple[int, ...]) -> bool:
#     if len(path) < 2:
#         return True

#     tmp_reversed = path[::-1]
#     return _get_bloom_filter().contains(tmp_reversed)


def filt(path: tuple[int, ...]) -> bool:
    """True iff `path` is a known strictly-upward segment (every hop c->p).

    The upward segments were enumerated offline and stored in a Bloom filter;
    here we just test membership. `path` is a sequence of ASNs in upward order
    (as read from the receiver toward a provider). It is handed straight to
    BloomFilter.contains, which packs each ASN as a u32 big-endian after the
    persisted seed -- matching exactly how keys were inserted at build time.

    A segment of fewer than two ASNs has no link to validate, so it is trivially
    upward and we short-circuit to True (also avoiding a needless probe).

    Note the filter is one-sided: a True result may be a false positive (bounded
    by the build-time target FPR), but a False result is authoritative -- that
    segment was definitely never inserted, i.e. it is not a valid upward path.
    """
    #### Count every invocation (shared across all forked worker processes).
    #### filter counter
    with _filt_call_count.get_lock():
        _filt_call_count.value += 1
    ####

    if len(path) < 2:
        return True

    if _bloom_filter is None or _bloom_meta is None:
        # Imported lazily
        _get_bloom_filter()

    # try:
    #     seed_prefix = struct.pack("!Q", _bloom_meta["seed"])
    #     return _encode_key(seed_prefix, path) in _bloom_filter
    # finally:
    #     _bloom_filter.close()
    
    seed_prefix = struct.pack("!Q", _bloom_meta["seed"])
    return _encode_key(seed_prefix, path) in _bloom_filter

##

def longest_upward_prefix(full_path: tuple[int, ...]) -> int:
    """filt(full_path[:L]) 가 True인 가장 큰 L."""
    n = len(full_path)
    # gallop: prefix(hi)가 invalid가 될 때까지 2배씩, lo는 마지막 valid
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

##

class UpPath(ROV):

    name: str = "UpPath"

    def _valid_ann(self, ann: "Ann", recv_rel: "Relationships") -> bool:
        """Returns announcement validity

        return false if invalid by filter confirmation of segmented path
        otherwise additionally use standard ROV to determine validity
        """

        # if not valid by up_path (can be confirmed by filter), return false
        if not self.ann_is_valid_by_up_path(ann):
            return False
        # if valid by up_path, additionally use standard ROV to determine check origin 
        else:
            return super()._valid_ann(ann, recv_rel)


    def ann_is_valid_by_up_path(self, ann: "Ann") -> bool:
        """True iff the announcement's path is a valid valley-free path.

        """
        # Receiver at index 0, origin at the last index.
        full_path = (self.as_.asn, *ann.as_path)

        ######## here after0 ########

        # n = len(full_path)

        # # ---- Phase 1: halve the start side to find some valid upward prefix 
        # # halving을 통해 가장 먼저 filter에 의해 valid한 것으로 확인되는 seg 추출
        # # 길이 2에서 멈춤. 길이 2짜리가 valid이면 up_len = 2, 아니면 up_len = 1 (이 경우 자신이 peak일 것으로 추정)
        # seg = full_path
        # up_len = 1 ## <-------------------------------------------------------------
        # while len(seg) > 2:
        #     seg = seg[: max(2, len(seg) // 2)]
        #     if filt(seg):
        #         up_len = len(seg) ## <-------------------------------------------------------------
        #         break

        # # up_len = len(seg) if filt(seg) else 1 ## <-------------------------------------------------------------

        # # 확인된 valid upward path에서 하나씩 늘리며 늘린 놈도 valid upward path인지 확인
        # while up_len < n and filt(full_path[: up_len+1]):
        #     up_len += 1
        
        ######## here after1 ########

        # n = len(full_path)
        # up_len = len(full_path)
        # while up_len > 1 and not filt(full_path[:up_len]):
        #     up_len //= 2

        # if up_len != 1:
        #     plus_len = 0
        #     tmp = up_len // 2

        #     while tmp > 0:
        #         candidate = up_len + plus_len + tmp
        #         if candidate <= n and filt(full_path[:candidate]):
        #             plus_len += tmp
        #         tmp //= 2

        #     up_len += plus_len

        # while up_len < n and filt(full_path[:up_len + 1]):
        #     up_len += 1

        ######## here after2 ########

        # n = len(full_path)
        # up_len = 1
        # while up_len < n:
        #     next_len = min(up_len*2, n)
            
        #     if filt(full_path[:next_len]):
        #         up_len = next_len
        #     else:
        #         break    
        
        # if up_len != 1:
        #     while up_len < n and filt(full_path[:up_len + 1]):
        #         up_len += 1

        ######## here after3 ########
        n = len(full_path)
        up_len = longest_upward_prefix(full_path)

        ######## here before ########

        # Whole path is upward (peak at the origin) -> valley free.
        if up_len == n:
            return True

        peak = full_path[up_len - 1] # peak
        next_as = full_path[up_len] # peak 다음 peer이거나 혹은 peak의 customer야 함 

        # case 1: peak->next_as is downward
        downward_reversed = full_path[up_len - 1: ][::-1] # peak부터 끝까지 뒤집어 valid upward 검증

        if filt(downward_reversed):
            return True

        # case 2: peak->next_as is peer
        if self._is_peer_link(peak, next_as):
            # peak와 그 다음이 peer라면 
            after_peer_downward_reversed = full_path[up_len:][::-1] # peak 다음부터 끝까지 뒤집어 valid upward 검증

            return filt(after_peer_downward_reversed)

        return False

    def _is_peer_link(self, asn1: int, asn2: int) -> bool:
        """True iff asn1 and asn2 are connected by a peer (p2p) link.

        Looks asn1 up in the AS graph and checks whether asn2 is among its
        peers. The relation is symmetric, so checking one direction suffices.
        Returns False if asn1 is not present in the graph.
        """
        as1 = self.as_.as_graph.as_dict.get(asn1)
        if as1 is None:
            return False
        return asn2 in as1.peer_asns
