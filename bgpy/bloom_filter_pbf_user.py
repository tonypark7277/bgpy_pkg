"""Build a Bloom filter over the path-segment SQLite DB, using
pybloomfiltermmap3 (C MurmurHash3, mmap-backed).

Install:    pip install pybloomfiltermmap3
Note:       this is a CPython C extension and is generally not
            compatible with PyPy.  Run with CPython.

Encoding (fixed-width binary, unambiguous because every ASN is exactly
4 bytes -- two distinct ASN sequences can never produce the same byte
string, so no delimiter is needed):

    seed(u64 big-endian) || asn1(u32 BE) || asn2(u32 BE) || ... || asnN(u32 BE)

The seed is drawn once at build time with os.urandom and persisted in
the meta JSON; it salts the filter so two builds produce different
filters (and arbitrary external segments cannot collide deterministically).

Segment order:
    The SQLite DB stores segments TOP-FIRST (tier-1 ASN first, origin
    ASN last).  This builder REVERSES each segment before insertion so
    the filter is keyed in CUSTOMER->PROVIDER (origin-first) order --
    the natural direction of a BGP announcement.  The query CLI / API
    therefore expects origin-first input.

Usage:
    build_pybloom_filter.py build [target_fpr]    # default 0.001
    build_pybloom_filter.py verify [n]            # sanity check N rows
    build_pybloom_filter.py query 65000 3356 174  # origin-first (stub..tier-1)
    build_pybloom_filter.py stats                 # show meta + file size
"""

import json
import os
import sqlite3
import struct
import sys
import time
from pathlib import Path

try:
    from pybloomfilter import BloomFilter
except ImportError as exc:
    raise SystemExit(
        "pybloomfiltermmap3 not installed.  Install with:\n"
        "    pip install pybloomfiltermmap3\n"
        f"(import error: {exc})"
    )

# DB_PATH     = Path(__file__).resolve().with_name("bgpy_path_segments.sqlite3")
# FILTER_PATH = Path(__file__).resolve().with_name("bgpy_path_filter_pbf.bloom")
# META_PATH   = Path(__file__).resolve().with_name("bgpy_path_filter_pbf.meta.json")

BASE_DIR = Path("/home/BGPfilter/UpPathDB_backup")

DB_PATH     = BASE_DIR / "bgpy_path_segments_max10.sqlite3"
FILTER_PATH = BASE_DIR / "bgpy_path_filter_pbf_max10.bloom"
META_PATH   = BASE_DIR / "bgpy_path_filter_pbf_max10.meta.json"

DEFAULT_FPR = 0.001
SCHEMA_VERSION = 1


# ----- Key encoding ------------------------------------------------------
def _encode_key(seed_prefix: bytes, asns) -> bytes:
    """seed(u64 BE) || asn1(u32 BE) || asn2(u32 BE) || ... || asnN(u32 BE)."""
    return seed_prefix + struct.pack(f"!{len(asns)}I", *asns)


# ----- Build -------------------------------------------------------------
# def build(target_fpr: float = DEFAULT_FPR) -> None:
#     if not DB_PATH.exists():
#         raise SystemExit(f"DB not found: {DB_PATH}")

#     conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
#     conn.execute("PRAGMA query_only = ON")
#     n = conn.execute("SELECT COUNT(*) FROM segments").fetchone()[0]
#     if n == 0:
#         raise SystemExit("DB is empty")

#     print(f"distinct segments    : {n:,}")
#     print(f"target FPR           : {target_fpr}")

#     # 64-bit seed drawn from OS entropy
#     seed = int.from_bytes(os.urandom(8), "big")
#     seed_hex = f"{seed:016x}"
#     seed_prefix = struct.pack("!Q", seed)
#     print(f"seed                 : 0x{seed_hex}")
#     print(f"filter path          : {FILTER_PATH}")

#     # Remove any prior filter file before constructing (pybloomfilter
#     # mmaps the file; reusing a stale file with different parameters
#     # produces silent corruption).
#     if FILTER_PATH.exists():
#         FILTER_PATH.unlink()

#     bf = BloomFilter(n, target_fpr, str(FILTER_PATH))
#     # Try to report planned filter size if attributes are exposed.
#     try:
#         bits = bf.num_bits
#         print(f"bloom m (bits)       : {bits:,}  "
#               f"(= {bits / 8 / (1 << 30):.2f} GiB)")
#     except AttributeError:
#         pass
#     try:
#         print(f"bloom k (hash funcs) : {bf.num_hashes}")
#     except AttributeError:
#         pass
#     print()

#     inserted = 0
#     start = time.monotonic()
#     last_heartbeat = start
#     HEARTBEAT_SEC = 5.0

#     # DB stores segments in provider->customer (top-first) order.  We
#     # want the filter keyed in customer->provider (origin-first) order,
#     # so we reverse the 4-byte chunks in each blob before insertion.
#     # Byte-level chunk reversal avoids per-row struct unpack/repack.
#     cur = conn.execute("SELECT seg FROM segments")
#     try:
#         for (seg_blob,) in cur:
#             blob_len = len(seg_blob)
#             reversed_blob = b"".join(
#                 seg_blob[i:i + 4] for i in range(blob_len - 4, -1, -4)
#             )
#             bf.add(seed_prefix + reversed_blob)
#             inserted += 1
#             if inserted % 1_000_000 == 0:
#                 now = time.monotonic()
#                 if now - last_heartbeat >= HEARTBEAT_SEC:
#                     rate = inserted / (now - start)
#                     eta_min = (n - inserted) / rate / 60
#                     print(f"  {inserted:>13,} / {n:,} "
#                           f"({100 * inserted / n:5.1f}%)  "
#                           f"rate={rate / 1e6:.2f}M/s  "
#                           f"ETA={eta_min:6.1f} min")
#                     last_heartbeat = now
#     finally:
#         cur.close()
#         conn.close()

#     try:
#         bf.sync()
#     except AttributeError:
#         pass
#     bf.close()

#     elapsed = time.monotonic() - start
#     actual_size = FILTER_PATH.stat().st_size

#     print()
#     print(f"inserted             : {inserted:,}")
#     print(f"build time           : {elapsed / 60:.1f} min")
#     print(f"effective rate       : {inserted / elapsed / 1e6:.2f}M inserts/s")
#     print(f"filter on disk       : {actual_size:,} bytes "
#           f"({actual_size / (1 << 30):.2f} GiB)")

#     meta = {
#         "schema_version":  SCHEMA_VERSION,
#         "n_inserted":      inserted,
#         "fpr_target":      target_fpr,
#         "seed":            seed,
#         "seed_hex":        seed_hex,
#         "key_encoding":    ("seed(u64 big-endian) || asn1 || asn2 || "
#                             "... || asnN (each u32 big-endian); fixed-width, "
#                             "no delimiter"),
#         "segment_order":   ("origin-first / customer->provider (origin ASN "
#                             "first, tier-1 ASN last); DB is top-first so the "
#                             "builder reverses each segment before inserting"),
#         "library":         "pybloomfiltermmap3",
#         "filter_path":     str(FILTER_PATH),
#         "db_path":         str(DB_PATH),
#         "filter_bytes":    actual_size,
#         "build_seconds":   elapsed,
#     }
#     META_PATH.write_text(json.dumps(meta, indent=2))
#     print(f"meta written         : {META_PATH}")


# ----- Read helpers ------------------------------------------------------
def _load_for_read():
    if not META_PATH.exists() or not FILTER_PATH.exists():
        raise SystemExit(
            f"filter not built yet; run "
            f"`python {Path(__file__).name} build` first"
        )
    meta = json.loads(META_PATH.read_text())
    bf = BloomFilter.open(str(FILTER_PATH))
    return bf, meta


def query(asns) -> bool:
    bf, meta = _load_for_read()
    try:
        seed_prefix = struct.pack("!Q", meta["seed"])
        return _encode_key(seed_prefix, asns) in bf
    finally:
        bf.close()


# ----- Diagnostics -------------------------------------------------------
def verify(n: int = 200) -> None:
    """Pull N rows from the DB and check they all HIT the filter.

    A false negative (= a stored item missing from the filter) is
    mathematically impossible for a Bloom filter, so any miss here
    means an encoding mismatch between build and query.
    """
    bf, meta = _load_for_read()
    seed_prefix = struct.pack("!Q", meta["seed"])
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    hits = 0
    backwards_hits = 0         # also HIT if queried in DB's top-first order
    miss_examples = []
    try:
        for seg_len, seg_blob in conn.execute(
            f"SELECT seg_len, seg FROM segments ORDER BY RANDOM() LIMIT {n}"
        ):
            asns_top_first    = struct.unpack(f"!{seg_len}I", seg_blob)
            asns_origin_first = asns_top_first[::-1]
            # Filter is keyed in customer->provider (origin-first) order:
            if _encode_key(seed_prefix, asns_origin_first) in bf:
                hits += 1
            else:
                miss_examples.append(asns_origin_first)
            if _encode_key(seed_prefix, asns_top_first) in bf:
                backwards_hits += 1
    finally:
        conn.close()
        bf.close()

    print(f"origin-first (filter's order) hits: {hits}/{n}")
    print(f"top-first    (DB's    order)  hits: {backwards_hits}/{n}  "
          f"(expected to be ~= FPR * n)")
    if miss_examples:
        print("\nUNEXPECTED MISSES (this means encoding mismatch):")
        for m in miss_examples[:5]:
            print(f"  {m}")
    else:
        print("\nOK: every DB segment hits the filter when queried with "
              "customer->provider (origin-first) order.")


def stats() -> None:
    if not META_PATH.exists():
        raise SystemExit(f"meta not found: {META_PATH}")
    print(META_PATH.read_text())
    if FILTER_PATH.exists():
        sz = FILTER_PATH.stat().st_size
        print(f"\nfilter file on disk: {sz:,} bytes ({sz / (1 << 30):.2f} GiB)")


# ----- CLI ---------------------------------------------------------------
def _usage() -> None:
    print(__doc__)
    sys.exit(2)


def main() -> None:
    if len(sys.argv) < 2:
        _usage()
    cmd = sys.argv[1]
    if cmd == "build":
        fpr = float(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_FPR
        build(fpr)
    elif cmd == "query":
        if len(sys.argv) < 3:
            raise SystemExit("query needs at least one ASN")
        asns = [int(x) for x in sys.argv[2:]]
        hit = query(asns)
        print("HIT" if hit else "MISS")
        sys.exit(0 if hit else 1)
    elif cmd == "verify":
        nn = int(sys.argv[2]) if len(sys.argv) > 2 else 200
        verify(nn)
    elif cmd == "stats":
        stats()
    else:
        _usage()


if __name__ == "__main__":
    main()
