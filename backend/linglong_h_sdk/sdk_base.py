#!/usr/bin/env python3
# coding=utf-8
# Linglong-H SDK base (sdk_base.py)
'''=========== *** doc @ linglong-h UDP SDK（基础层）*** ===========

类名与 maniSdk 一致：
	maniSdkSensDataClass — 灵龙-H 状态 116f+i（udp_bridge.robot_data 语义）
	maniSdkCtrlDataClass — 灵龙-H 遥控 remote_msg_end
	maniSdkClass — UDP：self.sens / self.ctrl，send / recv / packData / unpackData

本文件无后台线程；拓展见 sdk_extend.py。
======================================================'''
import binascii
import json
import select
import socket
import struct
import threading
import time
from enum import IntEnum
from typing import Callable

import numpy as np

DEFAULT_ROBOT_IP = "192.168.1.28"
DEFAULT_CMD_PORT = 3336
DEFAULT_STATE_PORT = 3333
DEFAULT_MODE_PORT = 4141
DEFAULT_CHASSIS_IP = "192.168.1.204"
DEFAULT_CHASSIS_CMD_PORT = 19205
DEFAULT_CHASSIS_STATE_PORT = 19204
DEFAULT_CHASSIS_CMD_TYPE = 2010
DEFAULT_CHASSIS_SPEED_QUERY_TYPE = 1005
DEFAULT_CHASSIS_BATTERY_QUERY_TYPE = 1007


class ManiInterpStartSource(IntEnum):
	"""关节/末端插值与 ``reset_to_init`` 起点，与 C++ ``ManiInterpStartSource`` 一致。"""

	kStatus = 0  # 从 sens（默认）
	kCtrl = 1  # 从 ctrl 当前期望


class _SendControlState:
	"""全局发送控制状态：用于跨类中断插值/回放发送。"""
	_lock = threading.Lock()
	_interrupt_seq = 0
	_last_reason = ""

	@classmethod
	def trigger_interrupt(cls, reason: str = ""):
		"""触发全局发送中断并记录原因。"""
		with cls._lock:
			cls._interrupt_seq += 1
			cls._last_reason = str(reason)

	@classmethod
	def get_interrupt_seq(cls) -> int:
		"""获取全局中断序号。"""
		with cls._lock:
			return int(cls._interrupt_seq)

	@classmethod
	def get_last_reason(cls) -> str:
		"""获取最近一次中断原因。"""
		with cls._lock:
			return cls._last_reason


class maniSdkSensDataClass:
	"""灵龙-H 传感状态：116 float + 1 int（与 udp_bridge 一致）。"""

	FMT = "<116f i"
	SIZE = struct.calcsize(FMT)

	def __init__(self):
		"""初始化对象并设置默认成员。"""
		self.q_exp = np.zeros((2, 7), dtype=np.float32)  # 双臂关节期望角（左/右，各7）
		self.q = np.zeros((2, 7), dtype=np.float32)  # 双臂关节实际角（左/右，各7）
		self.dq = np.zeros((2, 7), dtype=np.float32)  # 双臂关节角速度（左/右，各7）
		self.epos_h = np.zeros((2, 7), dtype=np.float32)  # 双臂末端实际位姿（x,y,z,r,p,y,+1保留）
		self.epos_exp = np.zeros((2, 7), dtype=np.float32)  # 双臂末端期望位姿（x,y,z,r,p,y,+1保留）
		self.epos_waist = np.zeros(7, dtype=np.float32)  # 腰部末端实际位姿（x,y,z,r,p,y,+1保留）
		self.epos_exp_waist = np.zeros(7, dtype=np.float32)  # 腰部末端期望位姿（x,y,z,r,p,y,+1保留）
		self.tau = np.zeros((2, 7), dtype=np.float32)  # 双臂关节力矩（左/右，各7）
		self.cap_rate_exp = np.zeros(2, dtype=np.float32)  # 左右夹爪期望开合
		self.cap_rate = np.zeros(2, dtype=np.float32)  # 左右夹爪实际开合
		self.waist = np.zeros(4, dtype=np.float32)  # 腰部4关节实际角
		self.waist_exp = np.zeros(4, dtype=np.float32)  # 腰部4关节期望角
		self.head = np.zeros(2, dtype=np.float32)  # 头部2关节实际角
		self.head_exp = np.zeros(2, dtype=np.float32)  # 头部2关节期望角
		self.base_vel = np.zeros(2, dtype=np.float32)  # 底盘实际速度 [vx, w]
		self.battery_level = np.float32(np.nan)  # 电量（0~1），来自底盘TCP查询
		self.save_data = 0  # 保留整数字段（与原协议尾部对齐）

	def print(self):
		"""打印当前对象字段。"""
		np.set_printoptions(suppress=True)
		print("============ maniSdkSensDataClass (116f+i)")
		for k, v in self.__dict__.items():
			if k.startswith("_"):
				continue
			print(k, "=", v)

	def unpack_from(self, buf: bytes):
		"""从 UDP 载荷解析（与 udp_bridge.udp_callback 顺序一致）。"""
		fmt = self.__class__.FMT
		size = self.__class__.SIZE
		if len(buf) < size:
			raise ValueError(f"状态包长度不足: {len(buf)} < {size}")
		t = struct.unpack(fmt, buf[:size])
		idx = 0
		for key in ("q_exp", "q", "dq", "epos_h", "epos_exp"):
			arr = getattr(self, key)
			for i in range(2):
				for j in range(7):
					arr[i, j] = np.float32(t[idx])
					idx += 1
		for j in range(7):
			self.epos_waist[j] = np.float32(t[idx])
			idx += 1
		for j in range(7):
			self.epos_exp_waist[j] = np.float32(t[idx])
			idx += 1
		for i in range(2):
			for j in range(7):
				self.tau[i, j] = np.float32(t[idx])
				idx += 1
		for i in range(2):
			self.cap_rate_exp[i] = np.float32(t[idx])
			idx += 1
		for i in range(2):
			self.cap_rate[i] = np.float32(t[idx])
			idx += 1
		for i in range(4):
			self.waist[i] = np.float32(t[idx])
			idx += 1
		for i in range(4):
			self.waist_exp[i] = np.float32(t[idx])
			idx += 1
		for j in range(2):
			self.head[j] = np.float32(t[idx])
			idx += 1
		for j in range(2):
			self.head_exp[j] = np.float32(t[idx])
			idx += 1
		for j in range(2):
			self.base_vel[j] = np.float32(t[idx])
			idx += 1
		self.save_data = int(t[idx])

	def pack_bytes(self) -> bytes:
		"""打成与 udp_bridge.udp_thread 相同顺序的一帧。"""
		flat: list[float] = []
		for key in ("q_exp", "q", "dq", "epos_h", "epos_exp"):
			flat.extend(float(x) for x in getattr(self, key).reshape(-1))
		flat.extend(float(x) for x in self.epos_waist)
		flat.extend(float(x) for x in self.epos_exp_waist)
		flat.extend(float(x) for x in self.tau.reshape(-1))
		flat.extend(float(x) for x in self.cap_rate_exp)
		flat.extend(float(x) for x in self.cap_rate)
		flat.extend(float(x) for x in self.waist)
		flat.extend(float(x) for x in self.waist_exp)
		flat.extend(float(x) for x in self.head)
		flat.extend(float(x) for x in self.head_exp)
		flat.extend(float(x) for x in self.base_vel)
		if len(flat) != 116:
			raise RuntimeError(f"内部错误: flat 长度 {len(flat)} != 116")
		return struct.pack(self.__class__.FMT, *flat, int(self.save_data))


class maniSdkCtrlDataClass:
	"""灵龙-H 控制指令：remote_msg_end（用于 packData / send）。"""

	FMT_NO_CRC = (
		"<"
		"c"
		"3f" "3f" "7f" "f"
		"3f" "3f" "7f" "f"
		"3f" "3f" "4f"
		"3f" "2f"
		"f" "f" "f" "f"
	)
	FMT_WITH_CRC = FMT_NO_CRC + "H"

	@classmethod
	def pack_payload(cls, ctrl: "maniSdkCtrlDataClass") -> bytes:
		"""函数说明。"""
		return struct.pack(
			cls.FMT_NO_CRC,
			int(ctrl.mode).to_bytes(1, "little", signed=False),
			*ctrl.arm_pos_exp_l.astype(np.float32),
			*ctrl.arm_att_exp_l.astype(np.float32),
			*ctrl.arm_q_exp_l.astype(np.float32),
			float(ctrl.cap_l),
			*ctrl.arm_pos_exp_r.astype(np.float32),
			*ctrl.arm_att_exp_r.astype(np.float32),
			*ctrl.arm_q_exp_r.astype(np.float32),
			float(ctrl.cap_r),
			*ctrl.waist_pos_exp.astype(np.float32),
			*ctrl.waist_att_exp.astype(np.float32),
			*ctrl.waist_q_exp.astype(np.float32),
			*ctrl.head_att_exp.astype(np.float32),
			*ctrl.head_q_exp.astype(np.float32),
			float(ctrl.car_translation_exp),
			float(ctrl.car_rotation_exp),
			float(ctrl.car_translation_status),
			float(ctrl.car_rotation_status),
		)

	def __init__(self):
		"""初始化对象并设置默认成员。"""
		self.mode = 1  # 控制模式：1=末端模式，0=关节模式
		self.arm_pos_exp_l = np.array([0.3, 0.25, 0.65], dtype=np.float32)  # 左臂末端位置期望 [x,y,z]
		self.arm_att_exp_l = np.zeros(3, np.float32)  # 左臂末端姿态期望 [roll,pitch,yaw]
		self.arm_q_exp_l = np.array([0.0, 0.35, 0.0, 0.0, 0.0, 0.0, 0.0], dtype=np.float32)  # 左臂7关节期望角
		self.cap_l = 0.0  # 左夹爪期望开合
		self.arm_pos_exp_r = np.array([0.3, -0.25, 0.65], dtype=np.float32)  # 右臂末端位置期望 [x,y,z]
		self.arm_att_exp_r = np.zeros(3, np.float32)  # 右臂末端姿态期望 [roll,pitch,yaw]
		self.arm_q_exp_r = np.array([0.0, -0.35, 0.0, 0.0, 0.0, 0.0, 0.0], dtype=np.float32)  # 右臂7关节期望角
		self.cap_r = 0.0  # 右夹爪期望开合
		self.waist_pos_exp = np.array([0.0, 0.0, 0.8], dtype=np.float32)  # 腰部末端位置期望 [x,y,z]
		self.waist_att_exp = np.zeros(3, np.float32)  # 腰部末端姿态期望 [roll,pitch,yaw]
		self.waist_q_exp = np.array([-0.2, 0.4, -0.2, 0.0], dtype=np.float32)  # 腰部4关节期望角
		self.head_att_exp = np.array([0.0, 0.1745, 0.0], np.float32)  # 头部姿态期望 [roll,pitch,yaw]
		self.head_q_exp = np.array([0.0, 0.1745], np.float32)  # 头部2关节期望角
		self.car_translation_exp = 0.0  # 底盘期望线速度 vx
		self.car_rotation_exp = 0.0  # 底盘期望角速度 w
		self.car_translation_status = 0.0  # 底盘实际线速度 vx（状态链路回填）
		self.car_rotation_status = 0.0  # 底盘实际角速度 w（状态链路回填）

	def print(self):
		"""打印当前对象字段。"""
		np.set_printoptions(suppress=True)
		print("============ maniSdkCtrlDataClass (remote_msg_end)")
		for k, v in self.__dict__.items():
			if k.startswith("_"):
				continue
			print(k, "=", v)


class RobotModeMessage:
	"""机器人模式切换结构体（C端: int32 x4, pack(1)）。"""

	FMT = "<4i"
	SIZE = struct.calcsize(FMT)

	def __init__(
		self,
		enable: int = 0,
		disable: int = 0,
		retract_mode: int = 0,
		inference_teleop_mode: int = 0,
	):
		"""初始化对象并设置默认成员。"""
		self.enable = int(enable)  # 上使能位
		self.disable = int(disable)  # 下使能位
		self.retract_mode = int(retract_mode)  # 收拢模式位
		self.inference_teleop_mode = int(inference_teleop_mode)  # 自主/摇操模式位

	def pack(self) -> bytes:
		"""打包当前对象为字节流。"""
		return struct.pack(
			self.__class__.FMT,
			int(self.enable),
			int(self.disable),
			int(self.retract_mode),
			int(self.inference_teleop_mode),
		)

	def unpack(self, buf: bytes):
		"""从字节流解析当前对象。"""
		size = self.__class__.SIZE
		if len(buf) < size:
			raise ValueError(f"RobotMode长度不足: {len(buf)} < {size}")
		self.enable, self.disable, self.retract_mode, self.inference_teleop_mode = struct.unpack(
			self.__class__.FMT, buf[:size]
		)
		return self

	def print(self):
		"""打印当前对象字段。"""
		print("============ RobotModeMessage")
		print("enable =", self.enable)
		print("disable =", self.disable)
		print("retract_mode =", self.retract_mode)
		print("inference_teleop_mode =", self.inference_teleop_mode)


class RobotModeManager:
	"""独立模式切换发送器：与 maniSdkClass 解耦，可并行发送模式包。"""

	def __init__(self, ip: str = DEFAULT_ROBOT_IP, port: int = DEFAULT_MODE_PORT, *, debug: bool = False):
		"""初始化对象并设置默认成员。"""
		self.rbtIpPort = (ip, port)  # 模式包发送目标 (ip,port)
		self.sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)  # 模式管理UDP socket
		self._robot_mode_pkt_len = RobotModeMessage.SIZE  # 模式包字节长度缓存
		self._debug = bool(debug)  # 是否开启调试打印线程
		self._debug_lock = threading.Lock()  # 调试打印共享数据互斥锁
		self._debug_last_msg = RobotModeMessage()  # 最近一次发送的模式消息
		self._debug_stop = threading.Event()  # 调试线程停止信号
		self._debug_thread = None  # 调试线程句柄
		if self._debug:
			self._debug_thread = threading.Thread(
				target=self._debug_thread_loop,
				name="RobotModeManager-debug",
				daemon=True,
			)
			self._debug_thread.start()

	def _debug_thread_loop(self):
		"""内部辅助函数。"""
		period = 0.1  # 10Hz
		while not self._debug_stop.is_set():
			with self._debug_lock:
				msg = self._debug_last_msg
				print("===== RobotModeManager Debug (10Hz) =====")
				print(f"dst = {self.rbtIpPort}")
				msg.print()
			time.sleep(period)

	def send_robot_mode(self, msg: RobotModeMessage):
		"""发送对应指令或数据。"""
		with self._debug_lock:
			self._debug_last_msg = RobotModeMessage(
				enable=int(msg.enable),
				disable=int(msg.disable),
				retract_mode=int(msg.retract_mode),
				inference_teleop_mode=int(msg.inference_teleop_mode),
			)
		self.sk.sendto(msg.pack(), self.rbtIpPort)

	def _send_robot_mode_for(self, msg: RobotModeMessage, duration_s: float, hz: float = 50.0):
		"""内部辅助函数。"""
		duration = max(0.0, float(duration_s))
		period = 1.0 / max(1.0, float(hz))
		t = 0.0
		while t < duration:
			start = time.time()
			self.send_robot_mode(msg)
			elapsed = time.time() - start
			sleep_t = period - elapsed
			if sleep_t > 0:
				time.sleep(sleep_t)
			t += period

	def _notify_control_switch(self, reason: str):
		"""触发全局发送中断：让插值/回放立即结束。"""
		_SendControlState.trigger_interrupt(reason)

	def robot_enable_up(self):
		"""上使能：只改 enable/disable 位，其他位保持当前值。"""
		self._notify_control_switch("robot_enable_up")
		with self._debug_lock:
			msg = RobotModeMessage(
				enable=int(self._debug_last_msg.enable),
				disable=int(self._debug_last_msg.disable),
				retract_mode=int(self._debug_last_msg.retract_mode),
				inference_teleop_mode=int(self._debug_last_msg.inference_teleop_mode),
			)
		msg.enable = 1
		msg.disable = 0
		self._send_robot_mode_for(msg, 4.0)
		msg.enable = 0
		self._send_robot_mode_for(msg, 1.0)

	def robot_enable_down(self):
		"""下使能：只改 enable/disable 位，其他位保持当前值。"""
		self._notify_control_switch("robot_enable_down")
		with self._debug_lock:
			msg = RobotModeMessage(
				enable=int(self._debug_last_msg.enable),
				disable=int(self._debug_last_msg.disable),
				retract_mode=int(self._debug_last_msg.retract_mode),
				inference_teleop_mode=int(self._debug_last_msg.inference_teleop_mode),
			)
		msg.enable = 0
		msg.disable = 1
		self._send_robot_mode_for(msg, 4.0)
		msg.disable = 0
		self._send_robot_mode_for(msg, 1.0)

	def robot_operation_mode(self):
		"""操作模式：只改 inference_teleop_mode=0，其他位保持当前值。"""
		self._notify_control_switch("robot_operation_mode")
		with self._debug_lock:
			msg = RobotModeMessage(
				enable=int(self._debug_last_msg.enable),
				disable=int(self._debug_last_msg.disable),
				retract_mode=int(self._debug_last_msg.retract_mode),
				inference_teleop_mode=int(self._debug_last_msg.inference_teleop_mode),
			)
		msg.inference_teleop_mode = 0
		self._send_robot_mode_for(msg, 1.0)

	def robot_autonomous_mode(self):
		"""自主模式：只改 inference_teleop_mode=1，其他位保持当前值。"""
		self._notify_control_switch("robot_autonomous_mode")
		with self._debug_lock:
			msg = RobotModeMessage(
				enable=int(self._debug_last_msg.enable),
				disable=int(self._debug_last_msg.disable),
				retract_mode=int(self._debug_last_msg.retract_mode),
				inference_teleop_mode=int(self._debug_last_msg.inference_teleop_mode),
			)
		msg.inference_teleop_mode = 1
		self._send_robot_mode_for(msg, 1.0)

	def robot_normal_mode(self):
		"""普通模式：只改 retract_mode=0，其他位保持当前值。"""
		self._notify_control_switch("robot_normal_mode")
		with self._debug_lock:
			msg = RobotModeMessage(
				enable=int(self._debug_last_msg.enable),
				disable=int(self._debug_last_msg.disable),
				retract_mode=int(self._debug_last_msg.retract_mode),
				inference_teleop_mode=int(self._debug_last_msg.inference_teleop_mode),
			)
		msg.retract_mode = 0
		self._send_robot_mode_for(msg, 1.0)

	def robot_retract_mode(self):
		"""归纳模式：只改 retract_mode=1，其他位保持当前值。"""
		self._notify_control_switch("robot_retract_mode")
		with self._debug_lock:
			msg = RobotModeMessage(
				enable=int(self._debug_last_msg.enable),
				disable=int(self._debug_last_msg.disable),
				retract_mode=int(self._debug_last_msg.retract_mode),
				inference_teleop_mode=int(self._debug_last_msg.inference_teleop_mode),
			)
		msg.retract_mode = 1
		self._send_robot_mode_for(msg, 1.0)

	def close(self):
		"""函数说明。"""
		self._debug_stop.set()
		t = self._debug_thread
		if t is not None and t.is_alive():
			t.join(timeout=1.0)
		self._debug_thread = None
		try:
			self.sk.close()
		except OSError:
			pass


class maniSdkClass:
	"""灵龙-H UDP：self.sens / self.ctrl；send / recv / packData / unpackData；无后台线程。"""

	@staticmethod
	def crc16_ccitt(data: bytes) -> int:
		"""函数说明。"""
		return binascii.crc_hqx(data, 0xFFFF)

	@staticmethod
	def _f32_vec(data, n: int, name: str) -> np.ndarray:
		"""内部辅助函数。"""
		a = np.asarray(data, dtype=np.float32).reshape(-1)
		if a.size != n:
			raise ValueError(f"{name} 需要长度 {n}，实际 {a.size}")
		return a

	@staticmethod
	def _pose_to_rotation_matrix(euler_angles_xyz: np.ndarray) -> np.ndarray:
		"""Rz(yaw) * Ry(pitch) * Rx(roll)."""
		roll, pitch, yaw = euler_angles_xyz
		cr, sr = np.cos(roll), np.sin(roll)
		cp, sp = np.cos(pitch), np.sin(pitch)
		cy, sy = np.cos(yaw), np.sin(yaw)
		rx = np.array([[1.0, 0.0, 0.0], [0.0, cr, -sr], [0.0, sr, cr]])
		ry = np.array([[cp, 0.0, sp], [0.0, 1.0, 0.0], [-sp, 0.0, cp]])
		rz = np.array([[cy, -sy, 0.0], [sy, cy, 0.0], [0.0, 0.0, 1.0]])
		return maniSdkClass._mat3_mul(maniSdkClass._mat3_mul(rz, ry), rx)

	@staticmethod
	def _mat3_mul(a: np.ndarray, b: np.ndarray) -> np.ndarray:
		"""手写3x3矩阵乘法，避免触发底层BLAS/OpenMP。"""
		out = np.zeros((3, 3), dtype=np.float64)
		for i in range(3):
			for j in range(3):
				s = 0.0
				for k in range(3):
					s += float(a[i, k]) * float(b[k, j])
				out[i, j] = s
		return out

	@staticmethod
	def _rotation_to_pose_nei(rotation_matrix: np.ndarray) -> np.ndarray:
		"""旋转矩阵 -> rpy，和 _pose_to_rotation_matrix 保持配套。"""
		sin_pitch = -rotation_matrix[2, 0]
		pitch = np.arcsin(np.clip(sin_pitch, -1.0, 1.0))
		if abs(rotation_matrix[2, 0]) < 0.999999:
			roll = np.arctan2(rotation_matrix[2, 1], rotation_matrix[2, 2])
			yaw = np.arctan2(rotation_matrix[1, 0], rotation_matrix[0, 0])
		else:
			roll = 0.0
			yaw = np.arctan2(-rotation_matrix[0, 1], rotation_matrix[1, 1])
		return np.array([roll, pitch, yaw], dtype=np.float32)

	@classmethod
	def _compose_rpy_increment(cls, rpy_current: np.ndarray, rpy_delta: np.ndarray) -> np.ndarray:
		"""相对姿态复合：R_new = R_cur * R_delta。"""
		r_cur = cls._pose_to_rotation_matrix(rpy_current.astype(float))
		r_delta = cls._pose_to_rotation_matrix(rpy_delta.astype(float))
		r_new = cls._mat3_mul(r_cur, r_delta)
		return cls._rotation_to_pose_nei(r_new)

	def __init__(
		self,
		ip: str = DEFAULT_ROBOT_IP,
		port: int = DEFAULT_CMD_PORT,
		*,
		state_port: int = DEFAULT_STATE_PORT,
		chassis_tcp_on_send: bool = True,
	):
		"""初始化对象并设置默认成员。"""
		self.rbtIpPort = (ip, port)  # 机器人控制UDP目标地址（发送cmd）
		self.stateIpPort = (ip, int(state_port))  # 机器人状态UDP目标地址（请求/接收state）
		self.sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)  # 机器人UDP socket（控制+状态复用）
		self.sk.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 65536)  # 提高接收缓冲，减少丢包
		self.sk.setblocking(0)  # 非阻塞模式，配合 select 轮询
		self.ctrl = maniSdkCtrlDataClass()  # 当前控制目标帧（待发送）
		self.sens = maniSdkSensDataClass()  # 最近一次状态帧（已接收）
		# 是否已通过状态口成功收编至少一帧合法 UDP 状态（见 ``_exchange_state_once``）
		self._state_udp_received_ok = False
		self._cmd_pkt_len = struct.calcsize(maniSdkCtrlDataClass.FMT_WITH_CRC)  # 控制包长度（含CRC）
		self._sens_pkt_len = maniSdkSensDataClass.SIZE  # 状态包长度（116f+i）
		self._state_cv = threading.Condition()  # 状态更新条件变量（线程间同步）
		self._mani_send_paused = False  # 是否处于 send 禁止状态
		self._paused_ctrl_snapshot: maniSdkCtrlDataClass | None = None  # pause 时记录的控制快照

		# 底盘速度命令链路（TCP: 19205）
		self.chassis_sock: socket.socket | None = None  # 底盘速度命令TCP连接
		self.chassis_req_id = 1  # 底盘速度命令请求序号（循环递增）
		self.chassis_timeout_s = 0.2  # 底盘速度命令TCP超时（秒）
		self.chassis_target: tuple[str, int] | None = (DEFAULT_CHASSIS_IP, DEFAULT_CHASSIS_CMD_PORT)  # 底盘速度命令目标
		self.chassis_request_builder: Callable[[int, float, float], bytes] | None = None  # 自定义底盘速度包构造器
		self.chassis_command_type = DEFAULT_CHASSIS_CMD_TYPE  # 底盘速度命令消息类型（默认2010）
		self.chassis_tcp_on_send = bool(chassis_tcp_on_send)  # send() 时是否自动联动底盘TCP发送

		# 底盘状态查询链路（TCP: 19204，速度1005/电量1007）
		self.chassis_state_sock: socket.socket | None = None  # 底盘状态查询TCP连接
		self.chassis_state_req_id = 1  # 底盘状态查询请求序号（速度/电量共用）
		self.chassis_state_timeout_s = 0.05  # 底盘状态查询TCP超时（秒）
		self.chassis_state_target: tuple[str, int] | None = (DEFAULT_CHASSIS_IP, DEFAULT_CHASSIS_STATE_PORT)  # 底盘状态查询目标
		self.chassis_speed_query_type = DEFAULT_CHASSIS_SPEED_QUERY_TYPE  # 底盘速度查询消息类型（默认1005）
		self.chassis_battery_query_type = DEFAULT_CHASSIS_BATTERY_QUERY_TYPE  # 底盘电量查询消息类型（默认1007）
		self.battery_level = float("nan")  # 最近一次查询得到的电量（0~1，未知为NaN）

	def peer(self) -> tuple[str, int]:
		"""返回控制UDP目标地址。"""
		return self.rbtIpPort

	def state_peer(self) -> tuple[str, int]:
		"""返回状态UDP目标地址。"""
		return self.stateIpPort

	def configure_chassis_tcp(
		self,
		ip: str = DEFAULT_CHASSIS_IP,
		port: int = DEFAULT_CHASSIS_CMD_PORT,
		*,
		timeout_s: float = 0.2,
		command_type: int = DEFAULT_CHASSIS_CMD_TYPE,
		request_builder: Callable[[int, float, float], bytes] | None = None,
	) -> "maniSdkClass":
		"""配置底盘TCP发送参数；request_builder(req_id, vx, wz)->bytes。"""
		self.chassis_target = (str(ip), int(port))
		self.chassis_timeout_s = max(0.01, float(timeout_s))
		self.chassis_command_type = int(command_type) & 0xFFFF
		if request_builder is not None:
			self.chassis_request_builder = request_builder
		return self

	def set_chassis_tcp_on_send(self, enabled: bool) -> "maniSdkClass":
		"""设置 send() 是否自动联动底盘TCP。"""
		self.chassis_tcp_on_send = bool(enabled)
		return self

	def configure_chassis_state_tcp(
		self,
		ip: str = DEFAULT_CHASSIS_IP,
		port: int = DEFAULT_CHASSIS_STATE_PORT,
		*,
		timeout_s: float = 0.05,
		query_type: int = DEFAULT_CHASSIS_SPEED_QUERY_TYPE,
		battery_query_type: int = DEFAULT_CHASSIS_BATTERY_QUERY_TYPE,
	) -> "maniSdkClass":
		"""配置底盘状态查询TCP（速度/电量）。

		- query_type: 速度查询消息类型（默认1005）
		- battery_query_type: 电量查询消息类型（默认1007）
		"""
		next_target = (str(ip), int(port))
		if self.chassis_state_target != next_target:
			self._close_chassis_state_connection()
		self.chassis_state_target = next_target
		self.chassis_state_timeout_s = max(0.01, float(timeout_s))
		self.chassis_speed_query_type = int(query_type) & 0xFFFF
		self.chassis_battery_query_type = int(battery_query_type) & 0xFFFF
		return self

	@staticmethod
	def _build_seer_header(req_id: int, msg_type: int, json_len: int, reserved6: bytes | None = None) -> bytes:
		"""构造对应协议数据。"""
		header = bytearray(16)
		header[0] = 0x5A
		header[1] = 0x01
		header[2:4] = struct.pack(">H", int(req_id) & 0xFFFF)
		header[4:8] = struct.pack(">I", int(json_len) & 0xFFFFFFFF)
		header[8:10] = struct.pack(">H", int(msg_type) & 0xFFFF)
		if reserved6 is not None:
			r = bytes(reserved6)[:6]
			header[10 : 10 + len(r)] = r
		return bytes(header)

	@staticmethod
	def _recv_exact(sock: socket.socket, n: int) -> bytes:
		"""内部辅助函数。"""
		buf = bytearray()
		while len(buf) < n:
			chunk = sock.recv(n - len(buf))
			if not chunk:
				raise OSError("TCP连接中断，接收不完整")
			buf.extend(chunk)
		return bytes(buf)

	def _default_chassis_request(self, req_id: int, translation: float, rotation: float) -> bytes:
		"""默认底盘TCP请求格式：Seer 协议头(16B) + JSON。"""
		command_type = int(self.chassis_command_type) & 0xFFFF
		json_bytes = json.dumps(
			{
				"vx": float(translation),
				"w": float(rotation),
			},
			separators=(",", ":"),
			ensure_ascii=False,
		).encode("utf-8")
		data_len = len(json_bytes)

		header = bytearray(16)
		header[0] = 0x5A  # 协议头
		header[1] = 0x01  # 版本
		header[2:4] = struct.pack(">H", int(req_id) & 0xFFFF)  # number, big-endian
		header[4:8] = struct.pack(">I", data_len)  # length, big-endian
		header[8:10] = struct.pack(">H", command_type)  # type, big-endian
		# reserved[0..1]: command_type 原始低/高字节（与 SeerTCPTest 一致）
		header[10] = command_type & 0xFF
		header[11] = (command_type >> 8) & 0xFF
		# reserved[2..3]: JSON长度按 Seer 兼容写法存储
		big_json_size = socket.htons(data_len & 0xFFFF)
		header[12] = big_json_size & 0xFF
		header[13] = (big_json_size >> 8) & 0xFF
		header[14] = 0
		header[15] = 0
		return bytes(header) + json_bytes

	def _close_chassis_connection(self):
		"""关闭对应资源连接。"""
		if self.chassis_sock is None:
			return
		try:
			self.chassis_sock.close()
		finally:
			self.chassis_sock = None

	def _close_chassis_state_connection(self):
		"""关闭对应资源连接。"""
		if self.chassis_state_sock is None:
			return
		try:
			self.chassis_state_sock.close()
		finally:
			self.chassis_state_sock = None

	def _ensure_chassis_connection(self) -> socket.socket:
		"""确保对应资源可用，不可用时自动创建。"""
		if self.chassis_sock is not None:
			return self.chassis_sock
		if self.chassis_target is None:
			raise ValueError("未配置底盘TCP目标，请先调用 configure_chassis_tcp()")
		sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
		sock.settimeout(float(self.chassis_timeout_s))
		sock.connect(self.chassis_target)
		self.chassis_sock = sock
		return sock

	def _ensure_chassis_state_connection(self) -> socket.socket:
		"""确保对应资源可用，不可用时自动创建。"""
		if self.chassis_state_sock is not None:
			return self.chassis_state_sock
		if self.chassis_state_target is None:
			raise ValueError("未配置底盘状态TCP目标，请先调用 configure_chassis_state_tcp()")
		sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
		sock.settimeout(float(self.chassis_state_timeout_s))
		sock.connect(self.chassis_state_target)
		self.chassis_state_sock = sock
		return sock

	def _build_chassis_request(self, translation: float, rotation: float) -> bytes:
		"""构造对应协议数据。"""
		builder = self.chassis_request_builder
		if builder is None:
			return self._default_chassis_request(self.chassis_req_id, float(translation), float(rotation))
		return builder(self.chassis_req_id, float(translation), float(rotation))

	def _query_chassis_state_json(self, msg_type: int, request_json: dict | None = None) -> dict:
		"""发送底盘状态查询请求并解析 JSON 响应。"""
		req_id = self.chassis_state_req_id
		if request_json is None:
			body = b""
		else:
			body = json.dumps(request_json, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
		request = self._build_seer_header(req_id=req_id, msg_type=msg_type, json_len=len(body)) + body
		try:
			sock = self._ensure_chassis_state_connection()
			sock.sendall(request)
			head = self._recv_exact(sock, 16)
		except OSError:
			self._close_chassis_state_connection()
			sock = self._ensure_chassis_state_connection()
			sock.sendall(request)
			head = self._recv_exact(sock, 16)

		json_len = struct.unpack(">I", head[4:8])[0]
		resp_text = "{}"
		if json_len > 0:
			resp_text = self._recv_exact(sock, int(json_len)).decode("utf-8", errors="replace")

		self.chassis_state_req_id = (self.chassis_state_req_id + 1) & 0xFFFF
		if self.chassis_state_req_id == 0:
			self.chassis_state_req_id = 1

		try:
			fields = json.loads(resp_text)
		except json.JSONDecodeError as exc:
			raise ValueError(f"底盘状态响应JSON解析失败: {resp_text}") from exc
		if not isinstance(fields, dict):
			raise ValueError(f"底盘状态响应不是JSON对象: {resp_text}")
		return fields

	def query_chassis_speed_state(self) -> tuple[float, float]:
		"""查询底盘实际速度，返回(vx, w)。

		优先使用字段 vx/w，兼容 r_vx/r_w。
		"""
		fields = self._query_chassis_state_json(self.chassis_speed_query_type, request_json=None)
		vx_raw = fields.get("vx", fields.get("r_vx", 0.0))
		w_raw = fields.get("w", fields.get("r_w", 0.0))
		return float(vx_raw), float(w_raw)

	def query_chassis_battery_level(self, simple: bool = True) -> float:
		"""查询底盘电量百分比（0~1）。

		simple=True 时，请求体使用 {"simple": true}。
		"""
		req = {"simple": True} if simple else None
		fields = self._query_chassis_state_json(self.chassis_battery_query_type, request_json=req)
		level_raw = fields.get("battery_level")
		if level_raw is None:
			raise ValueError(f"电池响应缺少 battery_level 字段: {fields}")
		return float(level_raw)

	def send_chassis_command(self, translation: float, rotation: float) -> int:
		"""通过底盘TCP发送速度命令；失败自动重连重试一次。"""
		request = self._build_chassis_request(translation, rotation)
		try:
			sock = self._ensure_chassis_connection()
			sock.sendall(request)
		except OSError:
			self._close_chassis_connection()
			sock = self._ensure_chassis_connection()
			try:
				sock.sendall(request)
			except OSError:
				self._close_chassis_connection()
				raise
		self.chassis_req_id = (self.chassis_req_id + 1) & 0xFFFF
		if self.chassis_req_id == 0:
			self.chassis_req_id = 1
		return len(request)

	def print_all(self):
		"""打印当前控制与状态对象。"""
		self.ctrl.print()
		self.sens.print()

	def _exchange_state_once(self, timeout: float) -> bool:
		"""单轮状态更新：UDP状态 + 底盘速度 + 底盘电量。"""
		with self._state_cv:
			payload = self.sens.pack_bytes()
		self.sk.sendto(payload, self.stateIpPort)
		try:
			r, _, _ = select.select([self.sk], [], [], float(timeout))
		except (ValueError, OSError):
			return False
		if not r:
			return False
		try:
			buf, _ = self.sk.recvfrom(2048)
		except (BlockingIOError, OSError):
			return False
		if len(buf) < self._sens_pkt_len:
			return False
		with self._state_cv:
			try:
				self.sens.unpack_from(buf[: self._sens_pkt_len])
			except ValueError:
				return False
			self._state_udp_received_ok = True
		try:
			vx, w = self.query_chassis_speed_state()
		except Exception:
			vx = None
			w = None
		try:
			battery_level = self.query_chassis_battery_level(simple=True)
		except Exception:
			battery_level = None
		with self._state_cv:
			if vx is not None and w is not None:
				self.sens.base_vel[0] = np.float32(vx)
				self.sens.base_vel[1] = np.float32(w)
				self.ctrl.car_translation_status = float(vx)
				self.ctrl.car_rotation_status = float(w)
			if battery_level is not None:
				self.battery_level = float(battery_level)
				self.sens.battery_level = np.float32(battery_level)
			self._state_cv.notify_all()
		return True

	def set_mode(self, mode: int) -> "maniSdkClass":
		"""设置对应控制目标参数。"""
		self.ctrl.mode = int(mode) & 0xFF
		return self

	def set_left_end(self, pos_xyz, rpy_xyz, relative: bool = False) -> "maniSdkClass":
		"""设置对应控制目标参数。"""
		pos = self._f32_vec(pos_xyz, 3, "pos_xyz(left)")
		rpy = self._f32_vec(rpy_xyz, 3, "rpy_xyz(left)")
		if relative:
			self.ctrl.arm_pos_exp_l[:] += pos
			self.ctrl.arm_att_exp_l[:] = self._compose_rpy_increment(self.ctrl.arm_att_exp_l, rpy)
		else:
			self.ctrl.arm_pos_exp_l[:] = pos
			self.ctrl.arm_att_exp_l[:] = rpy
		return self

	def set_right_end(self, pos_xyz, rpy_xyz, relative: bool = False) -> "maniSdkClass":
		"""设置对应控制目标参数。"""
		pos = self._f32_vec(pos_xyz, 3, "pos_xyz(right)")
		rpy = self._f32_vec(rpy_xyz, 3, "rpy_xyz(right)")
		if relative:
			self.ctrl.arm_pos_exp_r[:] += pos
			self.ctrl.arm_att_exp_r[:] = self._compose_rpy_increment(self.ctrl.arm_att_exp_r, rpy)
		else:
			self.ctrl.arm_pos_exp_r[:] = pos
			self.ctrl.arm_att_exp_r[:] = rpy
		return self

	def set_waist_end(self, pos_xyz, rpy_xyz, relative: bool = False) -> "maniSdkClass":
		"""设置对应控制目标参数。"""
		pos = self._f32_vec(pos_xyz, 3, "pos_xyz(waist)")
		rpy = self._f32_vec(rpy_xyz, 3, "rpy_xyz(waist)")
		if relative:
			self.ctrl.waist_pos_exp[:] += pos
			self.ctrl.waist_att_exp[:] = self._compose_rpy_increment(self.ctrl.waist_att_exp, rpy)
		else:
			self.ctrl.waist_pos_exp[:] = pos
			self.ctrl.waist_att_exp[:] = rpy
		return self

	def set_waist_joint(self, q4, relative: bool = False) -> "maniSdkClass":
		"""设置对应控制目标参数。"""
		q = self._f32_vec(q4, 4, "waist_q")
		if relative:
			self.ctrl.waist_q_exp[:] += q
		else:
			self.ctrl.waist_q_exp[:] = q
		return self

	def set_left_arm_joint(self, q7, relative: bool = False) -> "maniSdkClass":
		"""设置对应控制目标参数。"""
		q = self._f32_vec(q7, 7, "arm_q_l")
		if relative:
			self.ctrl.arm_q_exp_l[:] += q
		else:
			self.ctrl.arm_q_exp_l[:] = q
		return self

	def set_right_arm_joint(self, q7, relative: bool = False) -> "maniSdkClass":
		"""设置对应控制目标参数。"""
		q = self._f32_vec(q7, 7, "arm_q_r")
		if relative:
			self.ctrl.arm_q_exp_r[:] += q
		else:
			self.ctrl.arm_q_exp_r[:] = q
		return self

	def set_head_joint(self, q2, sync_att: bool = False, relative: bool = False) -> "maniSdkClass":
		"""设置对应控制目标参数。"""
		q = self._f32_vec(q2, 2, "head_q")
		if relative:
			self.ctrl.head_q_exp[:] += q
		else:
			self.ctrl.head_q_exp[:] = q
		if sync_att:
			self.sync_head_att_from_joint()
		return self

	def set_head_att_exp(self, rpy_xyz, relative: bool = False) -> "maniSdkClass":
		"""设置对应控制目标参数。"""
		rpy = self._f32_vec(rpy_xyz, 3, "head_att_exp")
		if relative:
			self.ctrl.head_att_exp[:] = self._compose_rpy_increment(self.ctrl.head_att_exp, rpy)
		else:
			self.ctrl.head_att_exp[:] = rpy
		return self

	def sync_head_att_from_joint(self) -> "maniSdkClass":
		"""同步相关字段，保持语义一致。"""
		h = self.ctrl.head_q_exp
		self.ctrl.head_att_exp[0] = 0.0
		self.ctrl.head_att_exp[1] = float(h[1])
		self.ctrl.head_att_exp[2] = float(h[0])
		return self

	def set_cap(self, cap_l: float, cap_r: float, relative: bool = False) -> "maniSdkClass":
		"""设置对应控制目标参数。"""
		if relative:
			self.ctrl.cap_l += float(cap_l)
			self.ctrl.cap_r += float(cap_r)
		else:
			self.ctrl.cap_l = float(cap_l)
			self.ctrl.cap_r = float(cap_r)
		return self

	def set_base_vel(self, translation: float, rotation: float, relative: bool = False) -> "maniSdkClass":
		# 这里只写期望值 exp；实际值 status 由底盘状态查询回填。
		"""设置对应控制目标参数。"""
		t = float(translation)
		r = float(rotation)
		if relative:
			self.ctrl.car_translation_exp += t
			self.ctrl.car_rotation_exp += r
		else:
			self.ctrl.car_translation_exp = t
			self.ctrl.car_rotation_exp = r
		return self

	def send(self, ctrl=None, force: bool = False):
		# UDP 控制始终发送；若启用联动，则追加一帧底盘 TCP 速度命令。
		"""函数说明。"""
		if self._mani_send_paused and not force:
			raise RuntimeError("mani send paused: 当前暂停状态不允许发送")
		payload_ctrl = self.ctrl if ctrl is None else ctrl
		self.sk.sendto(self.packData(payload_ctrl), self.rbtIpPort)
		if self.chassis_tcp_on_send:
			self.send_chassis_command(
				float(payload_ctrl.car_translation_exp),
				float(payload_ctrl.car_rotation_exp),
			)

	def is_mani_send_paused(self) -> bool:
		"""判断当前状态条件是否成立。"""
		return bool(self._mani_send_paused)

	def get_send_interrupt_seq(self) -> int:
		"""获取对应状态或配置值。"""
		return _SendControlState.get_interrupt_seq()

	def get_last_interrupt_reason(self) -> str:
		"""获取对应状态或配置值。"""
		return _SendControlState.get_last_reason()

	def is_send_interrupted_since(self, seq: int) -> bool:
		"""判断当前状态条件是否成立。"""
		return self.get_send_interrupt_seq() != int(seq)

	def _build_ctrl_snapshot_from_sens(self) -> maniSdkCtrlDataClass:
		"""用当前实际状态构造一帧保持位姿的控制目标。"""
		ctrl = maniSdkCtrlDataClass()
		ctrl.arm_q_exp_l[:] = self.sens.q[0].astype(np.float32)
		ctrl.arm_q_exp_r[:] = self.sens.q[1].astype(np.float32)
		ctrl.waist_q_exp[:] = self.sens.waist.astype(np.float32)
		ctrl.head_q_exp[:] = self.sens.head.astype(np.float32)
		ctrl.cap_l = float(self.sens.cap_rate[0])
		ctrl.cap_r = float(self.sens.cap_rate[1])

		# 同步末端字段，便于不同下位机控制通道兼容。
		ctrl.arm_pos_exp_l[:] = self.sens.epos_h[0, :3].astype(np.float32)
		ctrl.arm_att_exp_l[:] = self.sens.epos_h[0, 3:6].astype(np.float32)
		ctrl.arm_pos_exp_r[:] = self.sens.epos_h[1, :3].astype(np.float32)
		ctrl.arm_att_exp_r[:] = self.sens.epos_h[1, 3:6].astype(np.float32)
		ctrl.waist_pos_exp[:] = self.sens.epos_waist[:3].astype(np.float32)
		ctrl.waist_att_exp[:] = self.sens.epos_waist[3:6].astype(np.float32)
		ctrl.head_att_exp[0] = 0.0
		ctrl.head_att_exp[1] = float(ctrl.head_q_exp[1])
		ctrl.head_att_exp[2] = float(ctrl.head_q_exp[0])

		ctrl.car_translation_exp = float(self.sens.base_vel[0])
		ctrl.car_rotation_exp = float(self.sens.base_vel[1])
		ctrl.car_translation_status = float(self.sens.base_vel[0])
		ctrl.car_rotation_status = float(self.sens.base_vel[1])
		return ctrl

	def pause_mani_send(self, fetch_state: bool = True, timeout: float = 0.05):
		"""暂停 mani 控制发送：后续 send() 不再发包。"""
		if fetch_state:
			try:
				self.fetch_robot_state(timeout=timeout)
			except Exception:
				pass
		self._paused_ctrl_snapshot = self._build_ctrl_snapshot_from_sens()
		self._mani_send_paused = True
		_SendControlState.trigger_interrupt("pause_mani_send")

	def resume_mani_send(self, replay_s: float = 1.0, hz: float = 50.0):
		"""恢复 mani 控制发送：先回放暂停快照，再恢复正常 send。"""
		snapshot = self._paused_ctrl_snapshot
		if snapshot is None:
			self._mani_send_paused = False
			return

		duration = max(0.0, float(replay_s))
		period = 1.0 / max(1.0, float(hz))
		t = 0.0
		while t < duration:
			start = time.time()
			self.send(snapshot, force=True)
			elapsed = time.time() - start
			sleep_t = period - elapsed
			if sleep_t > 0:
				time.sleep(sleep_t)
			t += period

		self._mani_send_paused = False

	def send_state_bridge(self):
		"""发送一帧状态桥接数据到状态端口。"""
		with self._state_cv:
			payload = self.sens.pack_bytes()
		self.sk.sendto(payload, self.stateIpPort)

	def wait_for_first_state_udp(
		self,
		total_timeout_s: float = 2.0,
		poll_timeout_s: float = 0.05,
	) -> bool:
		"""阻塞直到经状态 UDP 成功收编至少一帧合法 ``sens``，或超时。

		适用于无后台状态线程的 ``maniSdkClass``：循环调用 ``_exchange_state_once``。
		含 ``LinglongHSdkClass`` 后台线程时请用子类重载实现（避免与线程争用同一 socket）。
		"""
		deadline = time.monotonic() + max(0.0, float(total_timeout_s))
		poll = max(1e-3, float(poll_timeout_s))
		while time.monotonic() < deadline:
			if self._state_udp_received_ok:
				return True
			remain = deadline - time.monotonic()
			if remain <= 0:
				break
			self._exchange_state_once(min(poll, remain))
		return bool(self._state_udp_received_ok)

	def fetch_robot_state(self, timeout: float = 0.05) -> maniSdkSensDataClass:
		"""触发一次完整状态链路并返回 sens。"""
		self._exchange_state_once(timeout)
		return self.sens

	def get_battery_level(self) -> float:
		"""获取最近一次底盘状态查询到的电量（0~1，未知时为 NaN）。"""
		return float(self.battery_level)

	def recv(self) -> maniSdkSensDataClass:
		"""非阻塞接收一帧UDP状态并更新sens。"""
		buf = b""
		try:
			buf, _ = self.sk.recvfrom(2048)
		except BlockingIOError:
			pass
		except OSError:
			pass
		if len(buf) >= self._sens_pkt_len:
			with self._state_cv:
				try:
					self.sens.unpack_from(buf[: self._sens_pkt_len])
				except ValueError:
					pass
				else:
					self._state_udp_received_ok = True
					self._state_cv.notify_all()
		return self.sens

	def packData(self, ctrl: maniSdkCtrlDataClass) -> bytes:
		"""将控制对象打包为带CRC的字节流。"""
		payload = maniSdkCtrlDataClass.pack_payload(ctrl)
		crc = self.crc16_ccitt(payload)
		return payload + struct.pack("<H", crc)

	def unpackData(self, buf: bytes):
		"""将状态字节流解包写入sens。"""
		with self._state_cv:
			self.sens.unpack_from(buf)
			self._state_udp_received_ok = True
			self._state_cv.notify_all()

	def close(self):
		"""关闭全部socket资源。"""
		self._close_chassis_connection()
		self._close_chassis_state_connection()
		try:
			self.sk.close()
		except OSError:
			pass
