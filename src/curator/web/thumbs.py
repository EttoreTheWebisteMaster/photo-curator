"""On-disk thumbnail cache. Generates 800px JPEGs from HEIC/JPEG/PNG originals.

Cache key: sha256(original_path + mtime + size)[:16] + '.jpg'. A change to the
original (new mtime/size) produces a new cache file, so stale thumbs are never served.
Read-only on the original.
"""
from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

from PIL import Image, ImageOps

try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except ImportError:
    pass

THUMB_MAX = 800
_JPEG_QUALITY = 82


def cache_dir() -> Path:
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Caches" / "curator" / "thumbs"
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "curator" / "thumbs"
    base.mkdir(parents=True, exist_ok=True)
    return base


def thumb_for(original_path: str, mtime: float, size: int) -> Path:
    key = hashlib.sha256(f"{original_path}:{mtime}:{size}".encode()).hexdigest()[:16]
    out = cache_dir() / f"{key}.jpg"
    if out.exists():
        return out

    with Image.open(original_path) as img:
        img = ImageOps.exif_transpose(img)
        img = img.convert("RGB")
        img.thumbnail((THUMB_MAX, THUMB_MAX), Image.LANCZOS)
        img.save(out, "JPEG", quality=_JPEG_QUALITY, optimize=True)
    return out