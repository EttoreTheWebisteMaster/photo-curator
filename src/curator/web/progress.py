"""Mobile-window ETA for batch processing.

Why not a simple ratio (`first 100 files took 10s → 1000 files = 100s`)?
Because the first samples are always slower (model warm-up, cold cache, JIT),
so a fixed ratio overestimates and the progress bar stalls.
We use a 60-second moving window, and we hide the ETA until we have enough samples.
"""
from __future__ import annotations

import time
from collections import deque


class ProgressEstimator:
    """Mobile-window ETA for batch processing."""

    def __init__(self, total: int, window_seconds: float = 60.0, min_samples: int = 20):
        self.total = total
        self.window = window_seconds
        self.min_samples = min_samples
        self.samples = deque()  # (timestamp, processed_count)
        self.started = time.monotonic()

    def update(self, processed: int) -> dict:
        now = time.monotonic()
        self.samples.append((now, processed))
        cutoff = now - self.window
        while self.samples and self.samples[0][0] < cutoff:
            self.samples.popleft()

        elapsed_total = now - self.started
        if processed < self.min_samples or len(self.samples) < 2:
            return {
                "processed": processed,
                "total": self.total,
                "eta_seconds": None,
                "eta_label": "Calibrating…",
                "confidence": "warming",
            }

        first_t, first_n = self.samples[0]
        last_t, last_n = self.samples[-1]
        rate = (last_n - first_n) / max(last_t - first_t, 0.001)
        remaining = max(self.total - processed, 0)
        eta = remaining / rate if rate > 0 else None

        if eta is None:
            label = "Estimating…"
        else:
            low = max(int(eta * 0.8 // 10 * 10), 10)
            high = max(int(eta * 1.2 // 10 * 10), low + 10)
            if high >= 60:
                label = f"{low // 60}–{high // 60 + 1} min remaining"
            else:
                label = f"{low}–{high} s remaining"

        return {
            "processed": processed,
            "total": self.total,
            "rate": round(rate, 2),
            "eta_seconds": int(eta) if eta else None,
            "eta_label": label,
            "confidence": "stable" if elapsed_total > 15 else "warming",
        }