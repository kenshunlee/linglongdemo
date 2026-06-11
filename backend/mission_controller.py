from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
import threading
import time
import uuid
from typing import Any, Callable

from mission_schema import MissionRuntime, MissionState, parse_market_command
from perception_pipeline import PerceptionPipeline


class MissionController:
    def __init__(
        self,
        robot_service: Any,
        dry_run: bool = True,
        event_callback: Callable[[str, dict[str, Any], dict[str, Any]], None] | None = None,
    ) -> None:
        self._robot = robot_service
        self._dry_run = dry_run
        self._perception = PerceptionPipeline()
        self._event_callback = event_callback

        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._worker: threading.Thread | None = None
        self._current: MissionRuntime | None = None
        self._history: list[MissionRuntime] = []

    def start(self, command_text: str, options: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._lock:
            if self._worker and self._worker.is_alive():
                raise RuntimeError("mission 已在执行中")

            mission_id = str(uuid.uuid4())
            target = parse_market_command(command_text)
            rt = MissionRuntime(mission_id=mission_id, command_text=command_text, target=target)
            self._current = rt
            self._stop_event.clear()
            self._worker = threading.Thread(target=self._run, args=(rt, options or {}), daemon=True)
            self._worker.start()
            return self._snapshot(rt)

    def stop(self) -> dict[str, Any]:
        self._stop_event.set()
        with self._lock:
            if self._current:
                self._log(self._current, "stop_requested", {})
                self._current.state = MissionState.FAILSAFE
            return self.status()

    def status(self) -> dict[str, Any]:
        with self._lock:
            if not self._current:
                return {
                    "running": False,
                    "state": MissionState.IDLE.value,
                    "current": None,
                    "history_count": len(self._history),
                }
            running = bool(self._worker and self._worker.is_alive())
            snap = self._snapshot(self._current)
            snap["running"] = running
            snap["history_count"] = len(self._history)
            return snap

    def history(self, limit: int = 10) -> dict[str, Any]:
        with self._lock:
            items = [self._snapshot(x) for x in self._history[-max(1, limit) :]]
            return {"count": len(items), "items": items}

    def _snapshot(self, rt: MissionRuntime) -> dict[str, Any]:
        return {
            "mission_id": rt.mission_id,
            "command_text": rt.command_text,
            "target": asdict(rt.target),
            "state": rt.state.value,
            "started_at": rt.started_at.isoformat(),
            "finished_at": rt.finished_at.isoformat() if rt.finished_at else None,
            "error": rt.error,
            "logs": rt.logs[-30:],
        }

    def _log(self, rt: MissionRuntime, event: str, payload: dict[str, Any]) -> None:
        rt.logs.append({"ts": datetime.now().isoformat(timespec="seconds"), "event": event, "payload": payload})
        if self._event_callback is not None:
            try:
                self._event_callback(event, payload, self._snapshot(rt))
            except Exception:
                # Event sink should never break mission execution.
                pass

    def _set_state(self, rt: MissionRuntime, state: MissionState, info: dict[str, Any] | None = None) -> None:
        rt.state = state
        self._log(rt, "state", {"state": state.value, **(info or {})})

    def _robot_call(self, rt: MissionRuntime, op: str, **kwargs: Any) -> dict[str, Any]:
        self._log(rt, "robot_action", {"action": op, "kwargs": kwargs, "dry_run": self._dry_run})
        if self._dry_run:
            return {"ok": True, "dry_run": True, "action": op}

        if op == "init":
            return self._robot.init_robot()
        if op == "move":
            return self._robot.move_distance(kwargs.get("distance_m", 0.2), kwargs.get("speed_mps", 0.15))
        if op == "turn":
            return self._robot.turn_angle(kwargs.get("angle_deg", 15.0), kwargs.get("angular_speed_dps", 25.0))
        if op == "gripper":
            return self._robot.gripper(kwargs.get("action", "open"), kwargs.get("side", "both"))
        if op == "arm_preset":
            return self._robot.arm_preset(kwargs.get("arm", "right"), kwargs.get("preset", "extend"))
        raise ValueError(f"unknown robot action: {op}")

    def _fetch_frame(self, camera_name: str) -> bytes | None:
        if self._dry_run:
            return b"mock-frame"
        frame, _ = self._robot.fetch_camera_frame(camera_name)
        return frame

    def _check_stop(self, rt: MissionRuntime) -> None:
        if self._stop_event.is_set():
            raise RuntimeError("mission manually stopped")

    def _run(self, rt: MissionRuntime, options: dict[str, Any]) -> None:
        try:
            self._set_state(rt, MissionState.PARSE)
            self._check_stop(rt)

            self._robot_call(rt, "init")

            self._set_state(rt, MissionState.NAV_ALONG_ROUTE, {"speed": "default"})
            self._robot_call(rt, "move", distance_m=1.2, speed_mps=options.get("default_speed_mps", 0.18))

            self._set_state(rt, MissionState.STALL_SEARCH)
            head = self._fetch_frame("head")
            detect = self._perception.detect_stall_and_item(head, rt.target.stall_label, rt.target.item_name)
            self._log(rt, "head_detect", {"has_target": detect["has_target"]})

            self._set_state(rt, MissionState.STALL_ALIGN)
            if not detect["has_target"]:
                self._robot_call(rt, "turn", angle_deg=20.0)
            for _ in range(3):
                self._check_stop(rt)
                time.sleep(1.0)

            self._set_state(rt, MissionState.ITEM_APPROACH)
            self._robot_call(rt, "move", distance_m=0.4, speed_mps=0.09)

            self._set_state(rt, MissionState.GRASP)
            left = self._fetch_frame("left")
            right = self._fetch_frame("right")
            arm_choice = self._perception.choose_arm_by_wrist_view(left, right)
            arm = arm_choice["arm"]
            self._log(rt, "arm_choice", arm_choice)
            self._robot_call(rt, "gripper", action="open", side=arm)
            self._robot_call(rt, "arm_preset", arm=arm, preset="extend")
            self._robot_call(rt, "gripper", action="close", side=arm)
            self._robot_call(rt, "arm_preset", arm=arm, preset="retract")

            self._set_state(rt, MissionState.RETREAT)
            self._robot_call(rt, "move", distance_m=-1.5, speed_mps=0.15)

            self._set_state(rt, MissionState.RETURN_ORIENT)
            self._robot_call(rt, "turn", angle_deg=90.0)

            self._set_state(rt, MissionState.HUMAN_SEARCH)
            self._robot_call(rt, "move", distance_m=0.8, speed_mps=0.15)
            human_frame = self._fetch_frame("head")
            human = self._perception.detect_human_and_hand(human_frame)
            self._log(rt, "human_detect", human)

            self._set_state(rt, MissionState.HAND_APPROACH)
            self._robot_call(rt, "arm_preset", arm=arm, preset="extend")

            self._set_state(rt, MissionState.RELEASE)
            self._robot_call(rt, "gripper", action="open", side=arm)

            self._set_state(rt, MissionState.RESET)
            self._robot_call(rt, "arm_preset", arm=arm, preset="retract")

            self._set_state(rt, MissionState.DONE)
        except Exception as e:
            rt.error = str(e)
            self._set_state(rt, MissionState.FAILSAFE, {"error": rt.error})
        finally:
            rt.finished_at = datetime.now()
            with self._lock:
                self._history.append(rt)
                self._current = None
