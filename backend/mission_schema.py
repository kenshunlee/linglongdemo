from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
import re
from typing import Any


class MissionState(str, Enum):
    IDLE = "IDLE"
    PARSE = "PARSE"
    NAV_ALONG_ROUTE = "NAV_ALONG_ROUTE"
    STALL_SEARCH = "STALL_SEARCH"
    STALL_ALIGN = "STALL_ALIGN"
    ITEM_APPROACH = "ITEM_APPROACH"
    GRASP = "GRASP"
    RETREAT = "RETREAT"
    RETURN_ORIENT = "RETURN_ORIENT"
    HUMAN_SEARCH = "HUMAN_SEARCH"
    HAND_APPROACH = "HAND_APPROACH"
    RELEASE = "RELEASE"
    RESET = "RESET"
    DONE = "DONE"
    FAILSAFE = "FAILSAFE"


@dataclass
class MissionTarget:
    stall_id: int | None = None
    stall_label: str | None = None
    item_name: str | None = None
    round_index: int = 1


@dataclass
class MissionRuntime:
    mission_id: str
    command_text: str
    target: MissionTarget
    state: MissionState = MissionState.IDLE
    started_at: datetime = field(default_factory=datetime.now)
    finished_at: datetime | None = None
    error: str = ""
    logs: list[dict[str, Any]] = field(default_factory=list)


def parse_market_command(command_text: str) -> MissionTarget:
    text = (command_text or "").strip()
    target = MissionTarget(item_name=None)

    zh_digit_map = {
        "一": 1,
        "二": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
    }

    m = re.search(r"(\d+)\s*号", text)
    if m:
        target.stall_id = int(m.group(1))
    else:
        for k, v in zh_digit_map.items():
            if f"{k}号" in text:
                target.stall_id = v
                break

    item_patterns = [
        r"拿([\u4e00-\u9fa5A-Za-z0-9_-]{1,12})",
        r"取([\u4e00-\u9fa5A-Za-z0-9_-]{1,12})",
        r"购买([\u4e00-\u9fa5A-Za-z0-9_-]{1,12})",
    ]
    for p in item_patterns:
        mm = re.search(p, text)
        if mm:
            target.item_name = mm.group(1)
            break

    if target.stall_id is not None:
        target.stall_label = f"{target.stall_id}号摊位"

    return target
