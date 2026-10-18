"""Read-only scan: capture date, file hash, perceptual hash, sharpness.

Safety rules:
- files are only ever opened for reading;
- Apple Photos libraries (.photoslibrary) are refused: work on an export instead.

Capture date, in order of trust:
  1. EXIF DateTimeOriginal / DateTimeDigitized      (time_known = 1)
  2. EXIF DateTime                                  (time_known = 1)
  3. date + time written in the file name           (time_known = 1)
  4. date in the file name (e.g. WhatsApp IMG-20210909-WA0026.jpg):
       - if the file's modification date is the same day, take the time of day from it
         (approximate: it is when the file arrived)                 (time_known = 1)
       - otherwise only the date is kept, time set to 00:00:00      (time_known = 0)
  5. file modification time                                       (time_known = 0)
"""
from __future__ import annotations

import hashlib
import os
import re
from datetime import date, datetime, time
from pathlib import Path

import imagehash
import numpy as np
from PIL import Image, ImageOps

try:  # HEIC/HEIF (iPhone photos)
    import pillow_heif

    pillow_heif.register_heif_opener()
except ImportError:  # pragma: no cover
    pass

EXTENSIONS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".tif", ".tiff", ".webp"}
EXIF_IFD = 0x8769
TAG_DATETIME_ORIGINAL = 36867
TAG_DATETIME_DIGITIZED = 36868
TAG_DATETIME = 306
ANALYSIS_MAX_SIDE = 1024
MIN_YEAR = 1990

# Matches e.g. IMG-20210909-WA0026, IMG_20210909_143012, PXL_20210909_143012345,
# "Screenshot 2021-09-09 at 14.30.12", "WhatsApp Image 2021-09-09 at 14.30.12".
FILENAME_DATETIME = re.compile(
    r"""
    (?<!\d)(?P<y>(?:19|20)\d{2})[-_.]?(?P<m>0[1-9]|1[0-2])[-_.]?(?P<d>0[1-9]|[12]\d|3[01])(?!\d)
    (?:(?:[\sT_-]|\bat\b)+(?P<H>[01]\d|2[0-3])[.:_-]?(?P<M>[0-5]\d)[.:_-]?(?P<S>[0-5]\d))?
    """,
    re.VERBOSE,
)


class LibraryRejected(Exception):
    pass


def check_root(root: Path) -> None:
    for part in [root, *root.parents]:
        if part.name.endswith(".photoslibrary"):
            raise LibraryRejected(
                f"{part} is an Apple Photos library. It must not be read or modified directly: "
                "export a copy first (e.g. with osxphotos) and scan that instead."
            )


def iter_photos(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            d for d in dirnames if not d.startswith(".") and not d.endswith(".photoslibrary")
        )
        for name in sorted(filenames):
            if name.startswith("."):
                continue
            if Path(name).suffix.lower() in EXTENSIONS:
                yield Path(dirpath) / name


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while block := f.read(chunk):
            h.update(block)
    return h.hexdigest()


def _parse_exif_date(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, bytes):
        value = value.decode("ascii", "ignore")
    value = value.strip().strip("\x00")
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(value, fmt)
        except ValueError:
            continue
        if dt.year < MIN_YEAR:  # placeholder dates such as 0000:00:00
            return None
        return dt
    return None


def parse_filename_datetime(filename: str) -> tuple[date, time | None] | None:
    """Extract (date, time-or-None) from a file name, or None if it has no plausible date."""
    m = FILENAME_DATETIME.search(Path(filename).stem)
    if not m:
        return None
    try:
        d = date(int(m["y"]), int(m["m"]), int(m["d"]))
    except ValueError:  # e.g. 2021-02-30
        return None
    if d.year < MIN_YEAR or d > date.today():
        return None
    t = time(int(m["H"]), int(m["M"]), int(m["S"])) if m["H"] else None
    return d, t


def read_taken_at(img: Image.Image, mtime: float, filename: str = "") -> tuple[str, str, bool]:
    """Return (ISO date, source, time_known). See the module docstring for the order of trust."""
    try:
        exif = img.getexif()
        ifd = exif.get_ifd(EXIF_IFD) if exif else {}
    except Exception:
        exif, ifd = {}, {}
    dt = _parse_exif_date(ifd.get(TAG_DATETIME_ORIGINAL) or ifd.get(TAG_DATETIME_DIGITIZED))
    if dt:
        return dt.isoformat(timespec="seconds"), "exif_original", True
    dt = _parse_exif_date(exif.get(TAG_DATETIME) if exif else None)
    if dt:
        return dt.isoformat(timespec="seconds"), "exif_datetime", True

    parsed = parse_filename_datetime(filename) if filename else None
    modified = datetime.fromtimestamp(mtime)
    if parsed:
        d, t = parsed
        if t:
            return datetime.combine(d, t).isoformat(timespec="seconds"), "filename_datetime", True
        if modified.date() == d:
            return modified.isoformat(timespec="seconds"), "filename_date_mtime", True
        return datetime.combine(d, time(0, 0)).isoformat(timespec="seconds"), "filename_date", False
    return modified.isoformat(timespec="seconds"), "file_mtime", False


def laplacian_variance(gray: np.ndarray) -> float:
    """Variance of the 4-neighbour Laplacian: higher = sharper (relative value)."""
    g = gray.astype(np.float32)
    lap = -4 * g[1:-1, 1:-1] + g[:-2, 1:-1] + g[2:, 1:-1] + g[1:-1, :-2] + g[1:-1, 2:]
    return float(lap.var())


def analyse(path: Path, mtime: float) -> dict:
    with Image.open(path) as img:
        taken_at, source, time_known = read_taken_at(img, mtime, path.name)
        img = ImageOps.exif_transpose(img)  # honour the EXIF orientation
        width, height = img.size
        img = img.convert("RGB")
        img.thumbnail((ANALYSIS_MAX_SIDE, ANALYSIS_MAX_SIDE))
        gray = np.asarray(img.convert("L"))
        return {
            "width": width,
            "height": height,
            "taken_at": taken_at,
            "taken_source": source,
            "time_known": int(time_known),
            "phash": str(imagehash.phash(img)),
            "sharpness": laplacian_variance(gray),
        }


def scan(root: Path, conn, progress=None, force: bool = False, stop_event=None) -> dict:
    """Index `root`. Incremental: unchanged files (same size + mtime) are skipped.

    Rows written by an older version (time_known missing) are re-scanned automatically;
    `force=True` re-scans everything.
    """
    root = root.resolve()
    check_root(root)
    stats = {"seen": 0, "new": 0, "updated": 0, "skipped": 0, "errors": 0}
    now = datetime.now().isoformat(timespec="seconds")

    for path in iter_photos(root):
        if stop_event is not None and stop_event.is_set():
            break
        stats["seen"] += 1
        st = path.stat()
        row = conn.execute(
            "SELECT id, size, mtime, error, time_known FROM photos WHERE path = ?", (str(path),)
        ).fetchone()
        unchanged = (
            row is not None
            and row["size"] == st.st_size
            and row["mtime"] == st.st_mtime
            and not row["error"]
            and row["time_known"] is not None
        )
        if unchanged and not force:
            stats["skipped"] += 1
            continue
        try:
            data = analyse(path, st.st_mtime)
            data["sha256"] = sha256_file(path)
            data["error"] = None
        except Exception as exc:  # corrupt file or unreadable format
            data = {"sha256": None, "width": None, "height": None, "taken_at": None,
                    "taken_source": None, "time_known": None, "phash": None, "sharpness": None,
                    "error": f"{type(exc).__name__}: {exc}"}
            stats["errors"] += 1
        data.update(path=str(path), size=st.st_size, mtime=st.st_mtime, scanned_at=now)
        cols = list(data)
        conn.execute(
            f"INSERT INTO photos ({','.join(cols)}) VALUES ({','.join('?' * len(cols))}) "
            f"ON CONFLICT(path) DO UPDATE SET {','.join(f'{c}=excluded.{c}' for c in cols if c != 'path')}",
            [data[c] for c in cols],
        )
        stats["updated" if row else "new"] += 1
        if stats["seen"] % 50 == 0:
            conn.commit()
        if progress:
            progress(stats)
    conn.commit()
    return stats
