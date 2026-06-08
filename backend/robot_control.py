"""
灵龙 H 机器人手动操控接口（独立于 ASR 语音处理）。

提供 HTTP 端点能力：
- 初始化（上电/进入自主）
- 底盘前进/后退、左右转向
- 夹爪开合
- 左右手预置动作（前伸/收回）
- 轨迹任务回放（按 config/<task>/ 加载）
- 相机画面代理（通过外部 URL 拉取 JPEG 帧）
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx


def _as_bool(v: str | None, default: bool = False) -> bool:
    if v is None:
        return default
    return str(v).strip().lower() in {"1", "true", "yes", "y", "on"}


def _parse_float(v: Any, default: float) -> float:
    try:
        return float(v)
    except Exception:
        return default


class RobotControlService:
    def __init__(self) -> None:
        self.enabled = _as_bool(os.getenv("ROBOT_SDK_ENABLED", "1"), True)

        self.robot_ip = os.getenv("ROBOT_IP", "192.168.1.28")
        self.cmd_port = int(os.getenv("ROBOT_CMD_PORT", "3336"))
        self.state_port = int(os.getenv("ROBOT_STATE_PORT", "3333"))
        self.mode_port = int(os.getenv("ROBOT_MODE_PORT", "4141"))

        # 任务目录：默认优先使用 SDK 文档目录下 config
        self.config_root = Path(
            os.getenv(
                "ROBOT_CONFIG_ROOT",
                str(Path(__file__).resolve().parents[2] / "灵龙H_API说明" / "sdk_v1.0" / "sdk" / "config"),
            )
        )

        self.head_camera_url = os.getenv("HEAD_CAMERA_URL", "")
        self.left_camera_url = os.getenv("LEFT_HAND_CAMERA_URL", "")
        self.right_camera_url = os.getenv("RIGHT_HAND_CAMERA_URL", "")

        self._lock = threading.RLock()
        self._sdk = None
        self._mode = None
        self._last_error = ""

        # 动作预置（近似动作，可按现场再调）
        self.left_extend_eef = [0.45, 0.30, 0.82, 0.0, 0.0, 0.0]
        self.left_retract_eef = [0.30, 0.22, 0.65, 0.0, 0.0, 0.0]
        self.right_extend_eef = [0.45, -0.30, 0.82, 0.0, 0.0, 0.0]
        self.right_retract_eef = [0.30, -0.22, 0.65, 0.0, 0.0, 0.0]

        self._sdk_ready = False
        self._sdk_reason = "未初始化"
        self._sdk_api = None
        self._try_prepare_sdk()

    def _try_prepare_sdk(self) -> None:
        if not self.enabled:
            self._sdk_ready = False
            self._sdk_reason = "ROBOT_SDK_ENABLED=0"
            return

        sdk_repo_root = Path(__file__).resolve().parents[2] / "灵龙H_API说明" / "sdk_v1.0" / "sdk"
        if not sdk_repo_root.exists():
            self._sdk_ready = False
            self._sdk_reason = f"SDK 目录不存在: {sdk_repo_root}"
            return

        sdk_root_str = str(sdk_repo_root)
        if sdk_root_str not in sys.path:
            sys.path.insert(0, sdk_root_str)

        try:
            from linglong_h_sdk import LinglongHSdkClass, ManiInterpStartSource, RobotModeManager, traj_replan

            self._sdk_api = {
                "LinglongHSdkClass": LinglongHSdkClass,
                "RobotModeManager": RobotModeManager,
                "ManiInterpStartSource": ManiInterpStartSource,
                "traj_replan": traj_replan,
            }
            self._sdk_ready = True
            self._sdk_reason = "SDK 可用"
        except Exception as e:
            self._sdk_ready = False
            self._sdk_reason = f"SDK 导入失败: {e}"

    def health(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "sdk_ready": self._sdk_ready,
            "sdk_reason": self._sdk_reason,
            "robot_ip": self.robot_ip,
            "cmd_port": self.cmd_port,
            "state_port": self.state_port,
            "mode_port": self.mode_port,
            "config_root": str(self.config_root),
            "camera": {
                "head": bool(self.head_camera_url),
                "left": bool(self.left_camera_url),
                "right": bool(self.right_camera_url),
            },
            "last_error": self._last_error,
        }

    def _ensure_clients(self) -> None:
        if not self._sdk_ready or self._sdk_api is None:
            raise RuntimeError(self._sdk_reason)

        if self._sdk is None:
            LinglongHSdkClass = self._sdk_api["LinglongHSdkClass"]
            self._sdk = LinglongHSdkClass(
                ip=self.robot_ip,
                port=self.cmd_port,
                state_port=self.state_port,
                chassis_tcp_on_send=False,
                auto_state_thread=True,
            )

        if self._mode is None:
            RobotModeManager = self._sdk_api["RobotModeManager"]
            self._mode = RobotModeManager(ip=self.robot_ip, port=self.mode_port)

    def _current_targets(self) -> tuple[list[float], list[float], list[float], list[float], list[float], float, float]:
        sdk = self._sdk
        eef_l = list(sdk.ctrl.arm_pos_exp_l[:3]) + list(sdk.ctrl.arm_att_exp_l[:3])
        eef_r = list(sdk.ctrl.arm_pos_exp_r[:3]) + list(sdk.ctrl.arm_att_exp_r[:3])
        waist_pos = list(sdk.ctrl.waist_pos_exp[:3])
        waist_att = list(sdk.ctrl.waist_att_exp[:3])
        head_att = list(sdk.ctrl.head_att_exp[:3])
        return eef_l, eef_r, waist_pos, waist_att, head_att, float(sdk.ctrl.cap_l), float(sdk.ctrl.cap_r)

    def init_robot(self) -> dict[str, Any]:
        with self._lock:
            self._ensure_clients()
            self._mode.robot_enable_up()
            self._mode.robot_autonomous_mode()
            self._sdk.reset_to_init(send_time=2.5, mode="eef", interp_start="ctrl")
            return {"ok": True, "message": "机器人初始化完成（已上使能 + 自主模式 + reset_to_init）"}

    def move_distance(self, distance_m: float, speed_mps: float = 0.15) -> dict[str, Any]:
        with self._lock:
            self._ensure_clients()
            dist = float(distance_m)
            speed = max(0.03, min(abs(float(speed_mps)), 0.5))
            duration = abs(dist) / speed if speed > 1e-6 else 0.0
            direction = 1.0 if dist >= 0 else -1.0
            vx = direction * speed

            start = time.time()
            while time.time() - start < duration:
                self._sdk.set_base_vel(vx, 0.0)
                self._sdk.send()
                time.sleep(0.05)

            self._sdk.set_base_vel(0.0, 0.0)
            self._sdk.send()
            return {"ok": True, "distance_m": dist, "speed_mps": speed, "duration_s": round(duration, 3)}

    def turn_angle(self, angle_deg: float, angular_speed_dps: float = 25.0) -> dict[str, Any]:
        with self._lock:
            self._ensure_clients()
            deg = float(angle_deg)
            w_deg = max(5.0, min(abs(float(angular_speed_dps)), 120.0))
            duration = abs(deg) / w_deg if w_deg > 1e-6 else 0.0
            direction = 1.0 if deg >= 0 else -1.0
            w = direction * (w_deg * 3.1415926 / 180.0)

            start = time.time()
            while time.time() - start < duration:
                self._sdk.set_base_vel(0.0, w)
                self._sdk.send()
                time.sleep(0.05)

            self._sdk.set_base_vel(0.0, 0.0)
            self._sdk.send()
            return {"ok": True, "angle_deg": deg, "angular_speed_dps": w_deg, "duration_s": round(duration, 3)}

    def gripper(self, action: str, side: str = "both") -> dict[str, Any]:
        with self._lock:
            self._ensure_clients()
            act = action.strip().lower()
            tgt = 1.0 if act == "open" else 0.0
            eef_l, eef_r, waist_pos, waist_att, head_att, cap_l, cap_r = self._current_targets()

            side_l = side in {"left", "both"}
            side_r = side in {"right", "both"}
            if side_l:
                cap_l = tgt
            if side_r:
                cap_r = tgt

            self._sdk.send_eef_interpolation(
                target_eef_l=eef_l,
                target_eef_r=eef_r,
                send_time=0.8,
                target_waist_pos=waist_pos,
                target_waist_att=waist_att,
                target_head_att=head_att,
                target_cap_l=cap_l,
                target_cap_r=cap_r,
                interp_start="ctrl",
            )
            return {"ok": True, "action": act, "side": side, "cap_l": cap_l, "cap_r": cap_r}

    def arm_preset(self, arm: str, preset: str) -> dict[str, Any]:
        with self._lock:
            self._ensure_clients()
            arm_norm = arm.strip().lower()
            preset_norm = preset.strip().lower()

            eef_l, eef_r, waist_pos, waist_att, head_att, cap_l, cap_r = self._current_targets()

            if arm_norm == "left":
                if preset_norm == "extend":
                    eef_l = list(self.left_extend_eef)
                else:
                    eef_l = list(self.left_retract_eef)
            elif arm_norm == "right":
                if preset_norm == "extend":
                    eef_r = list(self.right_extend_eef)
                else:
                    eef_r = list(self.right_retract_eef)
            else:
                raise ValueError("arm 必须是 left 或 right")

            self._sdk.send_eef_interpolation(
                target_eef_l=eef_l,
                target_eef_r=eef_r,
                send_time=1.2,
                target_waist_pos=waist_pos,
                target_waist_att=waist_att,
                target_head_att=head_att,
                target_cap_l=cap_l,
                target_cap_r=cap_r,
                interp_start="ctrl",
            )
            return {"ok": True, "arm": arm_norm, "preset": preset_norm}

    def list_trajectory_tasks(self) -> dict[str, Any]:
        with self._lock:
            if not self._sdk_ready or self._sdk_api is None:
                return {"ok": False, "tasks": [], "detail": self._sdk_reason}
            tr = self._sdk_api["traj_replan"]
            ok, tasks, err = tr.discover_trajectory_task_names(str(self.config_root))
            return {"ok": bool(ok), "tasks": tasks if ok else [], "detail": err}

    def playback_trajectory(self, task_name: str) -> dict[str, Any]:
        with self._lock:
            self._ensure_clients()
            tr = self._sdk_api["traj_replan"]
            ManiInterpStartSource = self._sdk_api["ManiInterpStartSource"]
            name = task_name.strip()
            if not name:
                raise ValueError("task_name 不能为空")

            ok, bundle, err = tr.load_trajectory_task_from_config_directory(str(self.config_root), name)
            if not ok:
                raise RuntimeError(f"加载轨迹失败: {err}")

            obj_udp = self._sdk.object_udp_receiver()
            ok_play = tr.trajectory_playback_task(
                self._sdk,
                bundle,
                obj_udp,
                ManiInterpStartSource.kCtrl,
                lambda msg: print(f"[trajectory] {msg}"),
                lambda msg: print(f"[trajectory:err] {msg}"),
            )
            if not ok_play:
                raise RuntimeError("轨迹回放执行失败")
            return {"ok": True, "task_name": name, "actions": len(bundle.actions)}

    def _camera_url(self, camera_name: str) -> str:
        cam = camera_name.strip().lower()
        if cam == "head":
            return self.head_camera_url
        if cam == "left":
            return self.left_camera_url
        if cam == "right":
            return self.right_camera_url
        return ""

    def fetch_camera_frame(self, camera_name: str) -> tuple[bytes, str]:
        url = self._camera_url(camera_name)
        if not url:
            raise RuntimeError(f"相机 {camera_name} 未配置 URL")

        timeout = httpx.Timeout(4.0, connect=2.0)
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            res = client.get(url)
            res.raise_for_status()
            ctype = (res.headers.get("content-type") or "").lower()
            if "jpeg" in ctype or "jpg" in ctype:
                return bytes(res.content), "image/jpeg"
            if "png" in ctype:
                return bytes(res.content), "image/png"
            raise RuntimeError(f"相机 URL 未返回图片格式: {ctype}")


robot_service = RobotControlService()


def handle_robot_get(path: str, query: dict[str, list[str]]) -> tuple[int, dict[str, Any] | bytes, str]:
    try:
        if path == "/robot/health":
            return 200, {"success": True, "data": robot_service.health()}, "application/json; charset=utf-8"

        if path == "/robot/trajectory/tasks":
            data = robot_service.list_trajectory_tasks()
            return 200, {"success": True, "data": data}, "application/json; charset=utf-8"

        if path == "/robot/camera/frame":
            camera = (query.get("camera", ["head"])[0] or "head").strip().lower()
            frame, ctype = robot_service.fetch_camera_frame(camera)
            return 200, frame, ctype

        return 404, {"detail": "Not Found"}, "application/json; charset=utf-8"
    except Exception as e:
        robot_service._last_error = str(e)
        return 500, {"success": False, "detail": str(e)}, "application/json; charset=utf-8"


def handle_robot_post(path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any], str]:
    try:
        if path == "/robot/init":
            data = robot_service.init_robot()
            return 200, {"success": True, "data": data}, "application/json; charset=utf-8"

        if path == "/robot/move":
            distance_m = _parse_float(body.get("distance_m"), 0.2)
            speed = _parse_float(body.get("speed_mps"), 0.15)
            data = robot_service.move_distance(distance_m=distance_m, speed_mps=speed)
            return 200, {"success": True, "data": data}, "application/json; charset=utf-8"

        if path == "/robot/turn":
            angle_deg = _parse_float(body.get("angle_deg"), 30.0)
            speed_dps = _parse_float(body.get("angular_speed_dps"), 25.0)
            data = robot_service.turn_angle(angle_deg=angle_deg, angular_speed_dps=speed_dps)
            return 200, {"success": True, "data": data}, "application/json; charset=utf-8"

        if path == "/robot/gripper":
            action = str(body.get("action", "open")).strip().lower()
            side = str(body.get("side", "both")).strip().lower()
            if action not in {"open", "close"}:
                raise ValueError("action 必须是 open 或 close")
            if side not in {"left", "right", "both"}:
                raise ValueError("side 必须是 left/right/both")
            data = robot_service.gripper(action=action, side=side)
            return 200, {"success": True, "data": data}, "application/json; charset=utf-8"

        if path == "/robot/arm/preset":
            arm = str(body.get("arm", "left")).strip().lower()
            preset = str(body.get("preset", "extend")).strip().lower()
            if preset not in {"extend", "retract"}:
                raise ValueError("preset 必须是 extend 或 retract")
            data = robot_service.arm_preset(arm=arm, preset=preset)
            return 200, {"success": True, "data": data}, "application/json; charset=utf-8"

        if path == "/robot/trajectory/play":
            task_name = str(body.get("task_name", "")).strip()
            data = robot_service.playback_trajectory(task_name)
            return 200, {"success": True, "data": data}, "application/json; charset=utf-8"

        return 404, {"detail": "Not Found"}, "application/json; charset=utf-8"
    except Exception as e:
        robot_service._last_error = str(e)
        return 500, {"success": False, "detail": str(e)}, "application/json; charset=utf-8"


def maybe_handle_robot_request(method: str, raw_path: str, body_bytes: bytes | None = None):
    parsed = urlparse(raw_path)
    if not parsed.path.startswith("/robot/"):
        return None

    if method == "GET":
        return handle_robot_get(parsed.path, parse_qs(parsed.query))

    if method == "POST":
        payload = {}
        if body_bytes:
            payload = json.loads(body_bytes.decode("utf-8"))
        return handle_robot_post(parsed.path, payload)

    return 405, {"detail": "Method Not Allowed"}, "application/json; charset=utf-8"
