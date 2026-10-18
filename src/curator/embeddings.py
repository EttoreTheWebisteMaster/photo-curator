"""Visual embeddings (DINOv2, CLIP) for near-duplicate grouping and (later) ranking.

Design:
  - Optional deps: `pip install -e '.[embeddings]'` pulls in torch + transformers.
  - Lazy import: importing this module is cheap; torch loads only on first use.
  - MPS: on Apple Silicon, uses Metal when available, falls back to CPU.
  - Storage: float32 blobs in SQLite, L2-normalized, so cosine similarity = dot product.
  - Read-only on photos.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps


class Embedder(ABC):
    name: str = "base"

    @property
    @abstractmethod
    def dim(self) -> int: ...

    @abstractmethod
    def embed_batch(self, images: list[Image.Image]) -> np.ndarray:
        """Return (N, dim) float32 with L2-normalized rows."""


class DinoV2Embedder(Embedder):
    """DINOv2 ViT-S/14 (384-dim CLS token). ~85 MB on disk, ~1 GB peak RAM."""

    name = "dinov2_vits14"

    def __init__(self, device: str = "auto"):
        import torch
        from transformers import AutoImageProcessor, AutoModel

        self._torch = torch
        self.device = _pick_device(torch, device)
        self.model = AutoModel.from_pretrained("facebook/dinov2-small").to(self.device).eval()
        self.processor = AutoImageProcessor.from_pretrained("facebook/dinov2-small")
        self._dim = self.model.config.hidden_size

    @property
    def dim(self) -> int:
        return self._dim

    def embed_batch(self, images: list[Image.Image]) -> np.ndarray:
        imgs = [ImageOps.exif_transpose(im).convert("RGB") for im in images]
        with self._torch.no_grad():
            inputs = self.processor(images=imgs, return_tensors="pt").to(self.device)
            out = self.model(**inputs)
            feat = out.last_hidden_state[:, 0, :]          # CLS token
            feat = feat / feat.norm(dim=-1, keepdim=True)
        return feat.cpu().numpy().astype(np.float32)


class CLIPEmbedder(Embedder):
    """CLIP ViT-B/32 image encoder (512-dim). ~600 MB on disk."""

    name = "clip_vit_b32"

    def __init__(self, device: str = "auto"):
        import torch
        from transformers import CLIPImageProcessor, CLIPVisionModelWithProjection

        self._torch = torch
        self.device = _pick_device(torch, device)
        self.model = (
            CLIPVisionModelWithProjection.from_pretrained("openai/clip-vit-base-patch32")
            .to(self.device).eval()
        )
        self.processor = CLIPImageProcessor.from_pretrained("openai/clip-vit-base-patch32")
        self._dim = self.model.config.projection_dim

    @property
    def dim(self) -> int:
        return self._dim

    def embed_batch(self, images: list[Image.Image]) -> np.ndarray:
        imgs = [ImageOps.exif_transpose(im).convert("RGB") for im in images]
        with self._torch.no_grad():
            inputs = self.processor(images=imgs, return_tensors="pt").to(self.device)
            out = self.model(**inputs)
            feat = out.image_embeds
            feat = feat / feat.norm(dim=-1, keepdim=True)
        return feat.cpu().numpy().astype(np.float32)


def _pick_device(torch, device: str) -> str:
    if device != "auto":
        return device
    if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
        return "mps"
    return "cpu"


EMBEDDERS = {cls.name: cls for cls in (DinoV2Embedder, CLIPEmbedder)}


def get_embedder(name: str, device: str = "auto") -> Embedder:
    if name not in EMBEDDERS:
        raise ValueError(f"Unknown embedder {name!r}. Available: {sorted(EMBEDDERS)}")
    return EMBEDDERS[name](device=device)


def save_embedding(conn, photo_id: int, model: str, vec: np.ndarray) -> None:
    vec = np.ascontiguousarray(vec, dtype=np.float32)
    conn.execute(
        "INSERT OR REPLACE INTO embeddings (photo_id, model, dim, vector, created_at) "
        "VALUES (?,?,?,?,?)",
        (photo_id, model, len(vec), vec.tobytes(),
         datetime.now().isoformat(timespec="seconds")),
    )


def load_embedding(conn, photo_id: int, model: str) -> np.ndarray | None:
    row = conn.execute(
        "SELECT dim, vector FROM embeddings WHERE photo_id=? AND model=?",
        (photo_id, model),
    ).fetchone()
    if row is None:
        return None
    return np.frombuffer(row["vector"], dtype=np.float32, count=row["dim"]).copy()


def embed_folder(conn, model: str, batch_size: int = 8, device: str = "auto",
                 embedder: Embedder | None = None, progress=None) -> dict:
    """Compute embeddings for photos that don't have one for this model yet.

    `embedder` can be injected in tests to avoid loading torch.
    """
    rows = conn.execute(
        "SELECT p.id, p.path FROM photos p "
        "LEFT JOIN embeddings e ON e.photo_id = p.id AND e.model = ? "
        "WHERE p.error IS NULL AND e.photo_id IS NULL "
        "ORDER BY p.id",
        (model,),
    ).fetchall()
    stats = {"candidates": len(rows), "embedded": 0, "errors": 0}
    if not rows:
        return stats

    if embedder is None:
        embedder = get_embedder(model, device=device)

    for start in range(0, len(rows), batch_size):
        batch = rows[start : start + batch_size]
        imgs, ids = [], []
        for r in batch:
            try:
                with Image.open(r["path"]) as im:
                    im.load()
                    imgs.append(im.copy())
                ids.append(r["id"])
            except Exception:
                stats["errors"] += 1
        if not imgs:
            continue
        try:
            vecs = embedder.embed_batch(imgs)
        except Exception:
            stats["errors"] += len(imgs)
            continue
        for pid, vec in zip(ids, vecs):
            save_embedding(conn, pid, model, vec)
        conn.commit()
        stats["embedded"] += len(ids)
        if progress:
            progress(stats)
    return stats