from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import db, group, scan

SOURCE_LABELS = {
    "exif_original": "EXIF capture date",
    "exif_datetime": "EXIF date",
    "filename_datetime": "date and time from file name",
    "filename_date_mtime": "date from file name, time from file modification time",
    "filename_date": "date from file name only (time unknown)",
    "file_mtime": "file modification time only (time unknown)",
}


def cmd_scan(args) -> int:
    conn = db.connect(args.db)
    try:
        stats = scan.scan(
            Path(args.path).expanduser(),
            conn,
            progress=lambda s: print(f"\r  seen {s['seen']}", end="", file=sys.stderr),
            force=args.force,
        )
    except scan.LibraryRejected as exc:
        print(f"\nRefused: {exc}", file=sys.stderr)
        return 2
    print(file=sys.stderr)
    print(f"Scan: {stats['seen']} files · new {stats['new']} · updated {stats['updated']} · "
          f"unchanged {stats['skipped']} · errors {stats['errors']}")
    return 0


def cmd_group(args) -> int:
    conn = db.connect(args.db)
    r = group.build_groups(conn, args.session_gap, args.max_hamming)
    print(f"Grouped {r['photos']} photos into {r['groups']} groups across {r['sessions']} sessions "
          f"({r['day_buckets']} of them whole-day buckets for photos without a known time). "
          f"{r['groups_with_multiple']} groups hold more than one photo "
          f"({r['photos_in_multi_groups']} photos).")
    return 0


def cmd_stats(args) -> int:
    conn = db.connect(args.db)
    one = lambda q: conn.execute(q).fetchone()[0]
    print(f"Photos indexed: {one('SELECT COUNT(*) FROM photos WHERE error IS NULL')}")
    print(f"Unreadable files: {one('SELECT COUNT(*) FROM photos WHERE error IS NOT NULL')}")
    print("Exact copies (same SHA-256): "
          f"{one('SELECT COALESCE(SUM(n-1),0) FROM (SELECT COUNT(*) n FROM photos WHERE sha256 IS NOT NULL GROUP BY sha256 HAVING n>1)')}")
    print("Where the capture date comes from:")
    for src, n in conn.execute(
        "SELECT taken_source, COUNT(*) FROM photos WHERE error IS NULL GROUP BY taken_source ORDER BY 2 DESC"
    ):
        print(f"  {n:>6}  {SOURCE_LABELS.get(src, src)}")
    unknown = one("SELECT COUNT(*) FROM photos WHERE error IS NULL AND COALESCE(time_known,0) = 0")
    print(f"Photos without a trustworthy time of day: {unknown}")
    rng = conn.execute("SELECT MIN(taken_at), MAX(taken_at) FROM photos WHERE error IS NULL").fetchone()
    if rng[0]:
        print(f"Period: {rng[0][:10]} → {rng[1][:10]}")
    if args.errors:
        for r in conn.execute("SELECT path, error FROM photos WHERE error IS NOT NULL LIMIT 50"):
            print(f"  ! {r['path']}: {r['error']}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="curator", description="Local photo cataloguer (read-only).")
    p.add_argument("--db", default="curator.db", help="SQLite file (default: curator.db)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="index a folder of photos")
    s.add_argument("path")
    s.add_argument("--force", action="store_true", help="re-scan files even if unchanged")
    s.set_defaults(fn=cmd_scan)

    g = sub.add_parser("group", help="group by time session and visual similarity")
    g.add_argument("--session-gap", type=float, default=30.0, help="minutes of pause between sessions")
    g.add_argument("--max-hamming", type=int, default=10, help="max pHash distance (0-64)")
    g.set_defaults(fn=cmd_group)

    st = sub.add_parser("stats", help="summary of the database")
    st.add_argument("--errors", action="store_true", help="list unreadable files")
    st.set_defaults(fn=cmd_stats)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
