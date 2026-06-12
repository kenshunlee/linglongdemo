from __future__ import annotations

import json
from datetime import datetime
import math
import os
import threading
from typing import Any
from pathlib import Path
import zipfile

from reflow_client import ReflowClient


class MissionReflowBridge:
    def __init__(
        self,
        client: ReflowClient,
        enabled: bool,
        robot_service: Any | None,
        team_id: str,
        robot_id: str,
        scene_id: str,
        task_prefix: str,
        shared_state: dict[str, Any] | None = None,
    ) -> None:
        self._client = client
        self._enabled = enabled
        self._robot_service = robot_service
        self._team_id = team_id
        self._robot_id = robot_id
        self._scene_id = scene_id
        self._task_prefix = task_prefix
        self._shared_state = shared_state if shared_state is not None else {}
        self._lock = threading.RLock()
        self._ctx: dict[str, dict[str, Any]] = {}
        self._coord_sys = str(os.getenv("REFLOW_COORD_SYS", "slam_local")).strip() or "slam_local"
        self._pose_source = str(os.getenv("REFLOW_POSE_SOURCE", "robot")).strip() or "robot"
        self._space_version = str(os.getenv("REFLOW_SPACE_VERSION", "scene_market_v1.0")).strip() or "scene_market_v1.0"
        self._coord_note = str(os.getenv("REFLOW_COORD_NOTE", "任务起点为 SLAM/里程计原点")).strip()
        self._fixed_task_id = str(os.getenv("REFLOW_TASK_ID", "")).strip()
        self._buffer_root = Path(os.getenv("REFLOW_BUFFER_DIR", str(Path(__file__).resolve().parents[2] / "output" / "reflow_buffer")))
        self._package_dir = Path(os.getenv("REFLOW_PACKAGE_DIR", str(Path(__file__).resolve().parents[2] / "output" / "reflow_packages")))
        self._auto_zip_on_stop = str(os.getenv("REFLOW_AUTO_ZIP_ON_STOP", "1")).strip() in {"1", "true", "yes", "on"}

    def _now_iso(self) -> str:
        return datetime.now().astimezone().isoformat(timespec="seconds")

    def _safe(self, fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception:
            return None

    def _session_dir(self, ctx: dict[str, Any]) -> Path | None:
        session_id = str(ctx.get("session_id") or "").strip()
        if not session_id:
            return None
        return self._buffer_root / session_id

    def _ensure_session_dirs(self, ctx: dict[str, Any]) -> Path | None:
        session_dir = self._session_dir(ctx)
        if session_dir is None:
            return None
        (session_dir / "media").mkdir(parents=True, exist_ok=True)
        return session_dir

    def _write_json(self, path: Path, data: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _append_jsonl(self, path: Path, item: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(item, ensure_ascii=False) + "\n")

    def _package_path(self, ctx: dict[str, Any], suffix: str = "") -> Path | None:
        session_id = str(ctx.get("session_id") or "").strip()
        if not session_id:
            return None
        stamp = datetime.now().strftime("%Y%m%d%H%M%S")
        name = f"{self._scene_id}_{self._team_id}_{session_id}_{stamp}{suffix}.zip"
        return self._package_dir / name

    def _write_session_manifest(self, ctx: dict[str, Any], snap: dict[str, Any]) -> None:
        session_dir = self._ensure_session_dirs(ctx)
        if session_dir is None:
            return
        manifest = {
            "session": {
                "team_id": self._team_id,
                "robot_id": self._robot_id,
                "scene_id": self._scene_id,
                "task_id": ctx.get("task_id"),
                "session_id": ctx.get("session_id"),
                "space_version": self._space_version,
                "coord_sys": self._coord_sys,
                "coord_note": self._coord_note,
                "pose_source": self._pose_source,
            },
            "task": {
                "task_status": snap.get("state", "IDLE").lower(),
                "voice_intent": ctx.get("voice_intent") or snap.get("command_text"),
                "checkpoints": ctx.get("checkpoints") or [],
            },
            "latest_snapshot": snap,
        }
        self._write_json(session_dir / "session_manifest.json", manifest)
        if ctx.get("task_report"):
            self._write_json(session_dir / "task.json", ctx["task_report"])

    def _zip_session(self, ctx: dict[str, Any]) -> None:
        if not self._auto_zip_on_stop:
            return
        session_dir = self._session_dir(ctx)
        if session_dir is None or not session_dir.exists():
            return
        self._package_dir.mkdir(parents=True, exist_ok=True)
        zip_path = self._package_path(ctx)
        if zip_path is None:
            return
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for file_path in session_dir.rglob("*"):
                if file_path.is_file():
                    zf.write(file_path, file_path.relative_to(session_dir))

    def _get_ctx(self, snap: dict[str, Any]) -> dict[str, Any]:
        mission_id = str(snap.get("mission_id") or "")
        with self._lock:
            if mission_id not in self._ctx:
                task_id = self._fixed_task_id or f"{self._task_prefix}-{datetime.now().strftime('%Y%m%d%H%M%S')}"
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
                    "checkpoints": [],
                    "voice_intent": str(snap.get("command_text") or "").strip(),
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
                "space_version": self._space_version,
                "coord_sys": self._coord_sys,
                "coord_note": self._coord_note,
                "pose_source": self._pose_source,
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
        self._write_session_manifest(ctx, snap)
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
        ctx["report_seq"] = int(ctx.get("report_seq") or 0) + 1
        key_suffix = f"-{ctx['report_seq']}" if status == "running" else ""
        payload = {
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
            "voice_intent": ctx.get("voice_intent") or snap.get("command_text"),
            "checkpoints": ctx.get("checkpoints") or None,
            "completion_note": note or None,
            "idempotency_key": f"{ctx['mission_id']}-task-{status}{key_suffix}",
        }
        ctx["task_report"] = payload
        self._safe(
            self._client.report_task,
            payload=payload,
            idempotency_key=payload["idempotency_key"],
        )
        self._write_session_manifest(ctx, snap)

    def _capture_media(self, ctx: dict[str, Any], label: str) -> None:
        if not ctx.get("session_id") or self._robot_service is None:
            return
        try:
            self._robot_service.save_reflow_snapshots(ctx["session_id"], prefix=label)
        except Exception:
            pass

    def _record_checkpoint(self, ctx: dict[str, Any], checkpoint_id: str, status: str, snap: dict[str, Any], *, note: str = "", asr_active: bool = False) -> None:
        checkpoint = {
            "checkpoint_id": checkpoint_id,
            "status": status,
            "coord_sys": self._coord_sys,
            "x": round(ctx["x"], 3),
            "y": round(ctx["y"], 3),
            "z": 0.0,
            "voice_intent": ctx.get("voice_intent") or None,
            "asr_active": bool(asr_active or ctx.get("voice_intent")),
            "timestamp": snap.get("finished_at") or self._now_iso(),
        }
        if note:
            checkpoint["note"] = note
        ctx.setdefault("checkpoints", []).append(checkpoint)
        self._capture_media(ctx, checkpoint_id)
        self._report_task(ctx, "running", snap, note=note or status)

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
                "coord_sys": self._coord_sys,
                "pose_source": self._pose_source,
                "points": [point],
                "idempotency_key": f"{ctx['mission_id']}-traj-{ctx['traj_seq']}",
            },
            idempotency_key=f"{ctx['mission_id']}-traj-{ctx['traj_seq']}",
        )
        self._append_jsonl(
            self._ensure_session_dirs(ctx) / "trajectory.jsonl",
            {
                **point,
                "session_id": ctx["session_id"],
                "task_id": ctx["task_id"],
                "team_id": self._team_id,
                "robot_id": self._robot_id,
                "scene_id": self._scene_id,
                "coord_sys": self._coord_sys,
                "pose_source": self._pose_source,
            },
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
        self._append_jsonl(
            self._ensure_session_dirs(ctx) / "embodied.jsonl",
            {
                **sample,
                "session_id": ctx["session_id"],
                "task_id": ctx["task_id"],
                "team_id": self._team_id,
                "robot_id": self._robot_id,
                "scene_id": self._scene_id,
            },
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
                "coord_sys": self._coord_sys,
                "pose_source": self._pose_source,
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
        self._append_jsonl(
            self._ensure_session_dirs(ctx) / "events.jsonl",
            {
                "session_id": ctx["session_id"],
                "task_id": ctx["task_id"],
                "team_id": self._team_id,
                "robot_id": self._robot_id,
                "scene_id": self._scene_id,
                "event_type": "other",
                "occurred_at": self._now_iso(),
                "x": round(ctx["x"], 3),
                "y": round(ctx["y"], 3),
                "description": description,
            },
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
                self._record_checkpoint(ctx, "CP-START", "任务开始", snap, note="mission started", asr_active=True)
                self._report_task(ctx, "running", snap, note="mission started")
            if state == "STALL_SEARCH":
                stall_id = str((snap.get("target") or {}).get("stall_id") or "").strip()
                self._record_checkpoint(ctx, f"STALL-{stall_id or 'SEARCH'}", "到达摊位/搜索中", snap)
            if state == "GRASP":
                self._record_checkpoint(ctx, "CP-GRASP", "抓取中", snap)
            if state == "HUMAN_SEARCH":
                self._record_checkpoint(ctx, "CP-HUMAN-SEARCH", "回到终点/找人", snap)
            if state == "HAND_APPROACH":
                self._record_checkpoint(ctx, "CP-HAND", "递交中", snap)
            if state == "DONE":
                self._record_checkpoint(ctx, "CP-END", "完成", snap, note="overall_complete=true")
                self._report_task(ctx, "completed", snap, note="mission completed")
                self._write_session_manifest(ctx, snap)
                self._zip_session(ctx)
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
                self._record_checkpoint(ctx, "CP-FAIL", "失败", snap, note=str(payload.get("error") or "mission failed"))
                self._append_event(ctx, payload.get("error") or "mission failed")
                self._report_task(ctx, "failed", snap, note=str(payload.get("error") or "failed"))
                self._write_session_manifest(ctx, snap)
                self._zip_session(ctx)
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

        if event == "head_detect":
            self._record_checkpoint(ctx, "STALL-DETECTED", "识别完成", snap, note=f"has_target={bool(payload.get('has_target'))}")

        if event == "human_detect":
            self._record_checkpoint(ctx, "CP-HUMAN-DETECTED", "找到人/手部", snap)
