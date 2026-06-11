from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass
class Detection:
    label: str
    confidence: float
    bbox: tuple[int, int, int, int] | None = None
    distance_m: float | None = None


class PerceptionPipeline:
    """Lightweight perception facade.

    It keeps the interface stable for future OpenCV/LLM model integration while
    remaining executable in environments without vision dependencies.
    """

    def __init__(self) -> None:
        self._last_seen_at: dict[str, datetime] = {}

    def detect_stall_and_item(
        self,
        head_frame: bytes | None,
        stall_label: str | None,
        item_name: str | None,
    ) -> dict[str, Any]:
        # Placeholder behavior: if frame exists we return a weak positive result.
        has_frame = bool(head_frame)
        stall = Detection(
            label=stall_label or "unknown_stall",
            confidence=0.75 if has_frame else 0.0,
            bbox=(400, 120, 820, 620) if has_frame else None,
            distance_m=1.2 if has_frame else None,
        )
        item = Detection(
            label=item_name or "unknown_item",
            confidence=0.68 if has_frame else 0.0,
            bbox=(520, 280, 700, 560) if has_frame else None,
            distance_m=0.48 if has_frame else None,
        )

        if has_frame:
            self._last_seen_at[stall.label] = datetime.now()
            self._last_seen_at[item.label] = datetime.now()

        return {
            "stall": stall,
            "item": item,
            "has_target": has_frame,
        }

    def choose_arm_by_wrist_view(
        self,
        left_frame: bytes | None,
        right_frame: bytes | None,
    ) -> dict[str, Any]:
        # Deterministic proxy: prefer right when both exist, else whichever exists.
        left_score = len(left_frame or b"")
        right_score = len(right_frame or b"")
        if right_score >= left_score and right_score > 0:
            return {"arm": "right", "score": right_score}
        if left_score > 0:
            return {"arm": "left", "score": left_score}
        return {"arm": "right", "score": 0}

    def detect_human_and_hand(self, head_frame: bytes | None) -> dict[str, Any]:
        has_frame = bool(head_frame)
        return {
            "human_detected": has_frame,
            "hand_detected": has_frame,
            "human_distance_m": 0.9 if has_frame else None,
            "hand_bbox": (500, 260, 700, 620) if has_frame else None,
        }
