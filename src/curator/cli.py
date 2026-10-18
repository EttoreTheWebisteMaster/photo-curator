from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

from . import benchmark, db, embeddings, group, scan

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
    try:
        r = group.build_groups(
            conn,
            session_gap_min=args.session_gap,
            method=args.method,
            model=args.model,
            max_hamming=args.max_hamming,
            cosine_threshold=args.cosine_threshold,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"Grouped {r['photos']} photos into {r['groups']} groups across {r['sessions']} sessions "
          f"({r['day_buckets']} whole-day buckets for photos without a known time). "
          f"{r['groups_with_multiple']} groups hold more than one photo "
          f"({r['photos_in_multi_groups']} photos). method={args.method}"
          + (f", model={args.model}" if args.model else ""))
    return 0


def cmd_embed(args) -> int:
    conn = db.connect(args.db)
    try:
        stats = embeddings.embed_folder(
            conn, args.model,
            batch_size=args.batch_size,
            device=args.device,
            progress=lambda s: print(
                f"\r  embedded {s['embedded']}/{s['candidates']}",
                end="", file=sys.stderr,
            ),
        )
    except ImportError as exc:
        print(f"\nEmbeddings need optional deps: pip install -e '.[embeddings]'\n  {exc}",
              file=sys.stderr)
        return 2
    print(file=sys.stderr)
    print(f"Embedded {stats['embedded']} photos with {args.model} "
          f"(errors {stats['errors']}, candidates {stats['candidates']})")
    return 0


def cmd_groups(args) -> int:
    conn = db.connect(args.db)
    rows = conn.execute(
        "SELECT g.group_id AS gid, COUNT(*) AS n, "
        "       GROUP_CONCAT(p.path, char(10)) AS paths "
        "FROM groups g JOIN photos p ON p.id = g.photo_id "
        "GROUP BY g.group_id HAVING n >= ? "
        "ORDER BY n DESC, g.group_id",
        (args.min_size,),
    ).fetchall()
    if not rows:
        print("No groups yet (or none with the requested minimum size).", file=sys.stderr)
        return 1
    for r in rows:
        print(f"Group {r['gid']} ({r['n']} photos):")
        for path in r["paths"].split("\n"):
            print(f"  {path}")
    return 0


def cmd_label(args) -> int:
    conn = db.connect(args.db)
    photo = conn.execute(
        "SELECT id FROM photos WHERE path = ?",
        (str(Path(args.photo).expanduser().resolve()),),
    ).fetchone()
    if photo is None:
        print(f"Photo not indexed: {args.photo}", file=sys.stderr)
        return 1
    in_group = conn.execute(
        "SELECT 1 FROM groups WHERE photo_id = ? AND group_id = ?",
        (photo["id"], args.group),
    ).fetchone()
    if in_group is None:
        print(f"Photo {photo['id']} is not in group {args.group}", file=sys.stderr)
        return 1
    conn.execute(
        "INSERT OR REPLACE INTO labels (group_id, photo_id, label, reason, created_at) "
        "VALUES (?,?,?,?,?)",
        (args.group, photo["id"], args.label, args.reason,
         datetime.now().isoformat(timespec="seconds")),
    )
    conn.commit()
    print(f"Labeled photo {photo['id']} in group {args.group} as {args.label}")
    return 0


def cmd_benchmark(args) -> int:
    conn = db.connect(args.db)
    scorers = benchmark.default_scorers(model=args.model)
    results = benchmark.run_scorer_benchmark(conn, scorers, model=args.model)
    if not any(r["total"] for r in results.values()):
        print("No groups with a 'winner' label yet. "
              "Use 'curator label --group N --photo PATH --label winner' first.",
              file=sys.stderr)
        return 1
    print(f"{'scorer':<20} {'hits':>6} {'groups':>7} {'accuracy':>9}")
    for name, r in sorted(results.items(), key=lambda kv: -kv[1]["accuracy"]):
        print(f"{name:<20} {r['hits']:>6} {r['total']:>7} {r['accuracy']:>9.1%}")
    run_id = benchmark.save_benchmark(conn, "scorer", results)
    print(f"(saved as run {run_id})")
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
        "SELECT taken_source, COUNT(*) FROM photos WHERE error IS NULL "
        "GROUP BY taken_source ORDER BY 2 DESC"
    ):
        print(f"  {n:>6}  {SOURCE_LABELS.get(src, src)}")
    unknown = one("SELECT COUNT(*) FROM photos WHERE error IS NULL AND COALESCE(time_known,0) = 0")
    print(f"Photos without a trustworthy time of day: {unknown}")
    rng = conn.execute("SELECT MIN(taken_at), MAX(taken_at) FROM photos WHERE error IS NULL").fetchone()
    if rng[0]:
        print(f"Period: {rng[0][:10]} → {rng[1][:10]}")
    emb = conn.execute(
        "SELECT model, COUNT(*) FROM embeddings GROUP BY model ORDER BY model"
    ).fetchall()
    if emb:
        print("Embeddings indexed:")
        for r in emb:
            print(f"  {r[1]:>6}  {r[0]}")
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

    g = sub.add_parser("group", help="group by time session and similarity")
    g.add_argument("--session-gap", type=float, default=30.0,
                   help="minutes of pause between sessions")
    g.add_argument("--method", choices=["phash", "embeddings", "combined"], default="phash")
    g.add_argument("--model", help="embedding model, required for method=embeddings/combined")
    g.add_argument("--max-hamming", type=int, default=10)
    g.add_argument("--cosine-threshold", type=float, default=0.20,
                   help="embedding cutoff on 1 - cosine similarity (0.20 ≈ 0.80 similarity)")
    g.set_defaults(fn=cmd_group)

    e = sub.add_parser("embed", help="compute visual embeddings (DINOv2 / CLIP)")
    e.add_argument("--model", default="dinov2_vits14", choices=sorted(embeddings.EMBEDDERS))
    e.add_argument("--batch-size", type=int, default=8,
                   help="photos per forward pass (8 is safe on 8 GB M1)")
    e.add_argument("--device", default="auto", choices=["auto", "mps", "cpu"])
    e.set_defaults(fn=cmd_embed)

    gs = sub.add_parser("groups", help="list groups so you can pick winners")
    gs.add_argument("--min-size", type=int, default=2)
    gs.set_defaults(fn=cmd_groups)

    lab = sub.add_parser("label", help="label a photo within a group (winner / forse / rejected)")
    lab.add_argument("--group", type=int, required=True)
    lab.add_argument("--photo", required=True)
    lab.add_argument("--label", choices=["winner", "forse", "rejected"], default="winner")
    lab.add_argument("--reason", default=None)
    lab.set_defaults(fn=cmd_label)

    b = sub.add_parser("benchmark", help="evaluate scorers on your labeled groups")
    b.add_argument("--model", default=None, help="adds emb_centroid when set")
    b.set_defaults(fn=cmd_benchmark)

    st = sub.add_parser("stats", help="summary of the database")
    st.add_argument("--errors", action="store_true")
    st.set_defaults(fn=cmd_stats)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())