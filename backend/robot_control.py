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

try:
    import cv2  # type: ignore[import-not-found]
    import numpy as np  # type: ignore[import-not-found]
except Exception:
    cv2 = None
    np = None

try:
    import rclpy  # type: ignore[import-not-found]
    from rclpy.node import Node  # type: ignore[import-not-found]
    from rclpy.qos import QoSPresetProfiles  # type: ignore[import-not-found]
    from sensor_msgs.msg import CompressedImage  # type: ignore[import-not-found]
except Exception:
    rclpy = None
    Node = object
    QoSPresetProfiles = None
    CompressedImage = object


def _as_bool(v: str | None, default: bool = False) -> bool:
    if v is None:
        return default
    return str(v).strip().lower() in {"1", "true", "yes", "y", "on"}


def _parse_float(v: Any, default: float) -> float:
    try:
        return float(v)
    except Exception:
        return default


def _parse_pose6(v: Any, *, name: str) -> list[float]:
    if not isinstance(v, (list, tuple)):
        raise ValueError(f"{name} 必须是长度为 6 的数组 [x,y,z,r,p,y]")
    if len(v) != 6:
        raise ValueError(f"{name} 长度必须为 6")
    out: list[float] = []
    for item in v:
        out.append(float(item))
    return out


class _Ros2CameraBridge:
    def __init__(self, *, topics: dict[str, str], record_dir: Path, record_fps: float = 12.0, enabled: bool = True) -> None:
        self.enabled = enabled
        self.topics = topics
        self.record_dir = record_dir
        self.record_fps = max(1.0, float(record_fps))

        self._lock = threading.RLock()
        self._latest_jpeg: dict[str, bytes] = {}
        self._latest_ts: dict[str, float] = {}
        self._recording_files: dict[str, str] = {}
        self._recording_frames: dict[str, int] = {}
        self._writers: dict[str, Any] = {}
        self._sizes: dict[str, tuple[int, int]] = {}
        self._error = ""

        self._rclpy_started = False
        self._node = None
        self._spin_thread: threading.Thread | None = None

    def start(self) -> None:
        if not self.enabled:
            self._error = "CAMERA_ROS2_ENABLED=0"
            return
        if rclpy is None or QoSPresetProfiles is None or cv2 is None or np is None:
            self._error = "缺少 ROS2/cv2 依赖（需要 rclpy、sensor_msgs、opencv-python、numpy）"
            return

        try:
            self.record_dir.mkdir(parents=True, exist_ok=True)
            rclpy.init(args=None)
            self._rclpy_started = True

            node = rclpy.create_node("team66_camera_bridge")
            qos = QoSPresetProfiles.SENSOR_DATA.value

            for camera_name, topic in self.topics.items():
                cam = camera_name
                node.create_subscription(
                    CompressedImage,
                    topic,
                    lambda msg, _cam=cam: self._on_image(_cam, msg),
                    qos,
                )

            self._node = node
            self._spin_thread = threading.Thread(target=self._spin_forever, name="ros2-camera-bridge", daemon=True)
            self._spin_thread.start()
            self._error = ""
        except Exception as exc:
            self._error = f"ROS2 相机桥启动失败: {exc}"
            self.stop()

    def _spin_forever(self) -> None:
        if self._node is None or rclpy is None:
            return
        try:
            rclpy.spin(self._node)
        except Exception as exc:
            with self._lock:
                self._error = f"ROS2 spin 异常: {exc}"

    def _on_image(self, camera_name: str, msg: Any) -> None:
        data = bytes(msg.data)
        now_ts = time.time()
        with self._lock:
            self._latest_jpeg[camera_name] = data
            self._latest_ts[camera_name] = now_ts
        self._record_frame(camera_name, data)

    def _record_frame(self, camera_name: str, jpeg: bytes) -> None:
        if cv2 is None or np is None:
            return

        frame = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
        if frame is None:
            return

        h, w = frame.shape[:2]
        writer = self._writers.get(camera_name)

        if writer is None:
            ts = time.strftime("%Y%m%d%H%M%S")
            file_path = self.record_dir / f"camera_{camera_name}_{ts}.avi"
            fourcc = cv2.VideoWriter_fourcc(*"MJPG")
            writer = cv2.VideoWriter(str(file_path), fourcc, self.record_fps, (w, h))
            if not writer.isOpened():
                self._error = f"视频写入器打开失败: {file_path}"
                return
            self._writers[camera_name] = writer
            self._sizes[camera_name] = (w, h)
            self._recording_files[camera_name] = str(file_path)
            self._recording_frames[camera_name] = 0

        expected = self._sizes.get(camera_name, (w, h))
        if (w, h) != expected:
            frame = cv2.resize(frame, expected)

        writer.write(frame)
        self._recording_frames[camera_name] = int(self._recording_frames.get(camera_name, 0)) + 1

    def get_latest_frame(self, camera_name: str) -> bytes | None:
        with self._lock:
            return self._latest_jpeg.get(camera_name)

    def camera_status(self, camera_name: str) -> dict[str, Any]:
        with self._lock:
            latest = self._latest_ts.get(camera_name)
            return {
                "topic": self.topics.get(camera_name, ""),
                "has_frame": camera_name in self._latest_jpeg,
                "last_frame_ts": latest,
                "last_frame_iso": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(latest)) if latest else "",
                "recording_file": self._recording_files.get(camera_name, ""),
                "recording_frames": int(self._recording_frames.get(camera_name, 0)),
            }

    def save_snapshots(self, output_dir: Path, *, prefix: str = "SESSION-END") -> dict[str, str]:
        output_dir.mkdir(parents=True, exist_ok=True)
        saved: dict[str, str] = {}
        with self._lock:
            for camera_name, jpeg in self._latest_jpeg.items():
                if not jpeg:
                    continue
                ts = time.strftime("%Y%m%d_%H%M%S")
                file_path = output_dir / f"{prefix}_{camera_name}_{ts}.jpg"
                file_path.write_bytes(jpeg)
                saved[camera_name] = str(file_path)
        return saved

    def health(self) -> dict[str, Any]:
        with self._lock:
            return {
                "enabled": self.enabled,
                "running": bool(self._spin_thread and self._spin_thread.is_alive()),
                "topics": dict(self.topics),
                "record_dir": str(self.record_dir),
                "error": self._error,
            }

    def stop(self) -> None:
        for writer in self._writers.values():
            try:
                writer.release()
            except Exception:
                pass
        self._writers.clear()

        if self._node is not None:
            try:
                self._node.destroy_node()
            except Exception:
                pass
            self._node = None

        if self._rclpy_started and rclpy is not None:
            try:
                rclpy.shutdown()
            except Exception:
                pass
        self._rclpy_started = False


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
        self.camera_ros2_enabled = _as_bool(os.getenv("CAMERA_ROS2_ENABLED", "1"), True)
        self.camera_topics = {
            "head": os.getenv("HEAD_CAMERA_TOPIC", "/camera/head/image_raw/compressed"),
            "left": os.getenv("LEFT_CAMERA_TOPIC", "/camera/left/image_raw/compressed"),
            "right": os.getenv("RIGHT_CAMERA_TOPIC", "/camera/right/image_raw/compressed"),
        }
        self.camera_record_fps = _parse_float(os.getenv("CAMERA_RECORD_FPS", "12"), 12.0)
        self.camera_record_dir = Path(
            os.getenv(
                "CAMERA_RECORD_DIR",
                str(Path(__file__).resolve().parents[2] / "output" / "camera"),
            )
        )
        self.reflow_media_dir = Path(
            os.getenv(
                "REFLOW_MEDIA_DIR",
                str(Path(__file__).resolve().parents[2] / "output" / "reflow_buffer"),
            )
        )
        self._ros2_camera_bridge = _Ros2CameraBridge(
            topics=self.camera_topics,
            record_dir=self.camera_record_dir,
            record_fps=self.camera_record_fps,
            enabled=self.camera_ros2_enabled,
        )
        self._ros2_camera_bridge.start()

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

        # 末端精调安全参数（标准档默认值，可通过环境变量覆盖）
        self.eef_adjust_enabled = _as_bool(os.getenv("ROBOT_EEF_ADJUST_ENABLED", "1"), True)
        self.eef_adjust_default_send_time_s = max(
            0.1,
            _parse_float(os.getenv("ROBOT_EEF_ADJUST_DEFAULT_SEND_TIME_S", "0.5"), 0.5),
        )
        self.eef_adjust_send_time_min_s = max(
            0.05,
            _parse_float(os.getenv("ROBOT_EEF_ADJUST_SEND_TIME_MIN_S", "0.15"), 0.15),
        )
        self.eef_adjust_send_time_max_s = max(
            self.eef_adjust_send_time_min_s,
            _parse_float(os.getenv("ROBOT_EEF_ADJUST_SEND_TIME_MAX_S", "2.0"), 2.0),
        )

        self.eef_adjust_max_delta_pos_m = max(
            0.001,
            _parse_float(os.getenv("ROBOT_EEF_ADJUST_MAX_DELTA_POS_M", "0.06"), 0.06),
        )
        self.eef_adjust_max_delta_att_rad = max(
            0.01,
            _parse_float(os.getenv("ROBOT_EEF_ADJUST_MAX_DELTA_ATT_RAD", "0.35"), 0.35),
        )

        self.eef_workspace_x_min = _parse_float(os.getenv("ROBOT_EEF_X_MIN", "0.22"), 0.22)
        self.eef_workspace_x_max = _parse_float(os.getenv("ROBOT_EEF_X_MAX", "0.65"), 0.65)
        self.eef_workspace_z_min = _parse_float(os.getenv("ROBOT_EEF_Z_MIN", "0.52"), 0.52)
        self.eef_workspace_z_max = _parse_float(os.getenv("ROBOT_EEF_Z_MAX", "0.95"), 0.95)

        self.eef_workspace_left_y_min = _parse_float(os.getenv("ROBOT_EEF_LEFT_Y_MIN", "0.12"), 0.12)
        self.eef_workspace_left_y_max = _parse_float(os.getenv("ROBOT_EEF_LEFT_Y_MAX", "0.45"), 0.45)
        self.eef_workspace_right_y_min = _parse_float(os.getenv("ROBOT_EEF_RIGHT_Y_MIN", "-0.45"), -0.45)
        self.eef_workspace_right_y_max = _parse_float(os.getenv("ROBOT_EEF_RIGHT_Y_MAX", "-0.12"), -0.12)

        self.eef_workspace_roll_min = _parse_float(os.getenv("ROBOT_EEF_ROLL_MIN", "-1.57"), -1.57)
        self.eef_workspace_roll_max = _parse_float(os.getenv("ROBOT_EEF_ROLL_MAX", "1.57"), 1.57)
        self.eef_workspace_pitch_min = _parse_float(os.getenv("ROBOT_EEF_PITCH_MIN", "-1.57"), -1.57)
        self.eef_workspace_pitch_max = _parse_float(os.getenv("ROBOT_EEF_PITCH_MAX", "1.57"), 1.57)
        self.eef_workspace_yaw_min = _parse_float(os.getenv("ROBOT_EEF_YAW_MIN", "-1.57"), -1.57)
        self.eef_workspace_yaw_max = _parse_float(os.getenv("ROBOT_EEF_YAW_MAX", "1.57"), 1.57)

        self.eef_feedback_pos_tol_m = max(
            0.002,
            _parse_float(os.getenv("ROBOT_EEF_FEEDBACK_POS_TOL_M", "0.03"), 0.03),
        )
        self.eef_feedback_att_tol_rad = max(
            0.01,
            _parse_float(os.getenv("ROBOT_EEF_FEEDBACK_ATT_TOL_RAD", "0.25"), 0.25),
        )

        # 阻力保护：基于关节力矩反馈 tau 的自动急停（行进/前伸）
        self.resist_guard_enabled = _as_bool(os.getenv("ROBOT_RESIST_GUARD_ENABLED", "1"), True)
        self.resist_guard_move_enabled = _as_bool(os.getenv("ROBOT_RESIST_GUARD_MOVE_ENABLED", "1"), True)
        self.resist_guard_arm_extend_enabled = _as_bool(os.getenv("ROBOT_RESIST_GUARD_ARM_EXTEND_ENABLED", "1"), True)
        self.resist_guard_tau_threshold_move_nm = max(
            1.0,
            _parse_float(os.getenv("ROBOT_RESIST_TAU_THRESHOLD_MOVE_NM", "26.0"), 26.0),
        )
        self.resist_guard_tau_threshold_arm_nm = max(
            1.0,
            _parse_float(os.getenv("ROBOT_RESIST_TAU_THRESHOLD_ARM_NM", "22.0"), 22.0),
        )
        self.resist_guard_consecutive_hits = max(
            1,
            int(_parse_float(os.getenv("ROBOT_RESIST_CONSECUTIVE_HITS", "3"), 3)),
        )
        self.resist_guard_sample_interval_s = max(
            0.01,
            _parse_float(os.getenv("ROBOT_RESIST_SAMPLE_INTERVAL_S", "0.03"), 0.03),
        )

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
                "eef_adjust": {
                    "enabled": self.eef_adjust_enabled,
                    "default_send_time_s": self.eef_adjust_default_send_time_s,
                    "send_time_range_s": [self.eef_adjust_send_time_min_s, self.eef_adjust_send_time_max_s],
                    "max_delta_pos_m": self.eef_adjust_max_delta_pos_m,
                    "max_delta_att_rad": self.eef_adjust_max_delta_att_rad,
                    "workspace": {
                        "x": [self.eef_workspace_x_min, self.eef_workspace_x_max],
                        "z": [self.eef_workspace_z_min, self.eef_workspace_z_max],
                        "left_y": [self.eef_workspace_left_y_min, self.eef_workspace_left_y_max],
                        "right_y": [self.eef_workspace_right_y_min, self.eef_workspace_right_y_max],
                        "roll": [self.eef_workspace_roll_min, self.eef_workspace_roll_max],
                        "pitch": [self.eef_workspace_pitch_min, self.eef_workspace_pitch_max],
                        "yaw": [self.eef_workspace_yaw_min, self.eef_workspace_yaw_max],
                    },
                    "feedback_tol": {
                        "pos_m": self.eef_feedback_pos_tol_m,
                        "att_rad": self.eef_feedback_att_tol_rad,
                    },
                },
                "resistance_guard": {
                    "enabled": self.resist_guard_enabled,
                    "move_enabled": self.resist_guard_move_enabled,
                    "arm_extend_enabled": self.resist_guard_arm_extend_enabled,
                    "tau_threshold_move_nm": self.resist_guard_tau_threshold_move_nm,
                    "tau_threshold_arm_nm": self.resist_guard_tau_threshold_arm_nm,
                    "consecutive_hits": self.resist_guard_consecutive_hits,
                    "sample_interval_s": self.resist_guard_sample_interval_s,
                },
            },
            "audio": {
                "volume_percent": self.volume_percent,
            },
            "config_root": str(self.config_root),
            "camera": {
                "head": self._ros2_camera_bridge.camera_status("head"),
                "left": self._ros2_camera_bridge.camera_status("left"),
                "right": self._ros2_camera_bridge.camera_status("right"),
                "ros2": self._ros2_camera_bridge.health(),
                "record_dir": str(self.camera_record_dir),
                "reflow_media_dir": str(self.reflow_media_dir),
                "http_fallback": {
                    "head": bool(self.head_camera_url),
                    "left": bool(self.left_camera_url),
                    "right": bool(self.right_camera_url),
                },
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

    def _sample_tau_snapshot(self, timeout_s: float = 0.05) -> dict[str, Any] | None:
        try:
            self._sdk.fetch_robot_state(timeout=float(timeout_s))
        except Exception:
            return None

        sens = self._sdk.sens
        tau_left = [float(x) for x in sens.tau[0, :7]]
        tau_right = [float(x) for x in sens.tau[1, :7]]
        abs_left = [abs(x) for x in tau_left]
        abs_right = [abs(x) for x in tau_right]
        return {
            "tau_left": tau_left,
            "tau_right": tau_right,
            "max_abs_left": max(abs_left) if abs_left else 0.0,
            "max_abs_right": max(abs_right) if abs_right else 0.0,
            "max_abs_both": max(max(abs_left) if abs_left else 0.0, max(abs_right) if abs_right else 0.0),
        }

    def _is_tau_over_threshold(self, snapshot: dict[str, Any], *, threshold_nm: float, arm: str | None = None) -> bool:
        if arm == "left":
            return float(snapshot.get("max_abs_left", 0.0)) >= float(threshold_nm)
        if arm == "right":
            return float(snapshot.get("max_abs_right", 0.0)) >= float(threshold_nm)
        return float(snapshot.get("max_abs_both", 0.0)) >= float(threshold_nm)

    def _trigger_resistance_estop(self, *, context: str, snapshot: dict[str, Any], threshold_nm: float) -> dict[str, Any]:
        # 1) pause 触发全局发送中断；2) 立即把底盘速度置零并强制发一帧；3) 解除 pause 以允许后续人工恢复控制。
        pause_err = ""
        stop_err = ""
        resume_err = ""
        try:
            self._sdk.pause_mani_send(fetch_state=False)
        except Exception as exc:
            pause_err = str(exc)
        try:
            self._sdk.set_base_vel(0.0, 0.0)
            self._sdk.send(force=True)
        except Exception as exc:
            stop_err = str(exc)
        try:
            self._sdk.resume_mani_send(replay_s=0.0)
        except Exception as exc:
            resume_err = str(exc)

        diag = {
            "triggered": True,
            "context": context,
            "threshold_nm": float(threshold_nm),
            "tau_snapshot": snapshot,
            "pause_error": pause_err,
            "stop_error": stop_err,
            "resume_error": resume_err,
            "ts": round(time.time(), 3),
        }
        self._last_motion_diag = {"resistance_estop": diag}
        return diag

    @staticmethod
    def _max_pose_error(actual: list[float], target: list[float]) -> float:
        return max(abs(float(a) - float(b)) for a, b in zip(actual[:6], target[:6]))

    @staticmethod
    def _clamp(v: float, mn: float, mx: float) -> float:
        return max(mn, min(mx, float(v)))

    def _clamp_send_time_s(self, send_time_s: float | None) -> tuple[float, bool]:
        requested = self.eef_adjust_default_send_time_s if send_time_s is None else float(send_time_s)
        clamped = self._clamp(requested, self.eef_adjust_send_time_min_s, self.eef_adjust_send_time_max_s)
        return clamped, abs(clamped - requested) > 1e-9

    def _clamp_eef_delta(self, delta_pose: list[float]) -> tuple[list[float], dict[str, bool]]:
        out = list(delta_pose[:6])
        flags = {
            "delta_pos_limited": False,
            "delta_att_limited": False,
        }
        for i in (0, 1, 2):
            clamped = self._clamp(out[i], -self.eef_adjust_max_delta_pos_m, self.eef_adjust_max_delta_pos_m)
            if abs(clamped - out[i]) > 1e-9:
                flags["delta_pos_limited"] = True
                out[i] = clamped
        for i in (3, 4, 5):
            clamped = self._clamp(out[i], -self.eef_adjust_max_delta_att_rad, self.eef_adjust_max_delta_att_rad)
            if abs(clamped - out[i]) > 1e-9:
                flags["delta_att_limited"] = True
                out[i] = clamped
        return out, flags

    def _clamp_eef_target(self, arm: str, target_pose: list[float]) -> tuple[list[float], dict[str, bool]]:
        out = list(target_pose[:6])
        flags = {
            "workspace_limited": False,
        }

        x = self._clamp(out[0], self.eef_workspace_x_min, self.eef_workspace_x_max)
        z = self._clamp(out[2], self.eef_workspace_z_min, self.eef_workspace_z_max)
        y_min = self.eef_workspace_left_y_min if arm == "left" else self.eef_workspace_right_y_min
        y_max = self.eef_workspace_left_y_max if arm == "left" else self.eef_workspace_right_y_max
        y = self._clamp(out[1], y_min, y_max)

        roll = self._clamp(out[3], self.eef_workspace_roll_min, self.eef_workspace_roll_max)
        pitch = self._clamp(out[4], self.eef_workspace_pitch_min, self.eef_workspace_pitch_max)
        yaw = self._clamp(out[5], self.eef_workspace_yaw_min, self.eef_workspace_yaw_max)

        clamped = [x, y, z, roll, pitch, yaw]
        for i in range(6):
            if abs(clamped[i] - out[i]) > 1e-9:
                flags["workspace_limited"] = True
                break
        return clamped, flags

    def _verify_single_arm_eef_feedback(self, arm: str, target_pose: list[float]) -> dict[str, Any]:
        deadline = time.time() + self.motion_feedback_timeout_s
        samples = 0
        pos_err = float("inf")
        att_err = float("inf")
        snapshot: dict[str, Any] | None = None

        while time.time() < deadline:
            snapshot = self._sample_motion_snapshot(timeout_s=0.05)
            if snapshot is None:
                time.sleep(0.03)
                continue
            samples += 1
            actual = snapshot["left_eef"] if arm == "left" else snapshot["right_eef"]
            pos_err = max(abs(float(actual[i]) - float(target_pose[i])) for i in (0, 1, 2))
            att_err = max(abs(float(actual[i]) - float(target_pose[i])) for i in (3, 4, 5))
            if pos_err <= self.eef_feedback_pos_tol_m and att_err <= self.eef_feedback_att_tol_rad:
                break
            time.sleep(0.03)

        ok = pos_err <= self.eef_feedback_pos_tol_m and att_err <= self.eef_feedback_att_tol_rad
        diag = {
            "verified": True,
            "ok": ok,
            "arm": arm,
            "samples": samples,
            "target": [round(float(x), 4) for x in target_pose[:6]],
            "actual": None,
            "pos_error": None if pos_err == float("inf") else round(float(pos_err), 4),
            "att_error": None if att_err == float("inf") else round(float(att_err), 4),
            "pos_tol": self.eef_feedback_pos_tol_m,
            "att_tol": self.eef_feedback_att_tol_rad,
            "timeout_s": self.motion_feedback_timeout_s,
        }
        if snapshot is not None:
            actual_pose = snapshot["left_eef"] if arm == "left" else snapshot["right_eef"]
            diag["actual"] = [round(float(x), 4) for x in actual_pose[:6]]

        if not ok and self.motion_strict:
            raise RuntimeError(f"末端精调未检测到有效反馈，diag={diag}")
        return diag

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
            estop_diag = None
            hit_count = 0

            start = time.time()
            while time.time() - start < duration:
                if self.resist_guard_enabled and self.resist_guard_move_enabled:
                    tau_snapshot = self._sample_tau_snapshot(timeout_s=0.03)
                    if tau_snapshot is not None and self._is_tau_over_threshold(
                        tau_snapshot,
                        threshold_nm=self.resist_guard_tau_threshold_move_nm,
                        arm=None,
                    ):
                        hit_count += 1
                    else:
                        hit_count = 0

                    if hit_count >= self.resist_guard_consecutive_hits:
                        estop_diag = self._trigger_resistance_estop(
                            context="move_distance",
                            snapshot=tau_snapshot or {},
                            threshold_nm=self.resist_guard_tau_threshold_move_nm,
                        )
                        break

                self._sdk.set_base_vel(vx, 0.0)
                self._sdk.send()
                time.sleep(0.05)

            self._sdk.set_base_vel(0.0, 0.0)
            self._sdk.send()
            motion_diag = self._verify_motion_feedback(expect_vx=vx, expect_w=0.0)
            ok = bool(motion_diag.get("moved"))
            if estop_diag is not None:
                ok = False
            result = {
                "ok": True,
                "distance_m": dist,
                "speed_mps": speed,
                "duration_s": round(duration, 3),
                "motion_diag": motion_diag,
                "emergency_stopped": bool(estop_diag),
                "estop_diag": estop_diag,
                **self._build_action_result(
                    ok=ok,
                    reason="前进/后退过程中阻力过大已急停" if estop_diag is not None else "底盘未检测到有效线速度反馈",
                    suggestion=(
                        "检查前方是否有障碍、地面阻力或碰撞，再手动确认后重试。"
                        if estop_diag is not None
                        else "先检查机器人是否已上使能并处于自主模式，确认 CHASSIS_IP/CHASSIS_CMD_PORT 配置正确，必要时先调用 /robot/init 再重试。"
                    ),
                    diag={"motion_diag": motion_diag, "estop_diag": estop_diag},
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

            use_resist_guard = (
                self.resist_guard_enabled
                and self.resist_guard_arm_extend_enabled
                and preset_norm == "extend"
            )
            guard_stop = threading.Event()
            guard_state: dict[str, Any] = {
                "triggered": False,
                "estop_diag": None,
            }
            guard_thread: threading.Thread | None = None

            def _arm_extend_guard_loop() -> None:
                hits = 0
                while not guard_stop.is_set():
                    tau_snapshot = self._sample_tau_snapshot(timeout_s=0.03)
                    if tau_snapshot is not None and self._is_tau_over_threshold(
                        tau_snapshot,
                        threshold_nm=self.resist_guard_tau_threshold_arm_nm,
                        arm=arm_norm,
                    ):
                        hits += 1
                    else:
                        hits = 0

                    if hits >= self.resist_guard_consecutive_hits:
                        guard_state["triggered"] = True
                        guard_state["estop_diag"] = self._trigger_resistance_estop(
                            context=f"arm_preset:{arm_norm}:extend",
                            snapshot=tau_snapshot or {},
                            threshold_nm=self.resist_guard_tau_threshold_arm_nm,
                        )
                        guard_stop.set()
                        return

                    time.sleep(self.resist_guard_sample_interval_s)

            if use_resist_guard:
                guard_thread = threading.Thread(target=_arm_extend_guard_loop, name=f"resist-guard-{arm_norm}", daemon=True)
                guard_thread.start()

            try:
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
            finally:
                guard_stop.set()
                if guard_thread is not None:
                    guard_thread.join(timeout=0.5)

            if bool(guard_state.get("triggered")):
                motion_diag = {
                    "verified": False,
                    "ok": False,
                    "reason": "extend 过程中阻力过大触发急停",
                    "estop_diag": guard_state.get("estop_diag"),
                }
            else:
                motion_diag = self._verify_eef_feedback(
                    target_eef_l=eef_l,
                    target_eef_r=eef_r,
                    check_left=check_left,
                    check_right=check_right,
                )

            ok = bool(motion_diag.get("ok"))
            result = {
                "ok": True,
                "arm": arm_norm,
                "preset": preset_norm,
                "motion_diag": motion_diag,
                "emergency_stopped": bool(guard_state.get("triggered")),
                "estop_diag": guard_state.get("estop_diag"),
                **self._build_action_result(
                    ok=ok,
                    reason="前伸过程中阻力过大已急停" if bool(guard_state.get("triggered")) else "机械臂预置未检测到有效末端反馈",
                    suggestion=(
                        "检查前方障碍与关节受阻情况，确认安全后再执行前伸。"
                        if bool(guard_state.get("triggered"))
                        else "确认机器人已进入自主模式且对应臂无碰撞/限位；若末端目标变化较小，可适当放宽反馈阈值或延长超时。"
                    ),
                    diag={"motion_diag": motion_diag, "estop_diag": guard_state.get("estop_diag")},
                ),
            }
            self._remember_action_result(result)
            return result

    def arm_eef_adjust(
        self,
        *,
        arm: str,
        mode: str,
        pose: list[float],
        send_time_s: float | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            if not self.eef_adjust_enabled:
                raise RuntimeError("末端精调已禁用（ROBOT_EEF_ADJUST_ENABLED=0）")

            self._ensure_clients()
            self._prepare_motion_mode_if_needed()

            arm_norm = arm.strip().lower()
            if arm_norm not in {"left", "right"}:
                raise ValueError("arm 必须是 left 或 right")

            mode_norm = mode.strip().lower()
            if mode_norm not in {"delta", "absolute"}:
                raise ValueError("mode 必须是 delta 或 absolute")

            requested_pose = [float(x) for x in pose[:6]]
            send_time_applied, send_time_limited = self._clamp_send_time_s(send_time_s)

            eef_l, eef_r, waist_pos, waist_att, head_att, cap_l, cap_r = self._current_targets()
            current_pose = list(eef_l if arm_norm == "left" else eef_r)

            safety_flags: dict[str, bool] = {
                "send_time_limited": send_time_limited,
                "delta_pos_limited": False,
                "delta_att_limited": False,
                "workspace_limited": False,
            }

            if mode_norm == "delta":
                safe_delta, delta_flags = self._clamp_eef_delta(requested_pose)
                safety_flags.update(delta_flags)
                target_pose_raw = [float(current_pose[i]) + float(safe_delta[i]) for i in range(6)]
                applied_delta = safe_delta
            else:
                raw_delta = [float(requested_pose[i]) - float(current_pose[i]) for i in range(6)]
                safe_delta, delta_flags = self._clamp_eef_delta(raw_delta)
                safety_flags.update(delta_flags)
                target_pose_raw = [float(current_pose[i]) + float(safe_delta[i]) for i in range(6)]
                applied_delta = safe_delta

            target_pose, workspace_flags = self._clamp_eef_target(arm_norm, target_pose_raw)
            safety_flags.update(workspace_flags)
            applied_delta_final = [float(target_pose[i]) - float(current_pose[i]) for i in range(6)]

            if arm_norm == "left":
                eef_l = list(target_pose)
            else:
                eef_r = list(target_pose)

            self._sdk.send_eef_interpolation(
                target_eef_l=eef_l,
                target_eef_r=eef_r,
                send_time=send_time_applied,
                target_waist_pos=waist_pos,
                target_waist_att=waist_att,
                target_head_att=head_att,
                target_cap_l=cap_l,
                target_cap_r=cap_r,
                interp_start="ctrl",
            )
            motion_diag = self._verify_single_arm_eef_feedback(arm_norm, target_pose)
            ok = bool(motion_diag.get("ok"))
            result = {
                "ok": True,
                "arm": arm_norm,
                "mode": mode_norm,
                "send_time_s": round(float(send_time_applied), 4),
                "requested_pose": [round(float(x), 4) for x in requested_pose],
                "current_pose": [round(float(x), 4) for x in current_pose],
                "target_pose": [round(float(x), 4) for x in target_pose],
                "applied_delta": [round(float(x), 4) for x in applied_delta_final],
                "safety_flags": safety_flags,
                "motion_diag": motion_diag,
                **self._build_action_result(
                    ok=ok,
                    reason="末端精调未检测到有效反馈",
                    suggestion="检查动作空间限制、机械臂可达性与当前姿态，必要时减小步进并延长 send_time。",
                    diag={"motion_diag": motion_diag, "safety_flags": safety_flags},
                ),
            }
            self._remember_action_result(result)
            return result

    def get_arm_eef_current(self, arm: str) -> dict[str, Any]:
        with self._lock:
            self._ensure_clients()
            arm_norm = arm.strip().lower()
            if arm_norm not in {"left", "right"}:
                raise ValueError("arm 必须是 left 或 right")

            eef_l, eef_r, waist_pos, waist_att, head_att, cap_l, cap_r = self._current_targets()
            target_pose = eef_l if arm_norm == "left" else eef_r
            snapshot = self._sample_motion_snapshot(timeout_s=0.05)
            actual_pose = None
            if snapshot is not None:
                actual_pose = snapshot["left_eef"] if arm_norm == "left" else snapshot["right_eef"]

            return {
                "ok": True,
                "arm": arm_norm,
                "target_pose": [round(float(x), 4) for x in target_pose[:6]],
                "actual_pose": None if actual_pose is None else [round(float(x), 4) for x in actual_pose[:6]],
                "waist_target": [round(float(x), 4) for x in (waist_pos + waist_att)[:6]],
                "head_target": [round(float(x), 4) for x in head_att[:3]],
                "caps": {
                    "left": round(float(cap_l), 4),
                    "right": round(float(cap_r), 4),
                },
            }

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
        cam = camera_name.strip().lower()
        if cam not in {"head", "left", "right"}:
            raise RuntimeError(f"未知相机名: {camera_name}")

        frame = self._ros2_camera_bridge.get_latest_frame(cam)
        if frame:
            return frame, "image/jpeg"

        url = self._camera_url(camera_name)
        if not url:
            raise RuntimeError(f"相机 {camera_name} 尚未收到 ROS2 帧，且未配置 HTTP 回退 URL")

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

    def save_reflow_snapshots(self, session_id: str, *, prefix: str = "SESSION-END") -> dict[str, str]:
        session_dir = self.reflow_media_dir / session_id / "media"
        return self._ros2_camera_bridge.save_snapshots(session_dir, prefix=prefix)


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

        if path == "/robot/arm/eef/current":
            arm = (query.get("arm", ["left"])[0] or "left").strip().lower()
            data = robot_service.get_arm_eef_current(arm)
            return 200, {"success": True, "data": data}, "application/json; charset=utf-8"

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

        if path == "/robot/arm/eef_adjust":
            arm = str(body.get("arm", "left")).strip().lower()
            mode = str(body.get("mode", "delta")).strip().lower()
            pose = _parse_pose6(body.get("pose", [0, 0, 0, 0, 0, 0]), name="pose")
            send_time_s_raw = body.get("send_time_s")
            send_time_s = None if send_time_s_raw is None else float(send_time_s_raw)
            data = robot_service.arm_eef_adjust(
                arm=arm,
                mode=mode,
                pose=pose,
                send_time_s=send_time_s,
            )
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
