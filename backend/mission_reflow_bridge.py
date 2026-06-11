from __future__ import annotations

from datetime import datetime
import math
import threading
from typing import Any

from reflow_client import ReflowClient


class MissionReflowBridge:
    def __init__(
        self,
        client: ReflowClient,
        enabled: bool,
        team_id: str,
        robot_id: str,
        scene_id: str,
        task_prefix: str,
        shared_state: dict[str, Any] | None = None,
    ) -> None:
        self._client = client
        self._enabled = enabled
        self._team_id = team_id
        self._robot_id = robot_id
        self._scene_id = scene_id
        self._task_prefix = task_prefix
        self._shared_state = shared_state if shared_state is not None else {}
        self._lock = threading.RLock()
        self._ctx: dict[str, dict[str, Any]] = {}

    def _now_iso(self) -> str:
        return datetime.now().astimezone().isoformat(timespec="seconds")

    def _safe(self, fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception:
            return None

    def _get_ctx(self, snap: dict[str, Any]) -> dict[str, Any]:
        mission_id = str(snap.get("mission_id") or "")
        with self._lock:
            if mission_id not in self._ctx:
                task_id = f"{self._task_prefix}-{datetime.now().strftime('%Y%m%d%H%M%S')}"
                self._ctx[mission_id] = {
                    "mission_id": mission_id,
                    "task_id": task_id,
                    "session_id": "",
                    "start_at": snap.get("started_at") or self._now_iso(),
                    "x": 0.0,
                    "y": 0.0,
                    "yaw_deg": 0.0,
                    "traj_seq": 0,
                    "emb_seq": 0,
                }
            return self._ctx[mission_id]

    def _ensure_session(self, ctx: dict[str, Any], snap: dict[str, Any]) -> None:
        if not self._enabled or ctx.get("session_id"):
            return

        self._safe(self._client.login)
        resp = self._safe(
            self._client.create_session,
            payload={
                "team_id": self._team_id,
                "robot_id": self._robot_id,
                "scene_id": self._scene_id,
                "task_id": ctx["task_id"],
                "coord_sys": "SH2000",
                "pose_source": "robot",
                "mode": "auto",
                "body_type": "biped",
                "planned_start_at": ctx["start_at"],
                "idempotency_key": f"{ctx['mission_id']}-session",
            },
            idempotency_key=f"{ctx['mission_id']}-session",
        )
        if not isinstance(resp, dict):
            return
        data = resp.get("data") or {}
        session_id = str(data.get("session_id") or "").strip()
        if not session_id:
            return

        ctx["session_id"] = session_id
        if self._shared_state is not None:
            self._shared_state.update(
                {
                    "session_id": session_id,
                    "task_id": ctx["task_id"],
                    "scene_id": self._scene_id,
                    "robot_id": self._robot_id,
                    "team_id": self._team_id,
                }
            )

    def _report_task(self, ctx: dict[str, Any], status: str, snap: dict[str, Any], note: str = "") -> None:
        if not self._enabled or not ctx.get("session_id"):
            return
        self._safe(
            self._client.report_task,
            payload={
                "team_id": self._team_id,
                "robot_id": self._robot_id,
                "scene_id": self._scene_id,
                "task_id": ctx["task_id"],
                "session_id": ctx["session_id"],
                "start_at": ctx["start_at"],
                "end_at": snap.get("finished_at") if status in {"completed", "failed", "aborted"} else None,
                "task_status": status,
                "avg_speed_mps": 0.15,
                "task_phase": snap.get("state"),
                "voice_intent": snap.get("command_text"),
                "completion_note": note or None,
                "idempotency_key": f"{ctx['mission_id']}-task-{status}",
            },
            idempotency_key=f"{ctx['mission_id']}-task-{status}",
        )

    def _append_traj_embodied(self, ctx: dict[str, Any]) -> None:
        if not self._enabled or not ctx.get("session_id"):
            return

        ts = self._now_iso()
        point = {
            "point_seq": ctx["traj_seq"],
            "timestamp": ts,
            "x": round(ctx["x"], 3),
            "y": round(ctx["y"], 3),
            "yaw": round(math.radians(ctx["yaw_deg"]), 4),
            "pose_source": "robot",
        }
        sample = {
            "sample_seq": ctx["emb_seq"],
            "timestamp": ts,
            "joint_positions": [0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
            "joint_velocities": [0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
            "joint_torques": [0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
            "battery_pct": 80.0,
            "mode": "auto",
            "sensors": {"simulated": True},
        }

        self._safe(
            self._client.batch_trajectory,
            payload={
                "session_id": ctx["session_id"],
                "team_id": self._team_id,
                "robot_id": self._robot_id,
                "scene_id": self._scene_id,
                "task_id": ctx["task_id"],
                "coord_sys": "SH2000",
                "pose_source": "robot",
                "points": [point],
                "idempotency_key": f"{ctx['mission_id']}-traj-{ctx['traj_seq']}",
            },
            idempotency_key=f"{ctx['mission_id']}-traj-{ctx['traj_seq']}",
        )
        self._safe(
            self._client.batch_embodied,
            payload={
                "session_id": ctx["session_id"],
                "team_id": self._team_id,
                "robot_id": self._robot_id,
                "scene_id": self._scene_id,
                "task_id": ctx["task_id"],
                "samples": [sample],
                "idempotency_key": f"{ctx['mission_id']}-emb-{ctx['emb_seq']}",
            },
            idempotency_key=f"{ctx['mission_id']}-emb-{ctx['emb_seq']}",
        )

        ctx["traj_seq"] += 1
        ctx["emb_seq"] += 1

    def _append_event(self, ctx: dict[str, Any], description: str) -> None:
        if not self._enabled or not ctx.get("session_id"):
            return
        self._safe(
            self._client.batch_events,
            payload={
                "session_id": ctx["session_id"],
                "coord_sys": "SH2000",
                "pose_source": "robot",
                "events": [
                    {
                        "team_id": self._team_id,
                        "robot_id": self._robot_id,
                        "scene_id": self._scene_id,
                        "task_id": ctx["task_id"],
                        "session_id": ctx["session_id"],
                        "event_type": "other",
                        "occurred_at": self._now_iso(),
                        "x": round(ctx["x"], 3),
                        "y": round(ctx["y"], 3),
                        "description": description,
                    }
                ],
                "idempotency_key": f"{ctx['mission_id']}-event-{ctx['emb_seq']}",
            },
            idempotency_key=f"{ctx['mission_id']}-event-{ctx['emb_seq']}",
        )

    def _integrate_action(self, ctx: dict[str, Any], action: str, kwargs: dict[str, Any]) -> None:
        if action == "move":
            d = float(kwargs.get("distance_m", 0.0))
            rad = math.radians(ctx["yaw_deg"])
            ctx["x"] += d * math.cos(rad)
            ctx["y"] += d * math.sin(rad)
        elif action == "turn":
            a = float(kwargs.get("angle_deg", 0.0))
            ctx["yaw_deg"] += a

    def handle_event(self, event: str, payload: dict[str, Any], snap: dict[str, Any]) -> None:
        if not self._enabled:
            return

        ctx = self._get_ctx(snap)
        self._ensure_session(ctx, snap)

        if event == "state":
            state = str(payload.get("state") or "")
            if state == "PARSE":
                self._report_task(ctx, "running", snap, note="mission started")
            if state == "DONE":
                self._report_task(ctx, "completed", snap, note="mission completed")
                if ctx.get("session_id"):
                    self._safe(
                        self._client.update_session,
                        ctx["session_id"],
                        {
                            "status": "completed",
                            "start_at": ctx["start_at"],
                            "end_at": snap.get("finished_at") or self._now_iso(),
                        },
                    )
            if state == "FAILSAFE":
                self._append_event(ctx, payload.get("error") or "mission failed")
                self._report_task(ctx, "failed", snap, note=str(payload.get("error") or "failed"))
                if ctx.get("session_id"):
                    self._safe(
                        self._client.update_session,
                        ctx["session_id"],
                        {
                            "status": "aborted",
                            "start_at": ctx["start_at"],
                            "end_at": snap.get("finished_at") or self._now_iso(),
                        },
                    )

        if event == "robot_action":
            action = str(payload.get("action") or "")
            kwargs = payload.get("kwargs") or {}
            if isinstance(kwargs, dict):
                self._integrate_action(ctx, action, kwargs)
            self._append_traj_embodied(ctx)
