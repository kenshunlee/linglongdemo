# coding=utf-8
"""轨迹任务加载与回放（与 `linglong_h_sdk_cpp/src/sdk_trajectory.hpp` 中 `traj_replan` 对齐的 Python 实现）。"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Callable, Optional

import numpy as np

try:
	import yaml
except ImportError as e:  # pragma: no cover
	raise ImportError("sdk_trajectory 需要 PyYAML：pip install pyyaml") from e

from .sdk_base import ManiInterpStartSource
from .sdk_extend import LinglongHSdkClass, ObjectUdpReceiver, TrackedObject


class TrajectoryCsvFormat(IntEnum):
	kCanonicalBagLongTable = 0
	kEefInterpSegmentTable = 1


class PoseKind(IntEnum):
	kNone = 0
	kBagWide = 1
	kEefInterp = 2


class BagWideLayout:
	kRowFloats = 46
	kDur = 14
	kWaistQ0 = 15
	kHeadQ0 = 16
	kHeadQ1 = 17
	kEeWaist0 = 18
	kArmQL0 = 24
	kArmQR0 = 31
	kWaistQ4 = 38
	kBaseVel0 = 42


class EefInterpSegmentCsvLayout:
	# 每行必选列数三种；任选一种后再 **可选加 1 列** `post_segment_delay_s`
	kColsTargetsNoCapNoHead = 19  # +1 → 段末静置秒（见 EefInterpSegmentRow）
	kColsWithCapsNoHead = 21
	kFullCols = 24


@dataclass
class DualArmEefReplanYamlParams:
	recorded_ref_left6: list[float] = field(default_factory=list)
	recorded_ref_right6: list[float] = field(default_factory=list)
	robot_to_marker_calib_left6: list[float] = field(default_factory=list)
	robot_to_marker_calib_right6: list[float] = field(default_factory=list)

	def valid(self) -> bool:
		return (
			len(self.recorded_ref_left6) >= 6
			and len(self.recorded_ref_right6) >= 6
			and len(self.robot_to_marker_calib_left6) >= 6
			and len(self.robot_to_marker_calib_right6) >= 6
		)


@dataclass
class DualArmEefReplanSandwich:
	"""与 C++ ``DualArmEefReplanSandwich`` 一致：左右臂 4×4 sandwich 矩阵。"""

	S_left: np.ndarray = field(default_factory=lambda: np.eye(4, dtype=np.float64))
	S_right: np.ndarray = field(default_factory=lambda: np.eye(4, dtype=np.float64))


@dataclass
class BagJointTargetsSnapshot:
	"""与 C++ ``BagJointTargetsSnapshot`` 一致（宽行相邻帧端点）。"""

	qsl: np.ndarray = field(default_factory=lambda: np.zeros(7, dtype=np.float32))
	qel: np.ndarray = field(default_factory=lambda: np.zeros(7, dtype=np.float32))
	qsr: np.ndarray = field(default_factory=lambda: np.zeros(7, dtype=np.float32))
	qer: np.ndarray = field(default_factory=lambda: np.zeros(7, dtype=np.float32))
	wes: np.ndarray = field(default_factory=lambda: np.zeros(6, dtype=np.float32))
	wee: np.ndarray = field(default_factory=lambda: np.zeros(6, dtype=np.float32))
	els: np.ndarray = field(default_factory=lambda: np.zeros(6, dtype=np.float32))
	ele: np.ndarray = field(default_factory=lambda: np.zeros(6, dtype=np.float32))
	ers: np.ndarray = field(default_factory=lambda: np.zeros(6, dtype=np.float32))
	ere: np.ndarray = field(default_factory=lambda: np.zeros(6, dtype=np.float32))
	wq4s: np.ndarray = field(default_factory=lambda: np.zeros(4, dtype=np.float32))
	wq4e: np.ndarray = field(default_factory=lambda: np.zeros(4, dtype=np.float32))
	bvs: np.ndarray = field(default_factory=lambda: np.zeros(2, dtype=np.float32))
	bve: np.ndarray = field(default_factory=lambda: np.zeros(2, dtype=np.float32))
	hqs: np.ndarray = field(default_factory=lambda: np.zeros(2, dtype=np.float32))
	hqe: np.ndarray = field(default_factory=lambda: np.zeros(2, dtype=np.float32))
	cap_lr_s: np.ndarray = field(default_factory=lambda: np.zeros(2, dtype=np.float32))
	cap_lr_e: np.ndarray = field(default_factory=lambda: np.zeros(2, dtype=np.float32))


@dataclass
class TrajectoryPlaybackConfig:
	csv_format: TrajectoryCsvFormat = TrajectoryCsvFormat.kCanonicalBagLongTable
	pose_bag_use_status_column: bool = False
	dual_arm_eef_geometry: DualArmEefReplanYamlParams = field(default_factory=DualArmEefReplanYamlParams)
	has_dual_arm_eef_geometry: bool = False


@dataclass
class EefInterpSegmentRow:
	send_time_s: float = 0.02
	ee_l: np.ndarray = field(default_factory=lambda: np.zeros(6, dtype=np.float32))
	ee_r: np.ndarray = field(default_factory=lambda: np.zeros(6, dtype=np.float32))
	ee_waist: np.ndarray = field(default_factory=lambda: np.zeros(6, dtype=np.float32))
	cap_l: float = 0.0
	cap_r: float = 0.0
	has_cap: bool = False
	head_att: np.ndarray = field(default_factory=lambda: np.zeros(3, dtype=np.float32))
	has_head_att: bool = False
	# 本段发送完成后、开始 CSV 下一行前的静置时长（CSV 秒）；回放实际 sleep = post_segment_delay_s / max(speed,1e-3)
	post_segment_delay_s: float = 0.0


@dataclass
class TrajectoryConfigAction:
	action_name: str = ""
	playback: TrajectoryPlaybackConfig = field(default_factory=TrajectoryPlaybackConfig)
	pose_kind: PoseKind = PoseKind.kNone
	bag_wide_rows: list[list[float]] = field(default_factory=list)
	bag_row_ts_ns: list[int] = field(default_factory=list)
	eef_interp_rows: list[EefInterpSegmentRow] = field(default_factory=list)
	com_left: bool = False
	com_right: bool = False
	speed: float = 1.0
	sleep: float = 0.0


@dataclass
class TrajectoryConfigTask:
	task_name: str = ""
	actions: list[TrajectoryConfigAction] = field(default_factory=list)


@dataclass
class TrajectoryPlaybackRunBanner:
	dry_run_only_load: bool = False
	robot_ip: str = ""
	task_name: str = ""
	config_root_absolute_for_log: str = ""
	run_robot_enable_up: bool = False
	run_robot_autonomous_mode: bool = False
	run_reset_to_init_before_playback: bool = False
	run_reset_to_init_after_playback: bool = False
	run_robot_shutdown_sequence: bool = False
	# reset_to_init 与轨迹段 send_eef 的插值起点可分别配置（与 C++ TrajectoryPlaybackRunBanner 一致）
	interp_start_reset: ManiInterpStartSource = ManiInterpStartSource.kStatus
	interp_start_playback: ManiInterpStartSource = ManiInterpStartSource.kCtrl
	object_udp_listen_port: int = 0


# --- 几何（与 C++ Eigen 版一致：R = Rz(yaw) Ry(pitch) Rx(roll)）---


def homogeneous_from_xyz_rpy6(xyz_rpy6: np.ndarray | list[float]) -> np.ndarray:
	"""4×4 齐次矩阵；与 C++ ``homogeneous_from_xyz_rpy6`` 一致。"""
	p = np.asarray(xyz_rpy6, dtype=np.float64).reshape(-1)
	x, y, z, roll, pitch, yaw = [float(p[i]) for i in range(6)]
	Rz = np.array(
		[[np.cos(yaw), -np.sin(yaw), 0.0], [np.sin(yaw), np.cos(yaw), 0.0], [0.0, 0.0, 1.0]],
		dtype=np.float64,
	)
	Ry = np.array(
		[[np.cos(pitch), 0.0, np.sin(pitch)], [0.0, 1.0, 0.0], [-np.sin(pitch), 0.0, np.cos(pitch)]],
		dtype=np.float64,
	)
	Rx = np.array(
		[[1.0, 0.0, 0.0], [0.0, np.cos(roll), -np.sin(roll)], [0.0, np.sin(roll), np.cos(roll)]],
		dtype=np.float64,
	)
	R = Rz @ Ry @ Rx
	T = np.eye(4, dtype=np.float64)
	T[:3, :3] = R
	T[0, 3], T[1, 3], T[2, 3] = x, y, z
	return T


def _rpy_from_rotation_matrix(R: np.ndarray) -> tuple[float, float, float]:
	R = np.asarray(R, dtype=np.float64)
	pitch = float(np.arcsin(np.clip(-R[2, 0], -1.0, 1.0)))
	if abs(R[2, 0]) < 0.999999:
		roll = float(np.arctan2(R[2, 1], R[2, 2]))
		yaw = float(np.arctan2(R[1, 0], R[0, 0]))
	else:
		roll = 0.0
		yaw = float(np.arctan2(-R[0, 1], R[1, 1]))
	return roll, pitch, yaw


def xyz_rpy6_from_homogeneous(T: np.ndarray) -> list[float]:
	"""与 C++ ``xyz_rpy6_vec_from_homogeneous`` 一致，返回长度 6 的 ``list``。"""
	T = np.asarray(T, dtype=np.float64)
	R = T[:3, :3]
	roll, pitch, yaw = _rpy_from_rotation_matrix(R)
	return [float(T[0, 3]), float(T[1, 3]), float(T[2, 3]), roll, pitch, yaw]


def _xyz_rpy6_from_homogeneous_vec(T: np.ndarray) -> np.ndarray:
	T = np.asarray(T, dtype=np.float64)
	R = T[:3, :3]
	roll, pitch, yaw = _rpy_from_rotation_matrix(R)
	return np.array(
		[float(T[0, 3]), float(T[1, 3]), float(T[2, 3]), roll, pitch, yaw],
		dtype=np.float32,
	)


def conjugate_delta_by_calib(T_calib_marker: np.ndarray, delta_marker_frame: np.ndarray) -> np.ndarray:
	return T_calib_marker @ delta_marker_frame @ np.linalg.inv(T_calib_marker)


def homogeneous_from_xyz_rpy6_vec(v: list[float] | np.ndarray, offset: int = 0) -> np.ndarray:
	a = np.asarray(v, dtype=np.float64).reshape(-1)
	if a.size < offset + 6:
		return np.eye(4, dtype=np.float64)
	return homogeneous_from_xyz_rpy6(a[offset : offset + 6])


def apply_similarity_to_eef6(sandwiched_S: np.ndarray, eef_xyz_rpy6: list[float] | np.ndarray) -> list[float]:
	v = np.asarray(eef_xyz_rpy6, dtype=np.float64).reshape(-1)
	if v.size < 6:
		return []
	T_pose = homogeneous_from_xyz_rpy6(v[:6])
	T_out = np.asarray(sandwiched_S, dtype=np.float64) @ T_pose
	out6 = _xyz_rpy6_from_homogeneous_vec(T_out)
	return [float(x) for x in out6]


def apply_similarity_to_arm_row7(sandwiched_S: np.ndarray, arm_row7: np.ndarray | list[float]) -> np.ndarray:
	row = np.asarray(arm_row7, dtype=np.float32).reshape(-1)
	if row.size < 7:
		return row
	mapped = apply_similarity_to_eef6(sandwiched_S, row[:6])
	if len(mapped) >= 6:
		out = np.zeros(7, dtype=np.float32)
		out[:6] = np.asarray(mapped[:6], dtype=np.float32)
		out[6] = row[6]
		return out
	return row.copy()


def apply_similarity_to_arm_trajectory_rows(
	sandwiched_S: np.ndarray, arm_rows7: list[list[float]]
) -> list[list[float]]:
	"""与 C++ ``apply_similarity_to_arm_trajectory_rows`` 一致。"""
	out: list[list[float]] = []
	for row in arm_rows7:
		if len(row) < 6:
			continue
		slice6 = [float(row[j]) for j in range(6)]
		mapped = apply_similarity_to_eef6(sandwiched_S, slice6)
		if len(mapped) != 6:
			continue
		one = list(mapped)
		if len(row) > 6:
			one.extend(float(x) for x in row[6:])
		out.append(one)
	return out


def compute_sandwiched_replan_for_arm(
	T_waist_recording: np.ndarray,
	recorded_marker_ref6: list[float],
	current_target_ref6: list[float],
	yaml_calib_old_robot_to_marker6: list[float],
) -> np.ndarray:
	if (
		len(recorded_marker_ref6) < 6
		or len(current_target_ref6) < 6
		or len(yaml_calib_old_robot_to_marker6) < 6
	):
		return np.eye(4, dtype=np.float64)
	T_w = np.asarray(T_waist_recording, dtype=np.float64)
	T_cal = homogeneous_from_xyz_rpy6_vec(yaml_calib_old_robot_to_marker6, 0)
	T_rec = homogeneous_from_xyz_rpy6_vec(recorded_marker_ref6, 0)
	T_cur = homogeneous_from_xyz_rpy6_vec(current_target_ref6, 0)
	T_w_inv = np.linalg.inv(T_w)
	T_cal_in_w = T_w_inv @ T_cal
	T_rec_n = T_w_inv @ T_rec
	T_cur_n = T_w_inv @ T_cur
	delta = np.linalg.inv(T_rec_n) @ T_cur_n
	return conjugate_delta_by_calib(T_cal_in_w, delta)


def compute_dual_arm_eef_replan_sandwiches(
	T_waist_recording: np.ndarray,
	yaml_params: DualArmEefReplanYamlParams,
	live_target_left6: list[float],
	live_target_right6: list[float],
	out: DualArmEefReplanSandwich,
) -> tuple[bool, str]:
	"""与 C++ ``compute_dual_arm_eef_replan_sandwiches`` 一致：填充 ``out``，返回 ``(ok, err_msg)``。"""
	if not yaml_params.valid():
		return False, "DualArmEefReplanYamlParams 未通过 valid() 检查"
	if len(live_target_left6) < 6 or len(live_target_right6) < 6:
		return False, "live_target_left6 / live_target_right6 长度须至少为 6"
	out.S_left = compute_sandwiched_replan_for_arm(
		T_waist_recording,
		yaml_params.recorded_ref_left6,
		live_target_left6,
		yaml_params.robot_to_marker_calib_left6,
	)
	out.S_right = compute_sandwiched_replan_for_arm(
		T_waist_recording,
		yaml_params.recorded_ref_right6,
		live_target_right6,
		yaml_params.robot_to_marker_calib_right6,
	)
	return True, ""


def fill_joint_targets_for_bag_row_pair(
	target_pose: list[list[float]], gi: int
) -> tuple[bool, Optional[BagJointTargetsSnapshot]]:
	"""与 C++ ``fill_joint_targets_for_bag_row_pair`` 一致。"""
	BL = BagWideLayout
	if gi >= len(target_pose):
		return False, None
	rw = target_pose[gi]
	if len(rw) < BL.kRowFloats:
		return False, None
	out = BagJointTargetsSnapshot()
	for j in range(7):
		out.qel[j] = float(rw[BL.kArmQL0 + j])
		out.qer[j] = float(rw[BL.kArmQR0 + j])
	out.hqe[0] = float(rw[BL.kHeadQ0])
	out.hqe[1] = float(rw[BL.kHeadQ1])
	out.cap_lr_e[0] = float(rw[6])
	out.cap_lr_e[1] = float(rw[13])
	for j in range(6):
		out.ele[j] = float(rw[j])
		out.ere[j] = float(rw[7 + j])
	if gi > 0 and len(target_pose[gi - 1]) >= BL.kRowFloats:
		r0 = target_pose[gi - 1]
		for j in range(7):
			out.qsl[j] = float(r0[BL.kArmQL0 + j])
			out.qsr[j] = float(r0[BL.kArmQR0 + j])
		for j in range(6):
			out.wes[j] = float(r0[BL.kEeWaist0 + j])
			out.wee[j] = float(rw[BL.kEeWaist0 + j])
			out.els[j] = float(r0[j])
			out.ers[j] = float(r0[7 + j])
		for j in range(4):
			out.wq4s[j] = float(r0[BL.kWaistQ4 + j])
			out.wq4e[j] = float(rw[BL.kWaistQ4 + j])
		out.bvs[0] = float(r0[BL.kBaseVel0])
		out.bvs[1] = float(r0[BL.kBaseVel0 + 1])
		out.bve[0] = float(rw[BL.kBaseVel0])
		out.bve[1] = float(rw[BL.kBaseVel0 + 1])
		out.hqs[0] = float(r0[BL.kHeadQ0])
		out.hqs[1] = float(r0[BL.kHeadQ1])
		out.cap_lr_s[0] = float(r0[6])
		out.cap_lr_s[1] = float(r0[13])
	else:
		out.qsl = np.copy(out.qel)
		out.qsr = np.copy(out.qer)
		for j in range(6):
			out.wes[j] = float(rw[BL.kEeWaist0 + j])
			out.wee[j] = float(rw[BL.kEeWaist0 + j])
			out.els[j] = out.ele[j]
			out.ers[j] = out.ere[j]
		for j in range(4):
			out.wq4s[j] = float(rw[BL.kWaistQ4 + j])
			out.wq4e[j] = float(rw[BL.kWaistQ4 + j])
		out.bvs[0] = float(rw[BL.kBaseVel0])
		out.bvs[1] = float(rw[BL.kBaseVel0 + 1])
		out.bve = np.copy(out.bvs)
		out.hqs = np.copy(out.hqe)
		out.cap_lr_s = np.copy(out.cap_lr_e)
	return True, out


def decode_bag_motion_window(
	target_pose: list[list[float]],
	processed_rows: int,
	batch_window: int,
	live_waist_q4: np.ndarray,
	live_head_q2: np.ndarray,
) -> tuple[
	bool,
	list[list[float]],
	list[list[float]],
	list[list[float]],
	list[list[float]],
]:
	"""与 C++ ``decode_bag_motion_window`` 一致。"""
	out_l: list[list[float]] = []
	out_r: list[list[float]] = []
	out_w: list[list[float]] = []
	out_h: list[list[float]] = []
	if batch_window <= 0:
		return False, [], [], [], []
	for k in range(batch_window):
		i = processed_rows + k
		dec = decode_bag_motion_one_row(target_pose, i, live_waist_q4, live_head_q2)
		if dec is None:
			return False, [], [], [], []
		L, R, Wq, Hq = dec
		out_l.append(L.astype(float).tolist())
		out_r.append(R.astype(float).tolist())
		out_w.append(Wq.astype(float).tolist())
		out_h.append(Hq.astype(float).tolist())
	return len(out_l) == batch_window, out_l, out_r, out_w, out_h


def decode_bag_motion_one_row(
	target_pose: list[list[float]],
	gi: int,
	live_waist_q4: np.ndarray,
	live_head_q2: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray] | None:
	if gi >= len(target_pose):
		return None
	row = target_pose[gi]
	if len(row) < 14:
		return None
	L = np.asarray(row[:7], dtype=np.float32)
	R = np.asarray(row[7:14], dtype=np.float32)
	BL = BagWideLayout
	need_waist4 = BL.kWaistQ4 + 4
	if len(row) >= need_waist4:
		Wq = np.asarray(row[BL.kWaistQ4 : BL.kWaistQ4 + 4], dtype=np.float32)
	else:
		Wq = np.asarray(live_waist_q4, dtype=np.float32).copy()
		if len(row) >= 18:
			Wq[0] = float(row[BL.kWaistQ0])
	Hq = np.zeros(2, dtype=np.float32)
	if len(row) >= 18:
		Hq[0] = float(row[BL.kHeadQ0])
		Hq[1] = float(row[BL.kHeadQ1])
	else:
		Hq[:] = np.asarray(live_head_q2, dtype=np.float32)
	return L, R, Wq, Hq


def fill_waist_pose6_for_replan(
	trajectory_is_bag_wide: bool,
	target_pose: list[list[float]],
	processed_rows: int,
	waist_q4: np.ndarray,
) -> np.ndarray:
	out = np.zeros(6, dtype=np.float32)
	BL = BagWideLayout
	if (
		trajectory_is_bag_wide
		and processed_rows < len(target_pose)
		and len(target_pose[processed_rows]) >= BL.kRowFloats
	):
		r0 = target_pose[processed_rows]
		for j in range(6):
			out[j] = float(r0[BL.kEeWaist0 + j])
		return out
	out[5] = float(waist_q4[0])
	return out


def _trim(s: str) -> str:
	return s.strip()


def _ascii_lower(s: str) -> str:
	return s.lower()


def _split_csv_line_tokens(line: str) -> list[str]:
	cols: list[str] = []
	start = 0
	while start < len(line):
		comma = line.find(",", start)
		if comma < 0:
			cols.append(_trim(line[start:]))
			break
		cols.append(_trim(line[start:comma]))
		start = comma + 1
	return cols


def line_is_canonical_bag_header_row(line: str) -> bool:
	cols = _split_csv_line_tokens(line)
	if len(cols) < 5:
		return False
	c0, c1, c2, c4 = (_ascii_lower(cols[0]), _ascii_lower(cols[1]), _ascii_lower(cols[2]), _ascii_lower(cols[4]))
	return c0 == "timestamp" and c1 == "name" and c2 == "index" and c4 == "exp"


def _bag_get(ch: dict[str, dict[int, float]], name: str, idx: int, default: float = 0.0) -> float:
	return float(ch.get(name, {}).get(idx, default))


def _bag_has_ee6(ch: dict[str, dict[int, float]], name: str) -> bool:
	it = ch.get(name)
	if not it:
		return False
	return all(i in it for i in range(6))


def _bag_has_range(ch: dict[str, dict[int, float]], name: str, n: int) -> bool:
	it = ch.get(name)
	if not it:
		return False
	return all(i in it for i in range(n))


def bag_cache_complete_for_wide_row(ch: dict[str, dict[int, float]]) -> bool:
	return (
		_bag_has_ee6(ch, "ee_pose_l")
		and _bag_has_ee6(ch, "ee_pose_r")
		and _bag_has_range(ch, "ee_pose_waist", 6)
		and _bag_has_range(ch, "arm_q_l", 7)
		and _bag_has_range(ch, "arm_q_r", 7)
		and _bag_has_range(ch, "waist_q", 4)
		and _bag_has_range(ch, "head_q", 2)
		and _bag_has_range(ch, "cap", 2)
		and _bag_has_range(ch, "base_vel", 2)
	)


def read_canonical_pose_bag_csv_to_wide_rows(
	csv_path: str,
	out_row_ts_ns: Optional[list[int]] = None,
	use_status_column: bool = False,
) -> list[list[float]]:
	if out_row_ts_ns is not None:
		out_row_ts_ns.clear()
	by_ts: dict[int, dict[str, dict[int, float]]] = {}
	with open(csv_path, newline="") as f:
		for line in f:
			line = line.rstrip("\r\n")
			if not line:
				continue
			cols = _split_csv_line_tokens(line)
			if len(cols) < 5:
				continue
			if line_is_canonical_bag_header_row(line):
				continue
			c0low = _ascii_lower(cols[0])
			if c0low in ("timestamp", "time", "ts"):
				continue
			try:
				ts = int(cols[0])
			except ValueError:
				continue
			name = cols[1]
			try:
				idx = int(cols[2])
				cellv = float(cols[3] if use_status_column else cols[4])
			except (ValueError, IndexError):
				continue
			by_ts.setdefault(ts, {}).setdefault(name, {})[idx] = cellv

	cache: dict[str, dict[int, float]] = {}
	wide_rows: list[list[float]] = []
	k_default_dur = 0.02
	BL = BagWideLayout
	for ts in sorted(by_ts.keys()):
		for name, idx_map in by_ts[ts].items():
			if name not in cache:
				cache[name] = {}
			for idx, val in idx_map.items():
				cache[name][idx] = val
		if not bag_cache_complete_for_wide_row(cache):
			continue
		ch = cache
		row = [0.0] * BL.kRowFloats
		for j in range(6):
			row[j] = _bag_get(ch, "ee_pose_l", j)
		row[6] = _bag_get(ch, "cap", 0)
		for j in range(6):
			row[7 + j] = _bag_get(ch, "ee_pose_r", j)
		row[13] = _bag_get(ch, "cap", 1)
		row[14] = k_default_dur
		row[15] = _bag_get(ch, "waist_q", 0)
		row[16] = _bag_get(ch, "head_q", 0)
		row[17] = _bag_get(ch, "head_q", 1)
		for j in range(6):
			row[18 + j] = _bag_get(ch, "ee_pose_waist", j)
		for j in range(7):
			row[24 + j] = _bag_get(ch, "arm_q_l", j)
			row[31 + j] = _bag_get(ch, "arm_q_r", j)
		for j in range(4):
			row[38 + j] = _bag_get(ch, "waist_q", j)
		row[42] = _bag_get(ch, "base_vel", 0)
		row[43] = _bag_get(ch, "base_vel", 1)
		wide_rows.append(row)
		if out_row_ts_ns is not None:
			out_row_ts_ns.append(ts)
		cache.clear()
	return wide_rows


def _line_tokens_all_floats(line: str) -> Optional[list[float]]:
	cols = _split_csv_line_tokens(line)
	if not cols:
		return None
	vals: list[float] = []
	for c in cols:
		if not c:
			return None
		try:
			vals.append(float(c))
		except ValueError:
			return None
	return vals


def read_eef_interp_segment_csv(csv_path: str, err_msg: Optional[list[str]] = None) -> list[EefInterpSegmentRow]:
	rows: list[EefInterpSegmentRow] = []
	L = EefInterpSegmentCsvLayout
	with open(csv_path) as f:
		for raw in f:
			line = _trim(raw.rstrip("\r\n"))
			if not line or line[0] == "#":
				continue
			vals = _line_tokens_all_floats(line)
			if vals is None:
				continue
			n = len(vals)
			base = None
			if n in (L.kColsTargetsNoCapNoHead, L.kColsTargetsNoCapNoHead + 1):
				base = L.kColsTargetsNoCapNoHead
			elif n in (L.kColsWithCapsNoHead, L.kColsWithCapsNoHead + 1):
				base = L.kColsWithCapsNoHead
			elif n in (L.kFullCols, L.kFullCols + 1):
				base = L.kFullCols
			else:
				if err_msg is not None:
					err_msg.append(
						f"eef_interp CSV 每行列数须为 19/20、21/22、24/25（末尾可选 post_segment_delay_s），当前行={n}"
					)
				return []
			r = EefInterpSegmentRow()
			r.send_time_s = float(vals[0])
			if r.send_time_s < 1e-9:
				r.send_time_s = 0.02
			r.ee_l = np.asarray(vals[1:7], dtype=np.float32)
			r.ee_r = np.asarray(vals[7:13], dtype=np.float32)
			r.ee_waist = np.asarray(vals[13:19], dtype=np.float32)
			if base >= L.kColsWithCapsNoHead:
				r.cap_l = float(vals[19])
				r.cap_r = float(vals[20])
				r.has_cap = True
			if base >= L.kFullCols:
				r.head_att = np.asarray(vals[21:24], dtype=np.float32)
				r.has_head_att = True
			if n > base:
				r.post_segment_delay_s = max(0.0, float(vals[base]))
			rows.append(r)
	return rows


def _yaml_bool_loose(n: Any) -> bool:
	if n is None or not isinstance(n, (bool, int, float, str)):
		return False
	if isinstance(n, bool):
		return n
	if isinstance(n, (int, float)):
		return bool(n)
	s = str(n).strip().lower()
	return s in ("true", "1", "yes", "on")


def _yaml_scalar_lower(n: Any) -> str:
	if n is None:
		return ""
	return str(n).strip().lower()


def _map_csv_format_string(s: str) -> TrajectoryCsvFormat:
	s = _ascii_lower(_trim(s))
	if not s:
		return TrajectoryCsvFormat.kCanonicalBagLongTable
	if s in ("eef_interp", "eef-interp", "eef_segments", "interp", "segment", "bezier"):
		return TrajectoryCsvFormat.kEefInterpSegmentTable
	return TrajectoryCsvFormat.kCanonicalBagLongTable


def find_older_node_for_eef_replan(root: Any) -> Any | None:
	if root is None:
		return None
	if isinstance(root, dict):
		if "older" in root:
			return root["older"]
		er = root.get("eef_replan")
		if isinstance(er, dict) and "older" in er:
			return er["older"]
		ar = root.get("arm_eef_replan")
		if isinstance(ar, dict) and "older" in ar:
			return ar["older"]
		return None
	if isinstance(root, list) and root and isinstance(root[0], dict) and "older" in root[0]:
		return root[0]["older"]
	return None


def parse_older_two_rows_to_dual_arm_params(older_node: Any) -> DualArmEefReplanYamlParams | None:
	if not isinstance(older_node, list) or len(older_node) < 2:
		return None

	def read_row12(row_idx: int) -> tuple[list[float], list[float]] | None:
		row = older_node[row_idx]
		if not isinstance(row, (list, tuple)) or len(row) < 12:
			return None
		left = [float(row[j]) for j in range(6)]
		right = [float(row[j]) for j in range(6, 12)]
		return left, right

	r0 = read_row12(0)
	r1 = read_row12(1)
	if r0 is None or r1 is None:
		return None
	out = DualArmEefReplanYamlParams()
	out.recorded_ref_left6, out.recorded_ref_right6 = r0
	out.robot_to_marker_calib_left6, out.robot_to_marker_calib_right6 = r1
	return out


def fill_trajectory_playback_config_from_yaml_maps(
	action_map: dict[str, Any],
	file_level_map: dict[str, Any] | None,
) -> TrajectoryPlaybackConfig:
	cfg = TrajectoryPlaybackConfig()
	csv_format_explicit = False
	if isinstance(action_map.get("csv_format"), str):
		cfg.csv_format = _map_csv_format_string(action_map["csv_format"])
		csv_format_explicit = True
	elif file_level_map and isinstance(file_level_map.get("csv_format"), str):
		cfg.csv_format = _map_csv_format_string(file_level_map["csv_format"])
		csv_format_explicit = True
	if not csv_format_explicit:
		bezier_hint = (
			_yaml_bool_loose(action_map.get("bezier"))
			or _yaml_bool_loose(action_map.get("arm_bezier"))
			or (
				isinstance(file_level_map, dict)
				and (_yaml_bool_loose(file_level_map.get("bezier")) or _yaml_bool_loose(file_level_map.get("arm_bezier")))
			)
		)
		if bezier_hint:
			cfg.csv_format = TrajectoryCsvFormat.kEefInterpSegmentTable
	if isinstance(action_map.get("pose_bag_value"), str):
		cfg.pose_bag_use_status_column = _yaml_scalar_lower(action_map["pose_bag_value"]) == "status"
	elif file_level_map and isinstance(file_level_map.get("pose_bag_value"), str):
		cfg.pose_bag_use_status_column = _yaml_scalar_lower(file_level_map["pose_bag_value"]) == "status"
	older = find_older_node_for_eef_replan(action_map)
	if older is None and isinstance(file_level_map, dict):
		older = find_older_node_for_eef_replan(file_level_map)
	if older is not None:
		geo = parse_older_two_rows_to_dual_arm_params(older)
		if geo is not None:
			cfg.dual_arm_eef_geometry = geo
			cfg.has_dual_arm_eef_geometry = True
	return cfg


def playback_style_task_node(root: Any) -> Any:
	if isinstance(root, list) and root and isinstance(root[0], dict):
		return root[0]
	return root


def load_trajectory_playback_yaml(yaml_path: str) -> tuple[bool, TrajectoryPlaybackConfig, str]:
	"""与 C++ ``load_trajectory_playback_yaml`` 一致：``(ok, out, err_msg)``（文件/YAML 失败时 ``ok=False``）。"""
	try:
		with open(yaml_path, encoding="utf-8") as f:
			root = yaml.safe_load(f)
	except OSError as e:
		return False, TrajectoryPlaybackConfig(), str(e)
	except yaml.YAMLError as e:
		return False, TrajectoryPlaybackConfig(), str(e)
	task = playback_style_task_node(root)
	file_map = root if isinstance(root, dict) else None
	cfg = fill_trajectory_playback_config_from_yaml_maps(task if isinstance(task, dict) else {}, file_map)
	return True, cfg, ""


def load_dual_arm_eef_replan_yaml(yaml_path: str) -> tuple[bool, DualArmEefReplanYamlParams, str]:
	"""与 C++ ``load_dual_arm_eef_replan_yaml`` 一致。"""
	ok, cfg, err = load_trajectory_playback_yaml(yaml_path)
	if not ok:
		return False, DualArmEefReplanYamlParams(), err
	if not cfg.has_dual_arm_eef_geometry or not cfg.dual_arm_eef_geometry.valid():
		return False, DualArmEefReplanYamlParams(), "YAML 中未解析到合法 older（至少 2 行×12 列）"
	return True, cfg.dual_arm_eef_geometry, ""


def discover_trajectory_task_names(config_root: str) -> tuple[bool, list[str], str]:
	names: list[str] = []
	try:
		if not os.path.isdir(config_root):
			return False, [], f"config_root 不是目录: {config_root}"
		for cat in sorted(os.listdir(config_root)):
			if not cat or cat.startswith("."):
				continue
			p = os.path.join(config_root, cat)
			if not os.path.isdir(p):
				continue
			if os.path.isfile(os.path.join(p, "yaml", "config.yaml")) or os.path.isfile(
				os.path.join(p, "yaml", "content.yaml")
			):
				names.append(cat)
		return True, sorted(names), ""
	except OSError as e:
		return False, [], str(e)


def _yaml_float_or_default(n: Any, default: float) -> float:
	if n is None:
		return default
	try:
		return float(n)
	except (TypeError, ValueError):
		return default


def load_trajectory_task_from_config_directory(
	config_root: str, task_name: str
) -> tuple[bool, TrajectoryConfigTask, str]:
	bundle = TrajectoryConfigTask(task_name=task_name)
	try:
		yaml_file = os.path.join(config_root, task_name, "yaml", "config.yaml")
		if not os.path.isfile(yaml_file):
			yaml_file = os.path.join(config_root, task_name, "yaml", "content.yaml")
		if not os.path.isfile(yaml_file):
			return False, bundle, f'未找到任务 "{task_name}" 的 yaml/config.yaml 或 yaml/content.yaml'
		with open(yaml_file, encoding="utf-8") as f:
			config = yaml.safe_load(f)
		if not isinstance(config, list):
			return False, bundle, f"任务 YAML 须为动作列表（Sequence），文件: {yaml_file}"
		for node in config:
			if not isinstance(node, dict):
				continue
			if "action" not in node:
				return False, bundle, "动作节点缺少 action 字段"
			act = TrajectoryConfigAction()
			act.action_name = str(node["action"])
			act.playback = fill_trajectory_playback_config_from_yaml_maps(node, None)
			act.com_left = _yaml_bool_loose(node.get("com_left"))
			act.com_right = _yaml_bool_loose(node.get("com_right"))
			act.speed = _yaml_float_or_default(node.get("speed"), 1.0)
			act.sleep = _yaml_float_or_default(node.get("sleep"), 0.0)
			csv_path = os.path.join(config_root, task_name, "action", f"{act.action_name}.csv")
			if not os.path.isfile(csv_path):
				return False, bundle, f"位姿 CSV 不存在: {csv_path}"
			if act.playback.csv_format == TrajectoryCsvFormat.kEefInterpSegmentTable:
				err: list[str] = []
				act.eef_interp_rows = read_eef_interp_segment_csv(csv_path, err)
				if not act.eef_interp_rows:
					msg = err[0] if err else "eef_interp CSV 为空或解析失败"
					return False, bundle, f"{msg}: {csv_path}"
				act.pose_kind = PoseKind.kEefInterp
			else:
				act.bag_wide_rows = read_canonical_pose_bag_csv_to_wide_rows(
					csv_path, act.bag_row_ts_ns, act.playback.pose_bag_use_status_column
				)
				if not act.bag_wide_rows:
					return False, bundle, f"长表 CSV 为空或解析失败: {csv_path}"
				act.pose_kind = PoseKind.kBagWide
			bundle.actions.append(act)
		if not bundle.actions:
			return False, bundle, f'任务 "{task_name}" 未解析到任何动作'
		return True, bundle, ""
	except yaml.YAMLError as e:
		return False, bundle, f"读取任务 YAML 失败: {e}"
	except OSError as e:
		return False, bundle, str(e)


def trajectory_task_needs_object_udp(task: TrajectoryConfigTask) -> bool:
	return any(a.com_left or a.com_right for a in task.actions)


def _trim_ws_traj(s: str) -> str:
	return _trim(s)


def _geo_calib_live_defaults_from_geo(geo: DualArmEefReplanYamlParams) -> tuple[list[float], list[float]]:
	live_l = [float(x) for x in geo.robot_to_marker_calib_left6[:6]]
	live_r = [float(x) for x in geo.robot_to_marker_calib_right6[:6]]
	while len(live_l) < 6:
		live_l.append(0.0)
	while len(live_r) < 6:
		live_r.append(0.0)
	return live_l, live_r


def copy_tracked_object_to_live_pose6_traj(t: TrackedObject) -> list[float]:
	return [
		float(t.base_pos[0]),
		float(t.base_pos[1]),
		float(t.base_pos[2]),
		float(t.base_att[0]),
		float(t.base_att[1]),
		float(t.base_att[2]),
	]


def resolve_live627_for_dual_arm_eef_replan(
	task: TrajectoryConfigTask,
	act: TrajectoryConfigAction,
	geo: DualArmEefReplanYamlParams,
	object_udp: ObjectUdpReceiver | None,
	log_err: Optional[Callable[[str], None]] = None,
) -> tuple[bool, list[float], list[float]]:
	"""无 com：返回 ``geo`` 中标定-derived 默认值；有 com：在 ``object_udp.get_tracked_objects()`` 快照里 **按约定查 key**

	快照里可同时存在多条物体；**接收器只做记账**，本函数这一步才根据 ``task.task_name``（及 ``-`` 分左右规则）选出用于 live 的条目。
	playback 侧在 **`trajectory_playback_action_*` 每个动作开始时**（若启用 eef 重规划）调用一次，整段 CSV 复用返回的 live，不在每段/每帧重复取快照。
	"""
	live_l, live_r = _geo_calib_live_defaults_from_geo(geo)
	if not act.com_left and not act.com_right:
		return True, live_l, live_r
	if object_udp is None:
		if log_err:
			log_err("[traj_replan] com_left/right requires ObjectUdp (non-null receiver)\n")
		return False, live_l, live_r
	objs = object_udp.get_tracked_objects()
	tn = _trim_ws_traj(task.task_name)
	if not tn:
		if log_err:
			log_err("[traj_replan] com requires non-empty task_name for object key lookup\n")
		return False, live_l, live_r
	dash = tn.find("-")
	key_l = _trim_ws_traj(tn[:dash]) if dash >= 0 else tn
	key_r = _trim_ws_traj(tn[dash + 1 :]) if dash >= 0 else tn
	if act.com_left and act.com_right and dash < 0:
		if key_l not in objs:
			if log_err:
				log_err(f'[traj_replan] ObjectUdp: missing object "{key_l}" (dual-arm same key)\n')
			return False, live_l, live_r
		v = copy_tracked_object_to_live_pose6_traj(objs[key_l])
		return True, v, list(v)
	if act.com_left:
		if not key_l:
			if log_err:
				log_err("[traj_replan] left com: empty object key after task_name parse\n")
			return False, live_l, live_r
		if key_l not in objs:
			if log_err:
				log_err(f'[traj_replan] ObjectUdp: missing left object "{key_l}"\n')
			return False, live_l, live_r
		live_l = copy_tracked_object_to_live_pose6_traj(objs[key_l])
	if act.com_right:
		if not key_r:
			if log_err:
				log_err("[traj_replan] right com: empty object key after task_name parse\n")
			return False, live_l, live_r
		if key_r not in objs:
			if log_err:
				log_err(f'[traj_replan] ObjectUdp: missing right object "{key_r}"\n')
			return False, live_l, live_r
		live_r = copy_tracked_object_to_live_pose6_traj(objs[key_r])
	return True, live_l, live_r


def millis_since(t_prev: float) -> float:
	"""与 C++ ``steady_clock`` 语义一致：``t_prev`` 须为 ``time.monotonic()`` 返回值。"""
	return (time.monotonic() - t_prev) * 1000.0


def print_trajectory_config_task_summary(task: TrajectoryConfigTask, log: Callable[[str], None] = print) -> None:
	log(f"[playback] 任务={task.task_name} 动作数={len(task.actions)}")
	for i, a in enumerate(task.actions):
		line = f"  动作[{i}] {a.action_name}"
		if a.pose_kind == PoseKind.kEefInterp:
			line += f" eef_interp 行数={len(a.eef_interp_rows)} eef_replan={'on' if a.playback.has_dual_arm_eef_geometry else 'off'}"
		elif a.pose_kind == PoseKind.kBagWide:
			line += f" 宽行帧数={len(a.bag_wide_rows)} eef_replan={'on' if a.playback.has_dual_arm_eef_geometry else 'off'}"
		line += f" speed={a.speed}"
		log(line)


def print_trajectory_playback_run_banner(banner: TrajectoryPlaybackRunBanner, log: Callable[[str], None] = print) -> None:
	def _src(s: ManiInterpStartSource) -> str:
		return "ctrl" if s == ManiInterpStartSource.kCtrl else "status"

	log(
		f"[playback] 当前设置: dry_run={banner.dry_run_only_load} robot_ip={banner.robot_ip} task={banner.task_name}\n"
		f"[playback] config_root={banner.config_root_absolute_for_log}\n"
		f"[playback] 流程: enable_up={banner.run_robot_enable_up} autonomous={banner.run_robot_autonomous_mode} "
		f"reset_before={banner.run_reset_to_init_before_playback} reset_after={banner.run_reset_to_init_after_playback} "
		f"shutdown={banner.run_robot_shutdown_sequence} interp_reset={_src(banner.interp_start_reset)} "
		f"interp_playback={_src(banner.interp_start_playback)} object_udp_port={banner.object_udp_listen_port}"
	)


def apply_bag_wide_row_to_ctrl(
	sdk: LinglongHSdkClass,
	r: list[float],
	replan_l7: np.ndarray | None,
	replan_r7: np.ndarray | None,
) -> None:
	BL = BagWideLayout
	if len(r) < BL.kRowFloats:
		return
	c = sdk.ctrl
	c.mode = 1
	if replan_l7 is not None and replan_r7 is not None:
		for i in range(3):
			c.arm_pos_exp_l[i] = float(replan_l7[i])
			c.arm_att_exp_l[i] = float(replan_l7[3 + i])
		c.cap_l = float(replan_l7[6])
		for i in range(3):
			c.arm_pos_exp_r[i] = float(replan_r7[i])
			c.arm_att_exp_r[i] = float(replan_r7[3 + i])
		c.cap_r = float(replan_r7[6])
	else:
		for i in range(3):
			c.arm_pos_exp_l[i] = float(r[i])
			c.arm_att_exp_l[i] = float(r[3 + i])
		c.cap_l = float(r[6])
		for i in range(3):
			c.arm_pos_exp_r[i] = float(r[7 + i])
			c.arm_att_exp_r[i] = float(r[10 + i])
		c.cap_r = float(r[13])
	for j in range(7):
		c.arm_q_exp_l[j] = float(r[BL.kArmQL0 + j])
		c.arm_q_exp_r[j] = float(r[BL.kArmQR0 + j])
	for j in range(4):
		c.waist_q_exp[j] = float(r[BL.kWaistQ4 + j])
	for i in range(3):
		c.waist_pos_exp[i] = float(r[BL.kEeWaist0 + i])
		c.waist_att_exp[i] = float(r[BL.kEeWaist0 + 3 + i])
	c.head_q_exp[0] = float(r[BL.kHeadQ0])
	c.head_q_exp[1] = float(r[BL.kHeadQ1])
	c.head_att_exp[0] = 0.0
	c.head_att_exp[1] = float(r[BL.kHeadQ1])
	c.head_att_exp[2] = float(r[BL.kHeadQ0])
	c.car_translation_exp = float(r[BL.kBaseVel0])
	c.car_rotation_exp = float(r[BL.kBaseVel0 + 1])
	c.car_translation_status = c.car_translation_exp
	c.car_rotation_status = c.car_rotation_exp


def trajectory_playback_action_eef_interp(
	sdk: LinglongHSdkClass,
	bundle: TrajectoryConfigTask,
	act: TrajectoryConfigAction,
	object_udp: ObjectUdpReceiver | None,
	interp_start: ManiInterpStartSource | str | int = ManiInterpStartSource.kStatus,
	log_info: Callable[[str], None] = print,
	log_err: Callable[[str], None] = print,
) -> bool:
	sdk.set_mode(1)
	spd = max(act.speed, 1e-3)
	last_end = time.monotonic()
	com_any = act.com_left or act.com_right
	use_eef_replan = act.playback.has_dual_arm_eef_geometry and act.playback.dual_arm_eef_geometry.valid()
	if com_any and not use_eef_replan:
		log_err(f'[playback] com_left/right 需要合法 older（双臂几何），跳过动作 "{act.action_name}"\n')
		return True
	if use_eef_replan:
		log_info("[playback] eef_interp 启用末端重规划（YAML older + 每段腰位姿 ee_waist sandwich）。\n")
		if com_any:
			log_info(
				f'[playback] com_left/right：live（base 下物体 6D）在本动作 **开头读取一次** UDP 快照，整段 CSV 各 `send_eef` 段复用；task_name="{bundle.task_name}"\n'
			)
	geo = act.playback.dual_arm_eef_geometry
	live_l6: list[float] = []
	live_r6: list[float] = []
	if use_eef_replan:
		ok, live_l6, live_r6 = resolve_live627_for_dual_arm_eef_replan(
			bundle, act, geo, object_udp, log_err=log_err
		)
		if not ok:
			log_info(f'[playback] 跳过动作 "{act.action_name}"（com 物体未就绪或 UDP 未启用）\n')
			return True
	for i, seg in enumerate(act.eef_interp_rows):
		if i > 0:
			log_info(f"[playback] eef 段 {i + 1} 开始前，距上一段 send_eef_interpolation 结束间隔 {millis_since(last_end):.1f} ms\n")
		seg_time = max(1e-3, seg.send_time_s / spd)
		wp = seg.ee_waist[:3].tolist()
		wa = seg.ee_waist[3:6].tolist()
		hp = seg.head_att.tolist() if seg.has_head_att else None
		cap_lp = float(seg.cap_l) if seg.has_cap else None
		cap_rp = float(seg.cap_r) if seg.has_cap else None
		eef_l = seg.ee_l.astype(float).tolist()
		eef_r = seg.ee_r.astype(float).tolist()
		if use_eef_replan:
			T_waist = homogeneous_from_xyz_rpy6(seg.ee_waist)
			S_left = compute_sandwiched_replan_for_arm(
				T_waist,
				geo.recorded_ref_left6,
				live_l6,
				geo.robot_to_marker_calib_left6,
			)
			S_right = compute_sandwiched_replan_for_arm(
				T_waist,
				geo.recorded_ref_right6,
				live_r6,
				geo.robot_to_marker_calib_right6,
			)
			vl = apply_similarity_to_eef6(S_left, eef_l)
			vr = apply_similarity_to_eef6(S_right, eef_r)
			if len(vl) >= 6:
				eef_l = [float(vl[j]) for j in range(6)]
			if len(vr) >= 6:
				eef_r = [float(vr[j]) for j in range(6)]
		ok_send = sdk.send_eef_interpolation(
			eef_l,
			eef_r,
			seg_time,
			wp,
			wa,
			hp,
			cap_lp,
			cap_rp,
			interp_start,
		)
		last_end = time.monotonic()
		if not ok_send:
			log_err(f"[playback] send_eef_interpolation 中断或失败，段索引={i}\n")
			return False
		log_info(f"[playback] eef 段 {i + 1}/{len(act.eef_interp_rows)} 完成，本段时长={seg_time}s\n")
		delay_s = max(0.0, float(seg.post_segment_delay_s)) / spd
		if delay_s > 1e-9 and i + 1 < len(act.eef_interp_rows):
			log_info(f"[playback] eef 段间静置 post_delay={delay_s}s（CSV×1/speed）后进入下一行\n")
			time.sleep(delay_s)
	return True


def trajectory_playback_action_bag_wide(
	sdk: LinglongHSdkClass,
	bundle: TrajectoryConfigTask,
	act: TrajectoryConfigAction,
	object_udp: ObjectUdpReceiver | None,
	interp_start: ManiInterpStartSource | str | int = ManiInterpStartSource.kStatus,
	log_info: Callable[[str], None] = print,
	log_err: Callable[[str], None] = print,
) -> bool:
	_ = interp_start  # bag 宽行路径仅 send(nullptr)；与 C++ 一致保留参数便于 API 对齐
	rows = act.bag_wide_rows
	ts = act.bag_row_ts_ns
	sdk.set_mode(1)
	spd = max(act.speed, 1e-3)
	com_any = act.com_left or act.com_right
	use_eef_replan = act.playback.has_dual_arm_eef_geometry and act.playback.dual_arm_eef_geometry.valid()
	if com_any and not use_eef_replan:
		log_err(f'[playback] com_left/right 需要合法 older（双臂几何），跳过动作 "{act.action_name}"\n')
		return True
	if use_eef_replan:
		log_info("[playback] 宽行回放启用末端重规划（YAML older + 每帧腰系 sandwich）。\n")
		if com_any:
			log_info(
				f'[playback] com_left/right：live（base 下物体 6D）在本动作 **开头读取一次** UDP 快照，整段宽行逐帧复用；task_name="{bundle.task_name}"\n'
			)

	def _loge(msg: str) -> None:
		log_err(msg)

	geo = act.playback.dual_arm_eef_geometry
	live_l6: list[float] = []
	live_r6: list[float] = []
	if use_eef_replan:
		ok, live_l6, live_r6 = resolve_live627_for_dual_arm_eef_replan(
			bundle, act, geo, object_udp, log_err=_loge
		)
		if not ok:
			log_info(f'[playback] 跳过动作 "{act.action_name}"（com 物体未就绪或 UDP 未启用）\n')
			return True

	bag_replay_waist_q4 = np.zeros(4, dtype=np.float32)
	bag_replay_head_q = np.array([0.0, 0.1745], dtype=np.float32)

	for gi, r in enumerate(rows):
		if use_eef_replan:
			dec = decode_bag_motion_one_row(rows, gi, bag_replay_waist_q4, bag_replay_head_q)
			if dec is None:
				log_err(f"[playback] decode_bag_motion_one_row 失败，gi={gi}\n")
				return False
			row_l7, row_r7, row_w4, row_h2 = dec
			bag_replay_waist_q4 = row_w4
			bag_replay_head_q = row_h2
			waist_pose6 = fill_waist_pose6_for_replan(True, rows, gi, row_w4)
			T_waist = homogeneous_from_xyz_rpy6(waist_pose6)
			S_left = compute_sandwiched_replan_for_arm(
				T_waist,
				geo.recorded_ref_left6,
				live_l6,
				geo.robot_to_marker_calib_left6,
			)
			S_right = compute_sandwiched_replan_for_arm(
				T_waist,
				geo.recorded_ref_right6,
				live_r6,
				geo.robot_to_marker_calib_right6,
			)
			new_l7 = apply_similarity_to_arm_row7(S_left, row_l7)
			new_r7 = apply_similarity_to_arm_row7(S_right, row_r7)
			apply_bag_wide_row_to_ctrl(sdk, r, new_l7, new_r7)
		else:
			apply_bag_wide_row_to_ctrl(sdk, r, None, None)
		sdk.send(None, False)
		if gi + 1 < len(rows):
			sleep_s = 0.02 / spd
			if len(ts) == len(rows) and gi + 1 < len(ts) and ts[gi + 1] > ts[gi]:
				sleep_s = (ts[gi + 1] - ts[gi]) * 1e-9 / spd
			if sleep_s > 1e-6:
				time.sleep(sleep_s)
	return True


def trajectory_playback_task(
	sdk: LinglongHSdkClass,
	bundle: TrajectoryConfigTask,
	object_udp: ObjectUdpReceiver | None = None,
	interp_start: ManiInterpStartSource | str | int = ManiInterpStartSource.kStatus,
	log_info: Callable[[str], None] = print,
	log_err: Callable[[str], None] = print,
) -> bool:
	for act in bundle.actions:
		if act.pose_kind == PoseKind.kEefInterp:
			if not trajectory_playback_action_eef_interp(
				sdk, bundle, act, object_udp, interp_start, log_info, log_err
			):
				return False
		elif act.pose_kind == PoseKind.kBagWide:
			if not trajectory_playback_action_bag_wide(
				sdk, bundle, act, object_udp, interp_start, log_info, log_err
			):
				return False
		if act.sleep > 0:
			time.sleep(float(act.sleep))
	return True
