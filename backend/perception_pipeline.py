from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from zhipu_vision import ZhipuVisionClient


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
        self._vision_client: ZhipuVisionClient | None = None
        self._vision_model = os.getenv("ZHIPU_VISION_MODEL", "glm-4.6v-flash")
        self._vision_timeout_s = float(os.getenv("ZHIPU_VISION_TIMEOUT_S", "25"))
        api_key = os.getenv("ZHIPU_API_KEY", "")
        base_url = os.getenv("ZHIPU_BASE_URL", "https://open.bigmodel.cn/api/paas/v4")
        if api_key.strip() and self._vision_model.strip():
            self._vision_client = ZhipuVisionClient(
                api_key=api_key,
                base_url=base_url,
                model=self._vision_model,
                timeout_s=self._vision_timeout_s,
            )

    @staticmethod
    def _stall_hint_text(stall_label: str | None, item_name: str | None) -> str:
        booth_descriptions = {
            1: "水果摊：梨、苹果、桔子",
            2: "毛线玩具摊：毛线/毛绒玩具",
            3: "鲜花摊：4束颜色不同的鲜花",
            4: "纸质小礼品盒摊：小礼品盒",
            5: "纸质大礼品盒摊：大礼品盒",
            6: "饮料摊：橙汁饮料与红茶饮料",
        }
        stall_text = stall_label or "未知摊位"
        item_text = item_name or "未知物品"
        return f"目标摊位：{stall_text}。目标物品：{item_text}。可参考摊位类型：{booth_descriptions}."

    @staticmethod
    def _normalize_bbox(value: Any) -> tuple[int, int, int, int] | None:
        if not isinstance(value, (list, tuple)) or len(value) != 4:
            return None
        try:
            x1, y1, x2, y2 = (int(round(float(v))) for v in value)
        except Exception:
            return None
        return x1, y1, x2, y2

    @staticmethod
    def _normalize_offset(value: Any) -> dict[str, int] | None:
        if not isinstance(value, dict):
            return None
        try:
            return {
                "x": int(round(float(value.get("x", 0)))),
                "y": int(round(float(value.get("y", 0)))),
            }
        except Exception:
            return None

    def _vision_enabled(self) -> bool:
        return self._vision_client is not None and self._vision_client.enabled()

    def _vision_fallback_stall(self, stall_label: str | None, item_name: str | None, has_frame: bool) -> dict[str, Any]:
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
        return {"stall": stall, "item": item, "has_target": has_frame, "source": "fallback"}

    def _vision_analyze(self, frame: bytes, prompt: str) -> dict[str, Any] | None:
        if not self._vision_enabled() or not frame:
            return None
        try:
            assert self._vision_client is not None
            resp = self._vision_client.analyze_image(prompt=prompt, image_bytes=frame)
            return resp.parsed or {"raw_text": resp.raw_text}
        except Exception:
            return None

    def detect_stall_and_item(
        self,
        head_frame: bytes | None,
        stall_label: str | None,
        item_name: str | None,
    ) -> dict[str, Any]:
        has_frame = bool(head_frame)
        if not has_frame:
            return self._vision_fallback_stall(stall_label, item_name, False)

        prompt = (
            "你是市场摊位视觉识别助手。请根据图像判断是否看到了目标摊位和目标物品。\n"
            f"{self._stall_hint_text(stall_label, item_name)}\n"
            "请只输出 JSON，不要输出多余解释。JSON 格式：\n"
            "{\n"
            '  "has_target": true,\n'
            '  "stall_match": true,\n'
            '  "stall_label_seen": "1号摊位",\n'
            '  "item_seen": true,\n'
            '  "item_name_seen": "苹果",\n'
            '  "item_category": "水果",\n'
            '  "confidence": 0.0,\n'
            '  "bbox": [x1, y1, x2, y2],\n'
            '  "center_offset_px": {"x": 0, "y": 0},\n'
            '  "recommended_turn_deg": 0.0,\n'
            '  "recommended_move_m": 0.0,\n'
            '  "distance_m": 0.0,\n'
            '  "notes": ""\n'
            "}"
        )
        result = self._vision_analyze(head_frame, prompt)
        if not result:
            return self._vision_fallback_stall(stall_label, item_name, True)

        stall = Detection(
            label=str(result.get("stall_label_seen") or stall_label or "unknown_stall"),
            confidence=float(result.get("confidence") or 0.0),
            bbox=self._normalize_bbox(result.get("bbox")),
            distance_m=float(result.get("distance_m")) if result.get("distance_m") is not None else None,
        )
        item = Detection(
            label=str(result.get("item_name_seen") or item_name or "unknown_item"),
            confidence=float(result.get("confidence") or 0.0),
            bbox=self._normalize_bbox(result.get("bbox")),
            distance_m=float(result.get("distance_m")) if result.get("distance_m") is not None else None,
        )

        if bool(result.get("has_target", False)):
            self._last_seen_at[stall.label] = datetime.now()
            self._last_seen_at[item.label] = datetime.now()

        return {
            "stall": stall,
            "item": item,
            "has_target": bool(result.get("has_target", False)),
            "stall_match": bool(result.get("stall_match", False)),
            "item_seen": bool(result.get("item_seen", False)),
            "confidence": float(result.get("confidence") or 0.0),
            "bbox": self._normalize_bbox(result.get("bbox")),
            "center_offset_px": self._normalize_offset(result.get("center_offset_px")),
            "recommended_turn_deg": float(result.get("recommended_turn_deg") or 0.0),
            "recommended_move_m": float(result.get("recommended_move_m") or 0.0),
            "distance_m": float(result.get("distance_m")) if result.get("distance_m") is not None else None,
            "notes": str(result.get("notes") or ""),
            "source": "zhipu" if self._vision_enabled() else "fallback",
            "raw": result,
        }

    def choose_arm_by_wrist_view(
        self,
        left_frame: bytes | None,
        right_frame: bytes | None,
        target_item: str | None = None,
    ) -> dict[str, Any]:
        if not self._vision_enabled():
            left_score = len(left_frame or b"")
            right_score = len(right_frame or b"")
            if right_score >= left_score and right_score > 0:
                return {"arm": "right", "score": right_score, "source": "fallback"}
            if left_score > 0:
                return {"arm": "left", "score": left_score, "source": "fallback"}
            return {"arm": "right", "score": 0, "source": "fallback"}

        def _score(frame: bytes | None, arm: str) -> dict[str, Any]:
            if not frame:
                return {"arm": arm, "score": 0.0, "visible": False}
            prompt = (
                "你是机械臂腕部相机抓取评估助手。请判断目标物品在当前腕部视角中的可见性和抓取可行性。\n"
                f"目标物品：{target_item or '未知物品'}。请只输出 JSON。\n"
                "JSON 格式：\n"
                '{"visible": true, "score": 0.0, "bbox": [x1, y1, x2, y2], "notes": ""}'
            )
            result = self._vision_analyze(frame, prompt)
            if not result:
                return {"arm": arm, "score": 0.0, "visible": False}
            return {
                "arm": arm,
                "score": float(result.get("score") or (1.0 if result.get("visible") else 0.0)),
                "visible": bool(result.get("visible", False)),
                "bbox": self._normalize_bbox(result.get("bbox")),
                "notes": str(result.get("notes") or ""),
                "source": "zhipu",
            }

        left_result = _score(left_frame, "left")
        right_result = _score(right_frame, "right")
        if float(right_result.get("score", 0.0)) >= float(left_result.get("score", 0.0)):
            return right_result
        return left_result

    def detect_human_and_hand(self, head_frame: bytes | None) -> dict[str, Any]:
        has_frame = bool(head_frame)
        if not has_frame:
            return {
                "human_detected": False,
                "hand_detected": False,
                "human_distance_m": None,
                "hand_bbox": None,
                "source": "fallback",
            }

        prompt = (
            "你是人手交接视觉助手。请判断图像里是否存在人的手，以及适合放物的接收区域。\n"
            "请只输出 JSON，不要解释。\n"
            "JSON 格式：\n"
            '{"human_detected": true, "hand_detected": true, "human_distance_m": 0.0, "hand_bbox": [x1, y1, x2, y2], "notes": ""}'
        )
        result = self._vision_analyze(head_frame, prompt)
        if not result:
            return {
                "human_detected": True,
                "hand_detected": True,
                "human_distance_m": 0.9,
                "hand_bbox": (500, 260, 700, 620),
                "source": "fallback",
            }

        return {
            "human_detected": bool(result.get("human_detected", False)),
            "hand_detected": bool(result.get("hand_detected", False)),
            "human_distance_m": float(result.get("human_distance_m")) if result.get("human_distance_m") is not None else None,
            "hand_bbox": self._normalize_bbox(result.get("hand_bbox")),
            "notes": str(result.get("notes") or ""),
            "source": "zhipu",
            "raw": result,
        }
