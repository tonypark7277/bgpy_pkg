"""Multi-filter API for pybloomfiltermmap3-backed Bloom filters.

Extends bloom_filter_pbf_user.py to support pre-loading and querying
multiple filters (max08..max11) within a single process.

Usage:
    from bgpy.bloom_filter_pbf_multi import load_filters, query_with

    filters = load_filters()             # load all four (08, 09, 10, 11)
    filters = load_filters([8, 10])      # load only max08 and max10

    bf, meta = filters[10]
    hit = query_with(bf, meta, [65000, 3356, 174])  # origin-first ASN list
"""

import json
import os
import struct
from pathlib import Path

try:
    from pybloomfilter import BloomFilter
except ImportError as exc:
    raise SystemExit(
        "pybloomfiltermmap3 not installed.  Install with:\n"
        "    pip install pybloomfiltermmap3\n"
        f"(import error: {exc})"
    )

from bgpy.bloom_filter_pbf_user import _encode_key

# Directory holding the prebuilt bloom filters. Defaults to the bgpy package
# directory (where build_pybloom_filter.py writes them), so a fresh clone uses
# its own filters. Override with the BGPY_FILTER_DIR env var to load from
# elsewhere (e.g. the UpPathDB_backup folder of prebuilt filters).
BASE_DIR = Path(os.environ.get("BGPY_FILTER_DIR") or Path(__file__).resolve().parent)

FILTER_REGISTRY: dict[int, tuple[Path, Path]] = {
    8:  (BASE_DIR / "bgpy_path_filter_pbf_max08.bloom",
         BASE_DIR / "bgpy_path_filter_pbf_max08.meta.json"),
    9:  (BASE_DIR / "bgpy_path_filter_pbf_max09.bloom",
         BASE_DIR / "bgpy_path_filter_pbf_max09.meta.json"),
    10: (BASE_DIR / "bgpy_path_filter_pbf_max10.bloom",
         BASE_DIR / "bgpy_path_filter_pbf_max10.meta.json"),
    11: (BASE_DIR / "bgpy_path_filter_pbf_max11.bloom",
         BASE_DIR / "bgpy_path_filter_pbf_max11.meta.json"),
}


def load_filters(
    max_hops: list[int] | None = None,
) -> dict[int, tuple]:
    """Open and return {max_hop: (BloomFilter, meta)} for the requested hops.

    Filters are kept open (mmap-backed). The caller is responsible for
    closing them when done by calling bf.close() on each BloomFilter.
    Defaults to all registered filters: [8, 9, 10, 11].
    """
    if max_hops is None:
        max_hops = list(FILTER_REGISTRY)
    loaded: dict[int, tuple] = {}
    for hop in max_hops:
        if hop not in FILTER_REGISTRY:
            raise ValueError(f"max_hop={hop} not in FILTER_REGISTRY; valid: {list(FILTER_REGISTRY)}")
        fp, mp = FILTER_REGISTRY[hop]
        if not fp.exists() or not mp.exists():
            raise FileNotFoundError(f"filter for max_hop={hop} not found: {fp}")
        meta = json.loads(mp.read_text())
        loaded[hop] = (BloomFilter.open(str(fp)), meta)
    return loaded


def query_with(bf, meta: dict, asns) -> bool:
    """Query a pre-loaded (BloomFilter, meta) pair without open/close overhead."""
    seed_prefix = struct.pack("!Q", meta["seed"])
    return _encode_key(seed_prefix, asns) in bf
