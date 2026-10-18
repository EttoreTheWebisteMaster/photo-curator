"""Commit planning: decide what moves where, without touching any file.

Step 5a exposes `plan_commit()` (used by /api/commit/preview) which returns a
`CommitPlan` listing every move, every error, and the stats for the summary UI.
Step 5b (`execute_commit`) will consume the same plan.

Invariants:
  - Never touches the filesystem in plan_commit (only reads to check existence).
  - Deterministic: same DB + same filesystem state → same plan.
  - Live Photo companions (`.MOV` next to a `.HEIC` with the same stem) are
    paired and share the same destination stem.
  - The NNN counter is per-day and shared between `_curator/winner/` and
    `_curator/rejected/`, so a still and its companion share the same NNN.
"""
from __future__ import annotations

import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .scan import sha256_file as _sha256_file

HEIC_EXTENSIONS = {".heic", ".heif", ".HEIC", ".HEIF"}
COMPANION_EXTS = (".MOV", ".mov")


@dataclass
class Move:
    src_path: Path
    dst_path: Path
    role: str                    # "winner" | "rejected"
    photo_id: int | None         # None for the .MOV companion
    companion_of: int | None     # photo_id of the still, when this is the .MOV
    sha256_expected: str | None  # from DB for the still; None for the companion


@dataclass
class CommitPlan:
    source_root: Path
    curator_root: Path
    moves: list[Move]
    errors: list[str]
    stats: dict

    @property
    def has_errors(self) -> bool:
        return bool(self.errors)


def _is_under(path_str: str, root: Path) -> bool:
    try:
        return Path(path_str).resolve().is_relative_to(root)
    except (ValueError, OSError):
        return False


def _find_companion(still_path: Path) -> Path | None:
    """Return the .MOV companion if this looks like an iPhone Live Photo."""
    if still_path.suffix not in HEIC_EXTENSIONS:
        return None
    for ext in COMPANION_EXTS:
        candidate = still_path.with_suffix(ext)
        if candidate.exists() and candidate.is_file():
            return candidate
    return None


def _existing_max_per_day(curator_root: Path) -> dict[str, int]:
    """Return {YYYYMMDD: max NNN} from existing files in _curator/{winner,rejected}."""
    out: dict[str, int] = {}
    if not curator_root.exists():
        return out
    for sub in ("winner", "rejected"):
        d = curator_root / sub
        if not d.exists():
            continue
        for f in d.iterdir():
            if not f.is_file():
                continue
            parts = f.stem.split("_")
            if len(parts) != 3:
                continue
            day, _time, nnn = parts
            if len(day) != 8 or not day.isdigit() or not nnn.isdigit():
                continue
            n = int(nnn)
            if n > out.get(day, 0):
                out[day] = n
    return out


def _collect_candidates(conn, source_root: Path) -> tuple[list[tuple[dict, str]], list[str]]:
    errors: list[str] = []

    pending = conn.execute(
        "SELECT COUNT(*) FROM group_states WHERE status='pending'"
    ).fetchone()[0]
    if pending > 0:
        errors.append(
            f"{pending} group(s) still pending. Review all groups before committing."
        )

    winners = conn.execute(
        """
        SELECT p.* FROM photos p
        JOIN group_states gs ON gs.current_winner_photo_id = p.id
        WHERE gs.status = 'completed'
          AND p.error IS NULL
          AND p.committed_at IS NULL
        """
    ).fetchall()

    losers = conn.execute(
        """
        SELECT p.* FROM photos p
        JOIN groups g ON g.photo_id = p.id
        JOIN group_states gs ON gs.group_id = g.group_id
        WHERE p.error IS NULL
          AND p.committed_at IS NULL
          AND (
              gs.status = 'deleted'
              OR (gs.status = 'completed'
                  AND (gs.current_winner_photo_id IS NULL
                       OR p.id != gs.current_winner_photo_id))
          )
        """
    ).fetchall()

    utility_rejected = conn.execute(
        """
        SELECT * FROM photos
        WHERE error IS NULL
          AND committed_at IS NULL
          AND COALESCE(is_utility, 0) = 1
          AND rejected_reviewed_at IS NOT NULL
        """
    ).fetchall()

    seen: set[int] = set()
    candidates: list[tuple[dict, str]] = []
    for row, role in ([(r, "winner") for r in winners]
                      + [(r, "rejected") for r in losers]
                      + [(r, "rejected") for r in utility_rejected]):
        if row["id"] in seen:
            continue
        if not _is_under(row["path"], source_root):
            continue
        seen.add(row["id"])
        candidates.append((dict(row), role))
    return candidates, errors


def plan_commit(conn, source_root: Path) -> CommitPlan:
    source_root = source_root.resolve()
    curator_root = source_root / "_curator"

    candidates, errors = _collect_candidates(conn, source_root)

    by_day: dict[str, list[tuple[dict, str]]] = {}
    for row, role in candidates:
        taken = row["taken_at"] or ""
        day = taken[:10].replace("-", "")
        if len(day) != 8 or not day.isdigit():
            errors.append(f"photo {row['id']} has no valid taken_at")
            continue
        by_day.setdefault(day, []).append((row, role))

    existing = _existing_max_per_day(curator_root)

    moves: list[Move] = []
    for day, photos in sorted(by_day.items()):
        next_nnn = existing.get(day, 0) + 1
        photos.sort(key=lambda x: (0 if x[1] == "winner" else 1,
                                   x[0]["taken_at"] or "",
                                   x[0]["id"]))
        for row, role in photos:
            pid = row["id"]
            src = Path(row["path"])
            if not src.exists():
                errors.append(f"photo {pid} missing on disk: {src}")
                continue
            taken = row["taken_at"] or ""
            try:
                time_part = datetime.fromisoformat(taken).strftime("%H%M%S")
            except ValueError:
                time_part = "000000"
            stem = f"{day}_{time_part}_{next_nnn:03d}"
            dst_dir = curator_root / ("winner" if role == "winner" else "rejected")
            dst = dst_dir / (stem + src.suffix)

            moves.append(Move(
                src_path=src, dst_path=dst, role=role,
                photo_id=pid, companion_of=None,
                sha256_expected=row["sha256"],
            ))

            companion = _find_companion(src)
            if companion is not None:
                moves.append(Move(
                    src_path=companion,
                    dst_path=dst_dir / (stem + companion.suffix),
                    role=role, photo_id=None, companion_of=pid,
                    sha256_expected=None,
                ))

            next_nnn += 1

    seen_dst: set[Path] = set()
    for m in moves:
        if m.dst_path in seen_dst:
            errors.append(f"destination collision: {m.dst_path}")
        seen_dst.add(m.dst_path)

    total_size = 0
    for m in moves:
        try:
            total_size += m.src_path.stat().st_size
        except OSError:
            pass

    stats = {
        "winner": sum(1 for m in moves if m.role == "winner"),
        "rejected": sum(1 for m in moves if m.role == "rejected"),
        "companion": sum(1 for m in moves if m.companion_of is not None),
        "total_moves": len(moves),
        "total_size": total_size,
        "curator_root": str(curator_root),
        "source_root": str(source_root),
    }
    return CommitPlan(source_root, curator_root, moves, errors, stats)


def plan_to_dict(plan: CommitPlan, sample_limit: int = 20) -> dict:
    return {
        "stats": plan.stats,
        "errors": plan.errors,
        "has_errors": plan.has_errors,
        "sample": [
            {"src": str(m.src_path), "dst": str(m.dst_path),
             "role": m.role, "companion_of": m.companion_of}
            for m in plan.moves[:sample_limit]
        ],
    }


def execute_commit(plan: CommitPlan, conn, progress=None) -> dict:
    """Move every file in `plan`. All-or-nothing.

    Steps per move:
      1. verify source exists, destination doesn't
      2. hash source, compare to sha256_expected (when present)
      3. mkdir parent, shutil.move
      4. hash destination, compare to hash source
      5. append to manifest rows (buffered, not written yet)

    If any step fails: roll back every completed move (reverse order),
    delete no manifest rows (none were written), raise the original error.

    On success: write all manifest rows + update photos.committed_at,
    in a single SQLite transaction.

    Returns: {commit_id, moved, manifest_rows, stats}
    """
    commit_id = uuid.uuid4().hex[:12]
    now = datetime.now().isoformat(timespec="seconds")

    completed: list[Move] = []
    manifest_rows: list[tuple] = []
    total = len(plan.moves)

    try:
        for i, move in enumerate(plan.moves):
            if not move.src_path.exists():
                raise RuntimeError(f"source missing: {move.src_path}")
            if move.dst_path.exists():
                raise RuntimeError(f"destination exists: {move.dst_path}")

            sha_before = _sha256_file(move.src_path)
            if move.sha256_expected and sha_before != move.sha256_expected:
                raise RuntimeError(
                    f"hash mismatch before move: {move.src_path}\n"
                    f"  expected: {move.sha256_expected}\n"
                    f"  actual:   {sha_before}"
                )

            move.dst_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(move.src_path), str(move.dst_path))
            completed.append(move)

            sha_after = _sha256_file(move.dst_path)
            if sha_after != sha_before:
                raise RuntimeError(
                    f"hash mismatch after move: {move.dst_path}\n"
                    f"  before: {sha_before}\n"
                    f"  after:  {sha_after}"
                )

            try:
                size = move.dst_path.stat().st_size
            except OSError:
                size = None

            manifest_rows.append((
                commit_id, move.photo_id, move.companion_of,
                str(move.src_path), str(move.dst_path),
                sha_before, sha_after, move.role, size, now,
            ))

            if progress:
                progress(i + 1, total)

        # All moves succeeded: write manifest + committed_at atomically
        try:
            conn.execute("BEGIN")
            conn.executemany(
                "INSERT INTO manifest (commit_id, photo_id, companion_of, "
                "original_path, new_path, sha256_before, sha256_after, role, size, moved_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                manifest_rows,
            )
            photo_ids = [m.photo_id for m in plan.moves if m.photo_id is not None]
            if photo_ids:
                conn.executemany(
                    "UPDATE photos SET committed_at = ? WHERE id = ?",
                    [(now, pid) for pid in photo_ids],
                )
            conn.commit()
            try:
                from .learning import store_reject_centroids
                store_reject_centroids(conn, commit_id)
            except Exception:
                pass  # learning is best-effort; never block a successful commit
        except Exception:
            conn.rollback()
            raise

    except Exception:
        # Roll back filesystem in reverse order
        for move in reversed(completed):
            try:
                if move.dst_path.exists() and not move.src_path.exists():
                    move.src_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(move.dst_path), str(move.src_path))
            except Exception:
                pass  # best-effort; the error we're propagating is more important
        raise

    return {
        "commit_id": commit_id,
        "moved": len(completed),
        "manifest_rows": len(manifest_rows),
        "stats": plan.stats,
    }


def undo_commit(conn, commit_id: str) -> dict:
    """Restore every file of `commit_id` back to its original path.

    Reads the manifest, moves files back (reverse order), verifies each
    SHA-256 against sha256_before, clears photos.committed_at, stamps
    undone_at. Rows that cannot be restored (missing file, hash mismatch,
    or original path occupied) are reported in `missing`, but the rest
    is undone — no silent corruption.
    """
    rows = conn.execute(
        "SELECT * FROM manifest WHERE commit_id = ? AND undone_at IS NULL "
        "ORDER BY id DESC",
        (commit_id,),
    ).fetchall()
    if not rows:
        return {"commit_id": commit_id, "restored": 0, "missing": [],
                "note": "already undone or not found"}

    now = datetime.now().isoformat(timespec="seconds")
    restored = 0
    missing: list[str] = []

    for row in rows:
        new_path = Path(row["new_path"])
        orig_path = Path(row["original_path"])

        if not new_path.exists():
            missing.append(f"{new_path} (not found)")
            continue
        if orig_path.exists():
            missing.append(f"{orig_path} (original path occupied)")
            continue

        try:
            orig_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(new_path), str(orig_path))
        except OSError as exc:
            missing.append(f"{new_path}: {exc}")
            continue

        sha = _sha256_file(orig_path)
        if sha != row["sha256_before"]:
            # Put it back where it was so state is consistent
            try:
                shutil.move(str(orig_path), str(new_path))
            except OSError:
                pass
            missing.append(f"{new_path} (hash mismatch)")
            continue

        restored += 1

    try:
        conn.execute("BEGIN")
        conn.execute(
            "UPDATE manifest SET undone_at = ? WHERE commit_id = ? AND undone_at IS NULL",
            (now, commit_id),
        )
        photo_ids = [r["photo_id"] for r in rows if r["photo_id"] is not None]
        if photo_ids:
            conn.executemany(
                "UPDATE photos SET committed_at = NULL WHERE id = ?",
                [(pid,) for pid in photo_ids],
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return {"commit_id": commit_id, "restored": restored, "missing": missing}