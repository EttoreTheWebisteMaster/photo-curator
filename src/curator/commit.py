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

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

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