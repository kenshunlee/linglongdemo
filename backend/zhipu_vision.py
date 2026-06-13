from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from typing import Any

import httpx


@dataclass
class VisionResponse:
    raw_text: str
    parsed: dict[str, Any] | None
    raw_response: dict[str, Any]


class ZhipuVisionClient:
    def __init__(self, *, api_key: str, base_url: str, model: str, timeout_s: float = 30.0) -> None:
        self.api_key = api_key.strip()
        self.base_url = base_url.rstrip("/")
        self.model = model.strip()
        self.timeout_s = max(5.0, float(timeout_s))

    def enabled(self) -> bool:
        return bool(self.api_key and self.base_url and self.model)

    def analyze_image(
        self,
        *,
        prompt: str,
        image_bytes: bytes,
        image_mime: str = "image/jpeg",
        max_tokens: int = 800,
        temperature: float = 0.1,
    ) -> VisionResponse:
        if not self.enabled():
            raise RuntimeError("ZHIPU_VISION_NOT_CONFIGURED")
        if not image_bytes:
            raise ValueError("image_bytes 不能为空")

        data_uri = self._image_data_uri(image_bytes, image_mime)
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": data_uri}},
                    ],
                }
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        url = f"{self.base_url}/chat/completions"
        timeout = httpx.Timeout(self.timeout_s, connect=min(5.0, self.timeout_s))
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            resp = client.post(url, headers=headers, json=payload)
        if resp.status_code >= 400:
            raise RuntimeError(f"ZHIPU_VISION_HTTP_{resp.status_code}: {resp.text}")

        data = resp.json()
        raw_text = self._extract_message_text(data)
        parsed = self._extract_json(raw_text)
        return VisionResponse(raw_text=raw_text, parsed=parsed, raw_response=data)

    @staticmethod
    def _image_data_uri(image_bytes: bytes, image_mime: str) -> str:
        mime = (image_mime or "image/jpeg").strip() or "image/jpeg"
        encoded = base64.b64encode(image_bytes).decode("ascii")
        return f"data:{mime};base64,{encoded}"

    @staticmethod
    def _extract_message_text(payload: dict[str, Any]) -> str:
        choices = payload.get("choices") or []
        if not choices:
            return ""
        message = (choices[0] or {}).get("message") or {}
        content = message.get("content")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            pieces: list[str] = []
            for part in content:
                if isinstance(part, dict):
                    if isinstance(part.get("text"), str):
                        pieces.append(part["text"])
                    elif isinstance(part.get("content"), str):
                        pieces.append(part["content"])
            return "\n".join(piece for piece in pieces if piece).strip()
        return str(content or "").strip()

    @staticmethod
    def _extract_json(text: str) -> dict[str, Any] | None:
        if not text:
            return None

        candidates = [text.strip()]
        fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL | re.IGNORECASE)
        if fence:
            candidates.insert(0, fence.group(1).strip())

        for candidate in candidates:
            try:
                data = json.loads(candidate)
                if isinstance(data, dict):
                    return data
            except Exception:
                continue
        return None