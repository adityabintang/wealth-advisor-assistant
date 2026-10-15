"""Confidence score = 1 - sum(penalties), clamped to [floor, 1]. Every penalty is itemised in the output."""

from __future__ import annotations

from typing import Iterable

from .models import ConfidencePenalty


def compute_confidence(penalties: Iterable[ConfidencePenalty], floor: float = 0.05) -> float:
    total = sum(p.amount for p in penalties)
    return round(max(floor, min(1.0, 1.0 - total)), 3)
