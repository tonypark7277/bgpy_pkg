# build_path_filter.py
#
# Step 1: count distinct contiguous sub-segments (length >= 2) of upward paths
#         from every origin leaf up to Tier-1 (input_clique).
#
# Currently uses STUBS_OR_MH (stubs + multihomed = all customer-less leaves)
# as the origin set. The STUBS-only comparison code is kept commented below.

import argparse
import sqlite3
import time
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
from struct import pack

from tqdm import tqdm

from bgpy.as_graphs.caida_as_graph import CAIDAASGraphConstructor
from bgpy.shared.enums import ASGroups

# Threshold derivation (see test_maxmin_all_t1.py):
#   For every reachable stub/MH, the longest "shortest c2p hops to a
#   reachable tier-1" we observed is 13 hops.  In node count that is
#   origin + 13 intermediates = 14 nodes total.  Paths longer than this
#   never reach a tier-1 in the CAIDA c2p subgraph, so we drop them.
MAX_HOPS_TO_TIER1 = 8
DEFAULT_MAX_PATH_LEN = MAX_HOPS_TO_TIER1 + 1   # node count cap (top..origin inclusive)
MIN_SEG_LEN = 2        # we only insert sub-segments of length >= 2
BATCH_SIZE = 20_000
SCHEMA_VERSION = 1


def db_path_for(max_path_len: int) -> Path:
    """Per-max-len SQLite store, e.g. bgpy_path_segments_max08.sqlite3."""
    return Path(__file__).resolve().with_name(
        f"bgpy_path_segments_max{max_path_len:02d}.sqlite3"
    )


# (a) Build topology once (uses CAIDA cache if present)
as_graph = CAIDAASGraphConstructor().run()


# (b) DFS upward from an origin AS. Yields tuples (top, ..., origin).
#
# A path is yielded ONLY if it terminates at a tier-1 (input_clique) AS.
# - Paths that hit the length cap before reaching a tier-1 are dropped
#   (would be unreachable in practice; see threshold derivation above).
# - Origins whose c2p closure never touches the clique (Category A
#   peer-only MH, Category B small isolated c2p islands) yield no paths.
def iter_upward_paths(
    start_as, max_len: int = DEFAULT_MAX_PATH_LEN
) -> Iterator[tuple[int, ...]]:
    def dfs(as_obj, partial_leaf_first):
        if as_obj.input_clique:
            yield tuple(reversed(partial_leaf_first))
            return
        if len(partial_leaf_first) >= max_len:
            return                                  # too long, drop
        for p in as_obj.providers:
            if p.asn in partial_leaf_first:         # loop guard
                continue
            yield from dfs(p, partial_leaf_first + [p.asn])

    yield from dfs(start_as, [start_as.asn])


# (c) Enumerate every contiguous sub-segment of length >= MIN_SEG_LEN.
def iter_subsegments(path: tuple[int, ...]) -> Iterator[tuple[int, ...]]:
    n = len(path)
    for i in range(n):
        for j in range(i + MIN_SEG_LEN, n + 1):
            yield path[i:j]


def segment_to_blob(seg: tuple[int, ...]) -> bytes:
    """Compact, collision-free segment encoding for SQLite storage."""
    return pack(f"!{len(seg)}I", *seg)


def open_segment_db(db_path: Path) -> tuple[sqlite3.Connection, Path]:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode = OFF")
    conn.execute("PRAGMA synchronous = OFF")
    conn.execute("PRAGMA temp_store = FILE")
    conn.execute("PRAGMA cache_size = -20000")
    return conn, db_path


def initialize_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS segments (
            seg_len INTEGER NOT NULL,
            seg BLOB NOT NULL,
            PRIMARY KEY (seg_len, seg)
        ) WITHOUT ROWID
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS build_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        ) WITHOUT ROWID
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS raw_counts (
            seg_len INTEGER PRIMARY KEY,
            raw_count INTEGER NOT NULL
        ) WITHOUT ROWID
        """
    )
    conn.commit()


def parse_args() -> tuple[int, int]:
    parser = argparse.ArgumentParser(
        description="Build distinct upward-path sub-segment SQLite store.",
    )
    parser.add_argument(
        "mode",
        nargs="?",
        choices=("1", "2"),
        default="1",
        help="1 = reuse existing DB if valid (default), 2 = force rebuild",
    )
    parser.add_argument(
        "-m",
        "--max-path-len",
        type=int,
        default=DEFAULT_MAX_PATH_LEN,
        help=(
            "max upward path length (node count cap, top..origin inclusive); "
            f"default {DEFAULT_MAX_PATH_LEN}"
        ),
    )
    args = parser.parse_args()
    if args.max_path_len < MIN_SEG_LEN:
        parser.error(f"--max-path-len must be >= MIN_SEG_LEN ({MIN_SEG_LEN})")
    return int(args.mode), args.max_path_len


def load_meta(conn: sqlite3.Connection) -> dict[str, str]:
    return {row[0]: row[1] for row in conn.execute("SELECT key, value FROM build_meta")}


def validate_existing_db(conn: sqlite3.Connection, max_path_len: int) -> bool:
    try:
        meta = load_meta(conn)
    except sqlite3.OperationalError:
        return False
    return (
        meta.get("schema_version") == str(SCHEMA_VERSION)
        and meta.get("max_path_len") == str(max_path_len)
        and meta.get("min_seg_len") == str(MIN_SEG_LEN)
    )


def store_report_meta(
    conn: sqlite3.Connection,
    max_path_len: int,
    paths_all: int,
    raw_all: int,
    per_len_raw_all: dict[int, int],
) -> None:
    conn.execute("DELETE FROM build_meta")
    conn.executemany(
        "INSERT OR REPLACE INTO build_meta(key, value) VALUES (?, ?)",
        [
            ("schema_version", str(SCHEMA_VERSION)),
            ("max_path_len", str(max_path_len)),
            ("min_seg_len", str(MIN_SEG_LEN)),
            ("paths_all", str(paths_all)),
            ("raw_all", str(raw_all)),
        ],
    )
    conn.execute("DELETE FROM raw_counts")
    conn.executemany(
        "INSERT INTO raw_counts(seg_len, raw_count) VALUES (?, ?)",
        sorted(per_len_raw_all.items()),
    )


def load_report_from_db(
    conn: sqlite3.Connection,
) -> tuple[int, int, int, dict[int, int], dict[int, int]]:
    meta = load_meta(conn)
    paths_all = int(meta.get("paths_all", "0"))
    raw_all = int(meta.get("raw_all", "0"))
    distinct_all = conn.execute("SELECT COUNT(*) FROM segments").fetchone()[0]
    per_len_distinct_all = {
        row[0]: row[1]
        for row in conn.execute(
            "SELECT seg_len, COUNT(*) FROM segments GROUP BY seg_len ORDER BY seg_len"
        )
    }
    per_len_raw_all = {
        row[0]: row[1]
        for row in conn.execute("SELECT seg_len, raw_count FROM raw_counts ORDER BY seg_len")
    }
    return paths_all, raw_all, distinct_all, per_len_distinct_all, per_len_raw_all


def flush_batch(conn: sqlite3.Connection, batch: list[tuple[int, bytes]]) -> None:
    if not batch:
        return
    # Explicitly close the cursor so PyPy's SQLite does not keep a statement
    # alive across commits.
    with closing(conn.cursor()) as cur:
        cur.executemany(
            "INSERT OR IGNORE INTO segments(seg_len, seg) VALUES (?, ?)",
            batch,
        )
    batch.clear()


def report(label: str, paths: int, raw: int, distinct: int, per_len_distinct: dict[int, int], per_len_raw: dict[int, int]) -> None:
    print(f"=== {label} ===")
    print(f"  upward paths enumerated : {paths:,}")
    print(f"  raw sub-segment inserts : {raw:,}")
    print(f"  DISTINCT sub-segments   : {distinct:,}")
    print(f"  breakdown:")
    print(f"    {'len':>4}  {'distinct':>15}  {'raw':>15}  {'dup_ratio':>10}")
    for k in sorted(per_len_raw):
        d = per_len_distinct.get(k, 0)
        r = per_len_raw[k]
        ratio = r / d if d else float("nan")
        print(f"    {k:>4}  {d:>15,}  {r:>15,}  {ratio:>10.2f}")
    print()


def build_database(conn: sqlite3.Connection, stubs_or_mh, max_path_len: int) -> tuple[int, int, int, dict[int, int], dict[int, int]]:
    raw_all = 0
    paths_all = 0
    per_len_raw_all: dict[int, int] = {}
    insert_batch: list[tuple[int, bytes]] = []
    total_origins = len(stubs_or_mh)
    last_heartbeat = time.monotonic()

    for origin_index, origin_as in enumerate(tqdm(stubs_or_mh, desc="origins"), start=1):
        for path in iter_upward_paths(origin_as, max_path_len):
            paths_all += 1
            for seg in iter_subsegments(path):
                seg_len = len(seg)
                raw_all += 1
                per_len_raw_all[seg_len] = per_len_raw_all.get(seg_len, 0) + 1
                insert_batch.append((seg_len, segment_to_blob(seg)))

                if len(insert_batch) >= BATCH_SIZE:
                    flush_batch(conn, insert_batch)

                # Keep the UI alive while a single origin takes a long time.
                now = time.monotonic()
                if now - last_heartbeat >= 2.0:
                    tqdm.write(
                        f"progress: origin={origin_index}/{total_origins} "
                        f"asn={origin_as.asn} paths={paths_all:,} raw={raw_all:,}",
                    )
                    last_heartbeat = now

    flush_batch(conn, insert_batch)
    store_report_meta(conn, max_path_len, paths_all, raw_all, per_len_raw_all)
    conn.commit()
    return load_report_from_db(conn)


def main() -> None:
    # (d) Origin set
    # stubs_only = as_graph.as_groups[ASGroups.STUBS.value]
    stubs_or_mh = as_graph.as_groups[ASGroups.STUBS_OR_MH.value]
    # stub_asns = frozenset(a.asn for a in stubs_only)
    # assert stub_asns.issubset({a.asn for a in stubs_or_mh})

    # print(f"STUBS         : {len(stubs_only):,}")
    run_mode, max_path_len = parse_args()
    db_path = db_path_for(max_path_len)

    print(f"STUBS_OR_MH   : {len(stubs_or_mh):,}")
    print(f"max upward path length: {max_path_len}, min sub-segment length: {MIN_SEG_LEN}")
    print()

    db_existed = db_path.exists()
    if run_mode == 2 and db_existed:
        db_path.unlink()
        db_existed = False

    conn, db_path = open_segment_db(db_path)
    print(f"SQLite distinct store: {db_path}")
    print()

    try:
        initialize_schema(conn)
        if run_mode == 1 and db_existed and validate_existing_db(conn, max_path_len):
            paths_all, raw_all, distinct_all, per_len_distinct_all, per_len_raw_all = load_report_from_db(conn)
        else:
            if db_existed:
                conn.close()
                db_path.unlink(missing_ok=True)
                conn, db_path = open_segment_db(db_path)
                initialize_schema(conn)
            paths_all, raw_all, distinct_all, per_len_distinct_all, per_len_raw_all = build_database(conn, stubs_or_mh, max_path_len)
    finally:
        conn.close()

    print()
    report(
        "STUBS_OR_MH",
        paths_all,
        raw_all,
        distinct_all,
        per_len_distinct_all,
        per_len_raw_all,
    )


if __name__ == "__main__":
    main()
