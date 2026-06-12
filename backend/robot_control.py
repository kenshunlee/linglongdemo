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

        self.backend_local_ip = os.getenv("BACKEND_LOCAL_IP", "")
        self.robot_ip = os.getenv("ROBOT_IP", "192.168.1.28")
        self.cmd_port = int(os.getenv("ROBOT_CMD_PORT", "3336"))
        self.state_port = int(os.getenv("ROBOT_STATE_PORT", "3333"))
        self.mode_port = int(os.getenv("ROBOT_MODE_PORT", "4141"))
        self.chassis_ip = os.getenv("CHASSIS_IP", "192.168.1.204")
        self.chassis_cmd_port = int(os.getenv("CHASSIS_CMD_PORT", "19205"))
        self.chassis_cmd_timeout_s = _parse_float(os.getenv("CHASSIS_CMD_TIMEOUT_S", "0.2"), 0.2)
        self.mode_settle_s = max(0.0, _parse_float(os.getenv("ROBOT_MODE_SETTLE_S", "0.6"), 0.6))
        self.motion_auto_prepare = _as_bool(os.getenv("ROBOT_MOTION_AUTO_PREPARE", "1"), True)
        self.motion_verify_feedback = _as_bool(os.getenv("ROBOT_MOTION_VERIFY_FEEDBACK", "1"), True)
        self.motion_feedback_timeout_s = max(0.05, _parse_float(os.getenv("ROBOT_MOTION_FEEDBACK_TIMEOUT_S", "0.6"), 0.6))
        self.motion_feedback_min_v = max(0.001, _parse_float(os.getenv("ROBOT_MOTION_FEEDBACK_MIN_V", "0.01"), 0.01))
        self.motion_strict = _as_bool(os.getenv("ROBOT_MOTION_STRICT", "0"), False)
        self.volume_percent = max(0, min(100, int(_parse_float(os.getenv("ROBOT_VOLUME_PERCENT", "70"), 70))))

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
        self._motion_prepared = False
        self._last_motion_diag: dict[str, Any] = {}
        self._last_action_result: dict[str, Any] = {}

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
            "backend_local_ip": self.backend_local_ip,
            "robot_ip": self.robot_ip,
            "cmd_port": self.cmd_port,
            "state_port": self.state_port,
            "mode_port": self.mode_port,
            "chassis": {
                "ip": self.chassis_ip,
                "cmd_port": self.chassis_cmd_port,
                "cmd_timeout_s": self.chassis_cmd_timeout_s,
            },
            "motion_checks": {
                "mode_settle_s": self.mode_settle_s,
                "auto_prepare": self.motion_auto_prepare,
                "verify_feedback": self.motion_verify_feedback,
                "feedback_timeout_s": self.motion_feedback_timeout_s,
                "feedback_min_v": self.motion_feedback_min_v,
                "strict": self.motion_strict,
                "prepared": self._motion_prepared,
                "last_diag": self._last_motion_diag,
                "last_action_result": self._last_action_result,
            },
            "audio": {
                "volume_percent": self.volume_percent,
            },
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
                chassis_tcp_on_send=True,
                auto_state_thread=True,
            )
            # Ensure base motion uses the chassis TCP endpoint expected by the SDK tests.
            self._sdk.configure_chassis_tcp(
                ip=self.chassis_ip,
                port=self.chassis_cmd_port,
                timeout_s=self.chassis_cmd_timeout_s,
            )

        if self._mode is None:
            RobotModeManager = self._sdk_api["RobotModeManager"]
            self._mode = RobotModeManager(ip=self.robot_ip, port=self.mode_port)

    def _prepare_motion_mode_if_needed(self) -> None:
        if not self.motion_auto_prepare:
            return
        if self._motion_prepared:
            return
        self._mode.robot_enable_up()
        if self.mode_settle_s > 0:
            time.sleep(self.mode_settle_s)
        self._mode.robot_autonomous_mode()
        if self.mode_settle_s > 0:
            time.sleep(min(self.mode_settle_s, 1.0))
        self._motion_prepared = True

    @staticmethod
    def _build_action_result(
        *,
        ok: bool,
        reason: str = "",
        suggestion: str = "",
        diag: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        result = {
            "check_result": bool(ok),
            "reason": reason if not ok else "",
            "suggestion": suggestion if not ok else "",
        }
        if diag is not None:
            result["diag"] = diag
        return result

    def _remember_action_result(self, result: dict[str, Any]) -> None:
        self._last_action_result = dict(result)

    def _sample_base_status(self, timeout_s: float = 0.05) -> tuple[float, float] | None:
        try:
            self._sdk.fetch_robot_state(timeout=float(timeout_s))
            return float(self._sdk.ctrl.car_translation_status), float(self._sdk.ctrl.car_rotation_status)
        except Exception:
            return None

    def _sample_motion_snapshot(self, timeout_s: float = 0.05) -> dict[str, Any] | None:
        try:
            self._sdk.fetch_robot_state(timeout=float(timeout_s))
        except Exception:
            return None

        sens = self._sdk.sens
        return {
            "cap_l": float(sens.cap_rate[0]),
            "cap_r": float(sens.cap_rate[1]),
            "left_eef": [float(x) for x in sens.epos_h[0, :6]],
            "right_eef": [float(x) for x in sens.epos_h[1, :6]],
            "waist_eef": [float(x) for x in sens.epos_waist[:6]],
            "base_vx": float(self._sdk.ctrl.car_translation_status),
            "base_w": float(self._sdk.ctrl.car_rotation_status),
            "battery": float(self._sdk.battery_level),
        }

    @staticmethod
    def _max_pose_error(actual: list[float], target: list[float]) -> float:
        return max(abs(float(a) - float(b)) for a, b in zip(actual[:6], target[:6]))

    def _verify_cap_feedback(self, target_cap_l: float, target_cap_r: float) -> dict[str, Any]:
        deadline = time.time() + self.motion_feedback_timeout_s
        samples = 0
        max_err_l = float("inf")
        max_err_r = float("inf")
        snapshot: dict[str, Any] | None = None
        while time.time() < deadline:
            snapshot = self._sample_motion_snapshot(timeout_s=0.05)
            if snapshot is None:
                time.sleep(0.03)
                continue
            samples += 1
            max_err_l = abs(float(snapshot["cap_l"]) - float(target_cap_l))
            max_err_r = abs(float(snapshot["cap_r"]) - float(target_cap_r))
            if max(max_err_l, max_err_r) <= 0.15:
                break
            time.sleep(0.03)

        ok = max(max_err_l, max_err_r) <= 0.15
        diag = {
            "verified": True,
            "ok": ok,
            "samples": samples,
            "target_cap_l": float(target_cap_l),
            "target_cap_r": float(target_cap_r),
            "actual_cap_l": None if snapshot is None else round(float(snapshot["cap_l"]), 4),
            "actual_cap_r": None if snapshot is None else round(float(snapshot["cap_r"]), 4),
            "max_error": None if snapshot is None else round(max(max_err_l, max_err_r), 4),
            "timeout_s": self.motion_feedback_timeout_s,
        }
        if not ok and self.motion_strict:
            raise RuntimeError(f"夹爪未检测到有效反馈，diag={diag}")
        return diag

    def _verify_eef_feedback(
        self,
        target_eef_l: list[float],
        target_eef_r: list[float],
        *,
        check_left: bool = True,
        check_right: bool = True,
    ) -> dict[str, Any]:
        deadline = time.time() + self.motion_feedback_timeout_s
        samples = 0
        last_snapshot: dict[str, Any] | None = None
        left_err = None
        right_err = None
        while time.time() < deadline:
            last_snapshot = self._sample_motion_snapshot(timeout_s=0.05)
            if last_snapshot is None:
                time.sleep(0.03)
                continue
            samples += 1
            if check_left:
                left_err = self._max_pose_error(last_snapshot["left_eef"], target_eef_l)
            if check_right:
                right_err = self._max_pose_error(last_snapshot["right_eef"], target_eef_r)
            if ((not check_left) or (left_err is not None and left_err <= 0.35)) and (
                (not check_right) or (right_err is not None and right_err <= 0.35)
            ):
                break
            time.sleep(0.03)

        ok_left = (not check_left) or (left_err is not None and left_err <= 0.35)
        ok_right = (not check_right) or (right_err is not None and right_err <= 0.35)
        ok = ok_left and ok_right
        diag = {
            "verified": True,
            "ok": ok,
            "samples": samples,
            "target_left": [round(float(x), 4) for x in target_eef_l[:6]],
            "target_right": [round(float(x), 4) for x in target_eef_r[:6]],
            "actual_left": None if last_snapshot is None else [round(float(x), 4) for x in last_snapshot["left_eef"]],
            "actual_right": None if last_snapshot is None else [round(float(x), 4) for x in last_snapshot["right_eef"]],
            "left_error": None if left_err is None else round(float(left_err), 4),
            "right_error": None if right_err is None else round(float(right_err), 4),
            "timeout_s": self.motion_feedback_timeout_s,
        }
        if not ok and self.motion_strict:
            raise RuntimeError(f"末端动作未检测到有效反馈，diag={diag}")
        return diag

    def _verify_motion_feedback(self, expect_vx: float, expect_w: float) -> dict[str, Any]:
        if not self.motion_verify_feedback or (abs(expect_vx) < 1e-6 and abs(expect_w) < 1e-6):
            diag = {"verified": False, "reason": "feedback_check_disabled_or_zero_target"}
            self._last_motion_diag = diag
            return diag

        deadline = time.time() + self.motion_feedback_timeout_s
        max_abs_vx = 0.0
        max_abs_w = 0.0
        samples = 0
        while time.time() < deadline:
            st = self._sample_base_status(timeout_s=0.05)
            if st is not None:
                samples += 1
                vx, w = st
                max_abs_vx = max(max_abs_vx, abs(vx))
                max_abs_w = max(max_abs_w, abs(w))
                if abs(expect_vx) > 1e-6 and max_abs_vx >= self.motion_feedback_min_v:
                    break
                if abs(expect_w) > 1e-6 and max_abs_w >= self.motion_feedback_min_v:
                    break
            time.sleep(0.03)

        moved = False
        if abs(expect_vx) > 1e-6 and max_abs_vx >= self.motion_feedback_min_v:
            moved = True
        if abs(expect_w) > 1e-6 and max_abs_w >= self.motion_feedback_min_v:
            moved = True

        diag = {
            "verified": True,
            "moved": moved,
            "samples": samples,
            "max_abs_vx": round(max_abs_vx, 4),
            "max_abs_w": round(max_abs_w, 4),
            "threshold": self.motion_feedback_min_v,
            "timeout_s": self.motion_feedback_timeout_s,
        }
        self._last_motion_diag = diag
        if not moved and self.motion_strict:
            raise RuntimeError(f"底盘未检测到实际速度反馈，diag={diag}")
        return diag

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
            self._motion_prepared = True
            result = {
                "ok": True,
                "message": "机器人初始化完成（已上使能 + 自主模式 + reset_to_init）",
                **self._build_action_result(ok=True),
            }
            self._remember_action_result(result)
            return result

    def set_volume(self, volume_percent: float) -> dict[str, Any]:
        with self._lock:
            self._ensure_clients()
            volume = max(0, min(100, int(round(float(volume_percent)))))
            self.volume_percent = volume

            # Try common SDK methods if available; keep API usable even if SDK lacks volume control.
            sdk_method_candidates = [
                "set_volume",
                "set_speaker_volume",
                "set_audio_volume",
                "set_tts_volume",
            ]
            applied_by = "local_cache"
            applied = False
            for name in sdk_method_candidates:
                fn = getattr(self._sdk, name, None)
                if not callable(fn):
                    continue
                try:
                    # Most SDKs accept either 0-100 or 0.0-1.0.
                    try:
                        fn(volume)
                    except TypeError:
                        fn(volume / 100.0)
                    applied_by = f"sdk.{name}"
                    applied = True
                    break
                except Exception:
                    continue

            result = {
                "ok": True,
                "volume_percent": volume,
                "applied": applied,
                "applied_by": applied_by,
                **self._build_action_result(ok=True),
            }
            self._remember_action_result(result)
            return result

    def move_distance(self, distance_m: float, speed_mps: float = 0.15) -> dict[str, Any]:
        with self._lock:
            self._ensure_clients()
            self._prepare_motion_mode_if_needed()
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
            motion_diag = self._verify_motion_feedback(expect_vx=vx, expect_w=0.0)
            ok = bool(motion_diag.get("moved"))
            result = {
                "ok": True,
                "distance_m": dist,
                "speed_mps": speed,
                "duration_s": round(duration, 3),
                "motion_diag": motion_diag,
                **self._build_action_result(
                    ok=ok,
                    reason="底盘未检测到有效线速度反馈",
                    suggestion="先检查机器人是否已上使能并处于自主模式，确认 CHASSIS_IP/CHASSIS_CMD_PORT 配置正确，必要时先调用 /robot/init 再重试。",
                    diag=motion_diag,
                ),
            }
            self._remember_action_result(result)
            return result

    def turn_angle(self, angle_deg: float, angular_speed_dps: float = 25.0) -> dict[str, Any]:
        with self._lock:
            self._ensure_clients()
            self._prepare_motion_mode_if_needed()
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
            motion_diag = self._verify_motion_feedback(expect_vx=0.0, expect_w=w)
            ok = bool(motion_diag.get("moved"))
            result = {
                "ok": True,
                "angle_deg": deg,
                "angular_speed_dps": w_deg,
                "duration_s": round(duration, 3),
                "motion_diag": motion_diag,
                **self._build_action_result(
                    ok=ok,
                    reason="底盘未检测到有效角速度反馈",
                    suggestion="确认底盘 TCP 速度链路可用，检查上使能/自主模式状态，必要时先调用 /robot/init 后再试。",
                    diag=motion_diag,
                ),
            }
            self._remember_action_result(result)
            return result

    def gripper(self, action: str, side: str = "both") -> dict[str, Any]:
        with self._lock:
            self._ensure_clients()
            self._prepare_motion_mode_if_needed()
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
            motion_diag = self._verify_cap_feedback(target_cap_l=cap_l, target_cap_r=cap_r)
            result = {
                "ok": True,
                "action": act,
                "side": side,
                "cap_l": cap_l,
                "cap_r": cap_r,
                "motion_diag": motion_diag,
                **self._build_action_result(
                    ok=bool(motion_diag.get("ok")),
                    reason="夹爪未检测到有效反馈",
                    suggestion="确认夹爪机构未被急停或保护限制，检查目标侧夹爪是否可达；若现场允许，可增大反馈超时后重试。",
                    diag=motion_diag,
                ),
            }
            self._remember_action_result(result)
            return result

    def arm_preset(self, arm: str, preset: str) -> dict[str, Any]:
        with self._lock:
            self._ensure_clients()
            self._prepare_motion_mode_if_needed()
            arm_norm = arm.strip().lower()
            preset_norm = preset.strip().lower()

            eef_l, eef_r, waist_pos, waist_att, head_att, cap_l, cap_r = self._current_targets()
            check_left = False
            check_right = False

            if arm_norm == "left":
                if preset_norm == "extend":
                    eef_l = list(self.left_extend_eef)
                else:
                    eef_l = list(self.left_retract_eef)
                check_left = True
            elif arm_norm == "right":
                if preset_norm == "extend":
                    eef_r = list(self.right_extend_eef)
                else:
                    eef_r = list(self.right_retract_eef)
                check_right = True
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
            motion_diag = self._verify_eef_feedback(
                target_eef_l=eef_l,
                target_eef_r=eef_r,
                check_left=check_left,
                check_right=check_right,
            )
            result = {
                "ok": True,
                "arm": arm_norm,
                "preset": preset_norm,
                "motion_diag": motion_diag,
                **self._build_action_result(
                    ok=bool(motion_diag.get("ok")),
                    reason="机械臂预置未检测到有效末端反馈",
                    suggestion="确认机器人已进入自主模式且对应臂无碰撞/限位；若末端目标变化较小，可适当放宽反馈阈值或延长超时。",
                    diag=motion_diag,
                ),
            }
            self._remember_action_result(result)
            return result

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
            self._prepare_motion_mode_if_needed()
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
            motion_diag = self._sample_motion_snapshot(timeout_s=self.motion_feedback_timeout_s)
            result = {
                "ok": True,
                "task_name": name,
                "actions": len(bundle.actions),
                "motion_diag": {
                    "verified": motion_diag is not None,
                    "snapshot": motion_diag,
                },
                **self._build_action_result(
                    ok=motion_diag is not None,
                    reason="轨迹回放后未能获取到状态快照",
                    suggestion="检查状态 UDP/TCP 链路是否正常，必要时增加 motion feedback 超时或检查轨迹是否真正触发了硬件动作。",
                    diag={"snapshot": motion_diag},
                ),
            }
            self._remember_action_result(result)
            return result

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

        if path == "/robot/volume":
            volume_percent = _parse_float(body.get("volume_percent"), robot_service.volume_percent)
            data = robot_service.set_volume(volume_percent=volume_percent)
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
