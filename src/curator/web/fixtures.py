"""Fake groups for Sprint 1. Replaced by real DB reads in Sprint 2.

Ten groups: 5 pending, 3 completed, 1 deleted, 1 more pending — enough to feel
the flow. Photos are real images from picsum.photos so the layout looks right.
"""
from __future__ import annotations

import random


def get_fake_groups() -> list[dict]:
    rng = random.Random(42)

    def make_photo(pid: str, status: str, sharpness: float, exposure: float) -> dict:
        return {
            "id": pid,
            "url": f"https://picsum.photos/seed/{pid}/1600/1200",
            "thumb": f"https://picsum.photos/seed/{pid}/400/300",
            "sharpness": sharpness,
            "exposure": exposure,
            "status": status,
        }

    definitions = [
        ("2026-09-15T14:32:00", "pending",   ["g1a", "g1b", "g1c", "g1d"]),
        ("2026-09-15T14:35:00", "pending",   ["g2a", "g2b", "g2c"]),
        ("2026-09-15T18:20:00", "completed", ["g3a", "g3b"]),
        ("2026-09-16T09:10:00", "pending",   ["g4a", "g4b", "g4c", "g4d", "g4e"]),
        ("2026-09-16T09:12:00", "deleted",   ["g5a", "g5b", "g5c"]),
        ("2026-09-17T15:45:00", "pending",   ["g6a", "g6b"]),
        ("2026-09-18T11:00:00", "completed", ["g7a", "g7b", "g7c"]),
        ("2026-09-19T07:30:00", "pending",   ["g8a", "g8b", "g8c", "g8d"]),
        ("2026-09-20T20:15:00", "pending",   ["g9a", "g9b", "g9c"]),
        ("2026-09-21T16:00:00", "completed", ["g10a", "g10b"]),
    ]

    groups = []
    for i, (date, status, seeds) in enumerate(definitions, start=1):
        photos = []
        for j, seed in enumerate(seeds):
            sharpness = 200 - j * 40 + rng.uniform(-20, 20)
            exposure = 0.85 - j * 0.05 + rng.uniform(-0.05, 0.05)
            photos.append(make_photo(
                seed,
                "winner" if j == 0 else "rejected",
                sharpness,
                exposure,
            ))
        groups.append({
            "id": i,
            "taken_at": date,
            "status": status,
            "current_winner": seeds[0],
            "photos": photos,
        })
    return groups